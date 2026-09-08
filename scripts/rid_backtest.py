#!/usr/bin/env python3
"""Causal candle-core backtest for the isolated RID strategy.

Historical archives do not contain the production spread/OI/order-book/taker snapshot,
so this test intentionally validates only the closed-candle core.  Production automatic
alerts remain stricter because they require at least one live flow confirmation.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import io
import json
import statistics
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from cryptopilot.config import Settings
from cryptopilot.indicators import atr, dmi, ema, supertrend
from cryptopilot.models import Candle, Side, Ticker
from cryptopilot.rid_strategy import _latest_impulse, _plan

BASE = "https://data.binance.vision/data/futures/um/monthly/klines"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest the RID closed-candle core")
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["ETHUSDT", "SOLUSDT", "XRPUSDT", "LINKUSDT", "ADAUSDT", "AVAXUSDT"],
    )
    parser.add_argument("--start", default="2026-03")
    parser.add_argument("--end", default="2026-08")
    parser.add_argument("--cache-dir", type=Path, default=Path(".rid-research-cache"))
    parser.add_argument("--output", type=Path, default=Path("rid_backtest.json"))
    parser.add_argument("--workers", type=int, default=8)
    return parser.parse_args()


def months_between(start: str, end: str) -> list[str]:
    sy, sm = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    cursor, finish = sy * 12 + sm - 1, ey * 12 + em - 1
    if cursor > finish:
        raise ValueError("start must not be after end")
    months: list[str] = []
    while cursor <= finish:
        year, month = divmod(cursor, 12)
        months.append(f"{year:04d}-{month + 1:02d}")
        cursor += 1
    return months


def _download(symbol: str, month: str, cache: Path) -> bytes | None:
    target = cache / symbol / f"{symbol}-5m-{month}.zip"
    if target.exists():
        return target.read_bytes()
    url = f"{BASE}/{symbol}/5m/{symbol}-5m-{month}.zip"
    request = urllib.request.Request(url, headers={"User-Agent": "CryptoPilot-RID/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return payload


def _parse(payload: bytes) -> list[Candle]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        name = next(name for name in archive.namelist() if name.endswith(".csv"))
        text = io.TextIOWrapper(archive.open(name), encoding="utf-8")
        rows: list[Candle] = []
        for row in csv.reader(text):
            if len(row) < 8 or not row[0].isdigit():
                continue
            timestamp = int(row[0])
            if timestamp > 100_000_000_000_000:
                timestamp //= 1000
            rows.append(
                Candle(
                    timestamp,
                    float(row[1]),
                    float(row[2]),
                    float(row[3]),
                    float(row[4]),
                    float(row[5]),
                    float(row[7]),
                )
            )
        return rows


def load(symbol: str, months: list[str], cache: Path, workers: int) -> list[Candle]:
    data: dict[str, list[Candle]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(_download, symbol, month, cache): month for month in months}
        for future in as_completed(jobs):
            payload = future.result()
            if payload is not None:
                data[jobs[future]] = _parse(payload)
    unique = {bar.open_time_ms: bar for month in months for bar in data.get(month, [])}
    return [unique[key] for key in sorted(unique)]


def resample(bars: list[Candle], minutes: int) -> list[Candle]:
    duration = minutes * 60_000
    groups: dict[int, list[Candle]] = {}
    for bar in bars:
        groups.setdefault(bar.open_time_ms // duration * duration, []).append(bar)
    expected = minutes // 5
    output: list[Candle] = []
    for timestamp in sorted(groups):
        rows = groups[timestamp]
        if len(rows) != expected:
            continue
        output.append(
            Candle(
                timestamp,
                rows[0].open,
                max(row.high for row in rows),
                min(row.low for row in rows),
                rows[-1].close,
                sum(row.volume for row in rows),
                sum(row.turnover for row in rows),
            )
        )
    return output


def _summary(records: list[dict]) -> dict:
    closed = [row for row in records if row["outcome"] not in {"NO_ENTRY", "CENSORED"}]
    values = [row["result_r"] for row in closed]
    wins = sum(value > 0 for value in values)
    profit = sum(value for value in values if value > 0)
    loss = abs(sum(value for value in values if value <= 0))
    running = peak = drawdown = 0.0
    for value in values:
        running += value
        peak = max(peak, running)
        drawdown = max(drawdown, peak - running)
    return {
        "candidates": len(records),
        "closed": len(closed),
        "no_entry": sum(row["outcome"] == "NO_ENTRY" for row in records),
        "wins": wins,
        "losses": len(values) - wins,
        "win_rate_pct": wins / len(values) * 100 if values else 0.0,
        "expectancy_r": statistics.mean(values) if values else 0.0,
        "profit_factor": profit / loss if loss else None,
        "net_r": sum(values),
        "max_drawdown_r": drawdown,
    }


def _advance(plan, future: list[Candle], cost_bps: float) -> dict:
    entry = None
    entered_at = None
    expiry = int(plan.expires_at.timestamp() * 1000)
    for bar in future:
        if bar.open_time_ms >= expiry:
            break
        if bar.low <= plan.entry_high and bar.high >= plan.entry_low:
            entry = min(max(bar.open, plan.entry_low), plan.entry_high)
            entered_at = bar.open_time_ms
            break
    if entry is None or entered_at is None:
        return {"outcome": "NO_ENTRY", "result_r": 0.0}
    is_long = plan.stop_loss < entry
    risk = abs(entry - plan.stop_loss)
    horizon = entered_at + 72 * 3_600_000
    exit_price = future[-1].close
    outcome = "TIME"
    tp1_hit = False
    for bar in future:
        if bar.open_time_ms < entered_at:
            continue
        if bar.open_time_ms >= horizon:
            break
        active_stop = entry if tp1_hit else plan.stop_loss
        stop = bar.low <= active_stop if is_long else bar.high >= active_stop
        target_price = plan.take_profit_2 if tp1_hit else plan.take_profit_1
        target = bar.high >= target_price if is_long else bar.low <= target_price
        if stop:
            # Same-bar ambiguity is resolved against the strategy.
            exit_price = active_stop
            outcome = "TP1_BE" if tp1_hit else "SL"
            break
        if target:
            if tp1_hit:
                exit_price = plan.take_profit_2
                outcome = "TP2"
                break
            tp1_hit = True
    remaining_r = ((exit_price - entry) if is_long else (entry - exit_price)) / risk
    if tp1_hit:
        gross = 0.5 + 0.5 * remaining_r
        exit_notional = 0.5 * plan.take_profit_1 + 0.5 * exit_price
    else:
        gross = remaining_r
        exit_notional = exit_price
    costs = (entry + exit_notional) * cost_bps / 10_000 / risk
    return {"outcome": outcome, "result_r": gross - costs}


def run_symbol(symbol: str, bars5: list[Candle], settings: Settings) -> list[dict]:
    bars15, bars60 = resample(bars5, 15), resample(bars5, 60)
    close_times15 = [bar.open_time_ms + 900_000 for bar in bars15]
    close_times60 = [bar.open_time_ms + 3_600_000 for bar in bars60]
    highs = np.asarray([bar.high for bar in bars5])
    lows = np.asarray([bar.low for bar in bars5])
    closes = np.asarray([bar.close for bar in bars5])
    atrs = atr(highs, lows, closes, 14)
    close15 = np.asarray([bar.close for bar in bars15])
    high15 = np.asarray([bar.high for bar in bars15])
    low15 = np.asarray([bar.low for bar in bars15])
    ema20_15, ema50_15 = ema(close15, 20), ema(close15, 50)
    plus15, minus15, _ = dmi(high15, low15, close15, 14)
    close60 = np.asarray([bar.close for bar in bars60])
    high60 = np.asarray([bar.high for bar in bars60])
    low60 = np.asarray([bar.low for bar in bars60])
    ema20_60, ema50_60 = ema(close60, 20), ema(close60, 50)
    trend60, _ = supertrend(high60, low60, close60)
    records: list[dict] = []
    blocked_until = 0
    potential: set[int] = set()
    for burst_index in range(50, len(bars5) - 2):
        volume_baseline = statistics.median(
            bar.volume for bar in bars5[burst_index - 30 : burst_index - 5]
        )
        leg = bars5[burst_index - 5 : burst_index + 1]
        displacement = abs(leg[-1].close - leg[0].open)
        if (
            bars5[burst_index].volume
            >= settings.rid_min_impulse_rvol * max(volume_baseline, 1e-12)
            and displacement >= settings.rid_min_impulse_atr * atrs[burst_index]
        ):
            potential.update(range(burst_index + 2, min(burst_index + 19, len(bars5))))
    for index in sorted(potential):
        if index < 260 or index >= len(bars5) - 865:
            continue
        decision = bars5[index].open_time_ms + 300_000
        if decision < blocked_until:
            continue
        current = bars5[index]
        previous = bars5[index - 3 : index]
        reactivation = (
            current.close > current.open and current.close > max(bar.close for bar in previous)
        ) or (
            current.close < current.open and current.close < min(bar.close for bar in previous)
        )
        recent = bars5[index - 29 : index + 1]
        baseline = statistics.median(bar.volume for bar in bars5[index - 49 : index - 29])
        if (
            not reactivation
            or max(bar.high for bar in recent) - min(bar.low for bar in recent)
            < settings.rid_min_impulse_atr * atrs[index]
            or max(bar.volume for bar in recent) < settings.rid_min_impulse_rvol * baseline
        ):
            continue
        window = bars5[index - 259 : index + 1]
        plausible = False
        sides = (Side.LONG, Side.SHORT) if settings.rid_short_enabled else (Side.LONG,)
        for side in sides:
            leg = _latest_impulse(
                window,
                atrs[index],
                side,
                min_atr=settings.rid_min_impulse_atr,
                max_atr=settings.rid_max_impulse_atr,
                min_rvol=settings.rid_min_impulse_rvol,
            )
            if leg is None:
                continue
            post = window[leg.extreme_index + 1 :]
            if len(post) < 2:
                continue
            size = abs(leg.extreme - leg.origin)
            impulse_volume = max(
                bar.volume for bar in window[leg.start_index : leg.extreme_index + 1]
            )
            contraction = statistics.median(bar.volume for bar in post[:-1]) / max(
                impulse_volume, 1e-12
            )
            if side is Side.LONG:
                pullback = min(bar.low for bar in post)
                retracement = (leg.extreme - pullback) / size
                side_reactivation = current.close > current.open and current.close > max(
                    bar.close for bar in previous
                )
            else:
                pullback = max(bar.high for bar in post)
                retracement = (pullback - leg.extreme) / size
                side_reactivation = current.close < current.open and current.close < min(
                    bar.close for bar in previous
                )
            if (
                side_reactivation
                and contraction <= settings.rid_max_pullback_volume_ratio
                and settings.rid_min_retracement
                <= retracement
                <= settings.rid_max_retracement
            ):
                plausible = True
                break
        if not plausible:
            continue
        i15 = bisect.bisect_right(close_times15, decision)
        i60 = bisect.bisect_right(close_times60, decision)
        if i15 < 260 or i60 < 260:
            continue
        dmi_total = plus15[i15 - 1] + minus15[i15 - 1]
        dmi_spread = (
            (plus15[i15 - 1] - minus15[i15 - 1]) / dmi_total * 100
            if dmi_total > 1e-12
            else 0.0
        )
        evaluated: list[tuple[int, Side, object, dict[str, float]]] = []
        sides = (Side.LONG, Side.SHORT) if settings.rid_short_enabled else (Side.LONG,)
        for side in sides:
            leg = _latest_impulse(
                window,
                atrs[index],
                side,
                min_atr=settings.rid_min_impulse_atr,
                max_atr=settings.rid_max_impulse_atr,
                min_rvol=settings.rid_min_impulse_rvol,
            )
            if leg is None:
                continue
            post = window[leg.extreme_index + 1 :]
            if len(post) < 2:
                continue
            impulse_volume = max(
                bar.volume for bar in window[leg.start_index : leg.extreme_index + 1]
            )
            contraction = statistics.median(bar.volume for bar in post[:-1]) / max(
                impulse_volume, 1e-12
            )
            size = abs(leg.extreme - leg.origin)
            if side is Side.LONG:
                pullback = min(bar.low for bar in post)
                retracement = (leg.extreme - pullback) / size
                recovery = (current.close - pullback) / size
                reactivated = current.close > current.open and current.close > max(
                    bar.close for bar in previous
                )
                trend15_ok = ema20_15[i15 - 1] > ema50_15[i15 - 1] and dmi_spread > -8
                trend60_ok = (
                    ema20_60[i60 - 1] > ema50_60[i60 - 1] or trend60[i60 - 1] > 0
                )
            else:
                pullback = max(bar.high for bar in post)
                retracement = (pullback - leg.extreme) / size
                recovery = (pullback - current.close) / size
                reactivated = current.close < current.open and current.close < min(
                    bar.close for bar in previous
                )
                trend15_ok = ema20_15[i15 - 1] < ema50_15[i15 - 1] and dmi_spread < 8
                trend60_ok = (
                    ema20_60[i60 - 1] < ema50_60[i60 - 1] or trend60[i60 - 1] < 0
                )
            if (
                contraction > settings.rid_max_pullback_volume_ratio
                or not settings.rid_min_retracement
                <= retracement
                <= settings.rid_max_retracement
                or recovery < 0.18
                or not reactivated
                or not trend15_ok
            ):
                continue
            score = min(
                100,
                round(
                    20
                    + min(15, (leg.size_atr - settings.rid_min_impulse_atr) * 5 + 7)
                    + min(15, (leg.volume_ratio - settings.rid_min_impulse_rvol) * 4 + 7)
                    + min(15, max(0, 1 - contraction) * 18)
                    + (15 if 0.30 <= retracement <= 0.70 else 8)
                    + 18
                    + (10 if trend60_ok else 3)
                ),
            )
            evaluated.append(
                (
                    score,
                    side,
                    leg,
                    {
                        "impulse_atr": leg.size_atr,
                        "impulse_rvol": leg.volume_ratio,
                        "contraction": contraction,
                        "retracement": retracement,
                        "recovery": recovery,
                        "trend60": float(trend60_ok),
                        "ema60_direction": float(
                            1 if ema20_60[i60 - 1] > ema50_60[i60 - 1] else -1
                        ),
                        "supertrend60_direction": float(trend60[i60 - 1]),
                        "dmi15": float(dmi_spread),
                    },
                )
            )
        if not evaluated:
            continue
        score, side, leg, metrics = max(evaluated, key=lambda row: row[0])
        if score < settings.rid_auto_min_score:
            continue
        quote = current.close
        ticker = Ticker(symbol, quote, quote, quote, 1e9, 1e9)
        plan, _ = _plan(
            side,
            ticker,
            window,
            leg,
            atrs[index],
            settings,
            datetime.fromtimestamp(decision / 1000, UTC),
        )
        if plan is None:
            continue
        result = _advance(
            plan,
            bars5[index + 1 : index + 866],
            settings.paper_one_way_cost_bps,
        )
        records.append(
            {
                "symbol": symbol,
                "signal_at": datetime.fromtimestamp(decision / 1000, UTC).isoformat(),
                "side": side.value,
                "score": score,
                "outcome": result["outcome"],
                "result_r": result["result_r"],
                "atr_pct": float(atrs[index] / current.close * 100),
                **metrics,
            }
        )
        blocked_until = decision + settings.rid_symbol_cooldown_minutes * 60_000
    return records


def main() -> None:
    args = arguments()
    months = months_between(args.start, args.end)
    settings = Settings(_env_file=None)
    all_records: list[dict] = []
    per_symbol: dict[str, dict] = {}
    for symbol in args.symbols:
        bars = load(symbol, months, args.cache_dir, args.workers)
        records = run_symbol(symbol, bars, settings)
        all_records.extend(records)
        per_symbol[symbol] = _summary(records)
        print(symbol, per_symbol[symbol], flush=True)
    midpoint = datetime.fromisoformat(f"{months[len(months) // 2]}-01T00:00:00+00:00")
    development = [r for r in all_records if datetime.fromisoformat(r["signal_at"]) < midpoint]
    validation = [r for r in all_records if datetime.fromisoformat(r["signal_at"]) >= midpoint]
    payload = {
        "protocol": (
            "Closed 5m/15m/1h candles only; decision after close; next-bar execution; "
            "stop-first same-bar rule; 50% TP1 then breakeven, 50% TP2; "
            "6 bps each way. Live flow gate cannot be backtested."
        ),
        "configuration": {
            "months": months,
            "symbols": args.symbols,
            "rid": {
                key: value
                for key, value in settings.model_dump().items()
                if key.startswith("rid_")
            },
        },
        "per_symbol": per_symbol,
        "development": _summary(development),
        "validation": _summary(validation),
        "combined": _summary(all_records),
        "records": all_records,
    }
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {key: payload[key] for key in ("development", "validation", "combined")},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
