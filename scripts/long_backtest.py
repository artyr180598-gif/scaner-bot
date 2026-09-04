#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from cryptopilot.models import Candle
from cryptopilot.research import MultiTimeframeResearchBacktester, SymbolResearchResult

BASE_URL = "https://data.binance.vision/data/futures/um/monthly/klines"
RESEARCH_VERSION = "3.0.0"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Long multi-timeframe futures backtest")
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    parser.add_argument("--start", default="2022-01", help="First month, YYYY-MM")
    parser.add_argument("--end", default=datetime.now(UTC).strftime("%Y-%m"), help="Last month")
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--cache-dir", type=Path, default=Path(".research-cache"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--confidence", type=int, default=84, help="LONG quality threshold")
    parser.add_argument("--short-confidence", type=int, default=86)
    parser.add_argument("--risk-pct", type=float, default=0.5)
    parser.add_argument("--cost-bps", type=float, default=6.0, help="One-way fees + slippage")
    parser.add_argument("--min-adx", type=float, default=18.0)
    parser.add_argument("--min-efficiency", type=float, default=0.14)
    parser.add_argument("--min-ema-gap-atr", type=float, default=0.08)
    parser.add_argument("--max-atr-regime", type=float, default=2.8)
    parser.add_argument("--disable-relative-strength", action="store_true")
    parser.add_argument("--max-portfolio-risk-pct", type=float, default=1.0)
    parser.add_argument("--max-same-side", type=int, default=1)
    return parser.parse_args()


def months_between(start: str, end: str) -> list[str]:
    start_year, start_month = map(int, start.split("-"))
    end_year, end_month = map(int, end.split("-"))
    cursor = start_year * 12 + start_month - 1
    finish = end_year * 12 + end_month - 1
    if cursor > finish:
        raise ValueError("start month must not be after end month")
    output: list[str] = []
    while cursor <= finish:
        year, month_index = divmod(cursor, 12)
        output.append(f"{year:04d}-{month_index + 1:02d}")
        cursor += 1
    return output


def download_month(symbol: str, month: str, cache_dir: Path) -> bytes | None:
    target = cache_dir / symbol / f"{symbol}-15m-{month}.zip"
    if target.exists():
        return target.read_bytes()
    url = f"{BASE_URL}/{symbol}/15m/{symbol}-15m-{month}.zip"
    request = urllib.request.Request(url, headers={"User-Agent": "CryptoPilot-Research/2.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = response.read()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            return payload
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if attempt == 2:
                raise
        except (TimeoutError, urllib.error.URLError):
            if attempt == 2:
                raise
        time.sleep(0.8 * (2**attempt))
    return None


def parse_archive(payload: bytes) -> list[Candle]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = [name for name in archive.namelist() if name.endswith(".csv")]
        if not names:
            raise ValueError("Archive contains no CSV")
        text = io.TextIOWrapper(archive.open(names[0]), encoding="utf-8")
        output: list[Candle] = []
        for row in csv.reader(text):
            if len(row) < 8 or not row[0].strip().isdigit():
                continue
            open_time = int(row[0])
            if open_time > 100_000_000_000_000:  # Some archives use microseconds.
                open_time //= 1000
            output.append(
                Candle(
                    open_time_ms=open_time,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                    turnover=float(row[7]),
                )
            )
        return output


def load_symbol(
    symbol: str, months: list[str], cache_dir: Path, workers: int
) -> tuple[list[Candle], list[str]]:
    by_month: dict[str, list[Candle]] = {}
    missing: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 12))) as executor:
        futures = {
            executor.submit(download_month, symbol, month, cache_dir): month for month in months
        }
        for future in as_completed(futures):
            month = futures[future]
            payload = future.result()
            if payload is None:
                missing.append(month)
            else:
                by_month[month] = parse_archive(payload)
    candles = [item for month in months for item in by_month.get(month, [])]
    unique = {item.open_time_ms: item for item in candles}
    return [unique[key] for key in sorted(unique)], sorted(missing)


def finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


def portfolio_summary(
    results: list[SymbolResearchResult],
    risk_pct: float,
    max_portfolio_risk_pct: float,
    max_same_side: int,
) -> dict:
    candidates = sorted(
        (record for result in results for record in result.records),
        key=lambda item: (
            item.entry_time,
            -item.confidence,
            -abs(item.score),
            item.symbol,
        ),
    )
    max_positions = max(1, int(max_portfolio_risk_pct / risk_pct))
    records = []
    active = []
    rejected_for_risk = 0
    for candidate in candidates:
        active = [item for item in active if item.exit_time > candidate.entry_time]
        same_side = sum(item.side == candidate.side for item in active)
        if len(active) >= max_positions or same_side >= max_same_side:
            rejected_for_risk += 1
            continue
        records.append(candidate)
        active.append(candidate)
    equity = 1000.0
    peak = equity
    max_drawdown = 0.0
    yearly: dict[str, float] = {}
    for record in records:
        equity *= max(0.01, 1 + risk_pct / 100 * record.result_r)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)
        year = str(record.entry_time.year)
        yearly[year] = yearly.get(year, 0.0) + record.result_r
    wins = sum(record.result_r > 0 for record in records)
    gross_profit = sum(record.result_r for record in records if record.result_r > 0)
    gross_loss = abs(sum(record.result_r for record in records if record.result_r <= 0))
    net_r = sum(record.result_r for record in records)
    return {
        "trades": len(records),
        "wins": wins,
        "losses": len(records) - wins,
        "win_rate": wins / len(records) * 100 if records else 0.0,
        "net_r": net_r,
        "expectancy_r": net_r / len(records) if records else 0.0,
        "profit_factor": finite(gross_profit / gross_loss) if gross_loss else None,
        "ending_equity": equity,
        "return_pct": (equity / 1000 - 1) * 100,
        "max_drawdown_pct": max_drawdown,
        "yearly_r": {key: round(value, 3) for key, value in sorted(yearly.items())},
        "rejected_for_portfolio_risk": rejected_for_risk,
        "max_simultaneous_positions": max_positions,
        "max_same_side_positions": max_same_side,
        "note": "Combined sequence applies chronological portfolio and same-side risk caps.",
    }


def markdown_report(payload: dict) -> str:
    config = payload["configuration"]
    overall = payload["combined"]
    lines = [
        "# CryptoPilot long-horizon research report",
        "",
        f"Generated: {payload['generated_at']}",
        f"Source: {payload['source']}",
        f"Requested period: {config['start']} through {config['end']}",
        f"Automatic quality threshold: LONG {config['confidence']}/100; "
        f"SHORT {config['short_confidence']}/100",
        f"Adaptive gates: ADX ≥ {config['min_adx']}, efficiency ≥ "
        f"{config['min_efficiency']}, EMA gap/ATR ≥ {config['min_ema_gap_atr']}, "
        f"ATR shock ≤ {config['max_atr_regime']}x, relative strength "
        f"{'on' if config['relative_strength_filter'] else 'off'}",
        f"Risk per trade: {config['risk_pct']}%; one-way costs: {config['cost_bps']} bps",
        f"Portfolio cap: {config['max_portfolio_risk_pct']}% total risk; "
        f"max {config['max_same_side']} same-side position(s)",
        "",
        "## Results by symbol",
        "",
        "| Symbol | Actual history | Trades | Win rate | Net R | Exp. R | PF | Max DD | Return* |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in payload["symbols"]:
        factor = result["profit_factor"]
        factor_text = "∞" if factor is None and result["net_r"] > 0 else f"{factor:.2f}"
        lines.append(
            f"| {result['symbol']} | {result['first_bar'][:10]}–{result['last_bar'][:10]} | "
            f"{result['trades']} | {result['win_rate']:.1f}% | {result['net_r']:+.2f} | "
            f"{result['expectancy_r']:+.3f} | {factor_text} | "
            f"{result['max_drawdown_pct']:.2f}% | {result['return_pct']:+.2f}% |"
        )
    lines.extend(
        [
            "",
            "\\* Return compounds an isolated 1,000 USDT account at the configured risk per trade.",
            "",
            "## Combined chronological sequence",
            "",
            f"- Trades: {overall['trades']} ({overall['wins']} wins / {overall['losses']} losses)",
            f"- Win rate: {overall['win_rate']:.2f}%",
            f"- Net result: {overall['net_r']:+.2f}R",
            f"- Expectancy: {overall['expectancy_r']:+.3f}R per trade",
            f"- Profit factor: {overall['profit_factor'] or 0:.2f}",
            f"- Illustrative return: {overall['return_pct']:+.2f}%",
            f"- Maximum drawdown: {overall['max_drawdown_pct']:.2f}%",
            f"- Candidates rejected by portfolio cap: "
            f"{overall['rejected_for_portfolio_risk']}",
            "",
            "### Yearly net R",
            "",
            "| Year | Net R |",
            "|---|---:|",
        ]
    )
    lines.extend(f"| {year} | {value:+.2f} |" for year, value in overall["yearly_r"].items())
    lines.extend(
        [
            "",
            "## Method and limitations",
            "",
            "- Uses only information from candles closed before each decision; "
            "entry is the next 15m open.",
            "- Reproduces 15m/1h/4h score, BTC regime gate, confidence threshold, "
            "anti-chase rule, entry zone, technical stop and TP2.",
            "- If stop and target touch in the same 15m candle, the stop is counted first.",
            "- Costs include the configured fee plus slippage on entry and exit.",
            "- Public kline archives do not contain historical bid/ask spread, funding, "
            "open interest or the full cross-sectional shortlist. Those production "
            "filters are omitted.",
            "- The combined sequence limits concurrent risk and same-side exposure, but remains "
            "an execution approximation rather than a claim of realizable return.",
            "- Historical results do not guarantee future performance.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    months = months_between(args.start, args.end)
    symbols = list(dict.fromkeys(symbol.upper() for symbol in args.symbols))
    if "BTCUSDT" not in symbols:
        symbols.insert(0, "BTCUSDT")
    print(f"Loading {len(months)} months for {', '.join(symbols)}", flush=True)
    data: dict[str, list[Candle]] = {}
    missing: dict[str, list[str]] = {}
    for symbol in symbols:
        data[symbol], missing[symbol] = load_symbol(symbol, months, args.cache_dir, args.workers)
        print(
            f"{symbol}: {len(data[symbol]):,} bars, {len(missing[symbol])} missing months",
            flush=True,
        )
        if len(data[symbol]) < 4000:
            raise RuntimeError(f"Not enough history for {symbol}")

    tester = MultiTimeframeResearchBacktester(
        auto_confidence=args.confidence,
        short_confidence=args.short_confidence,
        risk_per_trade_pct=args.risk_pct,
        one_way_cost_bps=args.cost_bps,
        min_primary_adx=args.min_adx,
        min_efficiency_ratio=args.min_efficiency,
        min_ema_gap_atr=args.min_ema_gap_atr,
        max_atr_regime_ratio=args.max_atr_regime,
        relative_strength_filter=not args.disable_relative_strength,
    )
    results: list[SymbolResearchResult] = []
    for symbol in symbols:
        result = tester.run(symbol, data[symbol], data["BTCUSDT"])
        results.append(result)
        print(
            f"{symbol}: {result.trades} trades, {result.win_rate:.1f}% wins, "
            f"{result.net_r:+.2f}R, PF {result.profit_factor:.2f}",
            flush=True,
        )

    payload = {
        "research_version": RESEARCH_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "Binance Public Data (USD-M futures, 15m monthly klines)",
        "configuration": {
            "symbols": symbols,
            "start": args.start,
            "end": args.end,
            "confidence": args.confidence,
            "short_confidence": args.short_confidence,
            "risk_pct": args.risk_pct,
            "cost_bps": args.cost_bps,
            "min_adx": args.min_adx,
            "min_efficiency": args.min_efficiency,
            "min_ema_gap_atr": args.min_ema_gap_atr,
            "max_atr_regime": args.max_atr_regime,
            "relative_strength_filter": not args.disable_relative_strength,
            "max_portfolio_risk_pct": args.max_portfolio_risk_pct,
            "max_same_side": args.max_same_side,
        },
        "missing_months": missing,
        "symbols": [result.to_dict(include_records=False) for result in results],
        "combined": portfolio_summary(
            results,
            args.risk_pct,
            args.max_portfolio_risk_pct,
            args.max_same_side,
        ),
        "trades": [record.to_dict() for result in results for record in result.records],
        "limitations": [
            "No historical spread, funding, open-interest or cross-sectional shortlist filter.",
            "Combined return uses portfolio caps but remains an execution approximation.",
            "No future performance guarantee.",
        ],
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "long_backtest.json"
    markdown_path = args.output_dir / "long_backtest.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = markdown_report(payload)
    markdown_path.write_text(markdown, encoding="utf-8")
    print("\n" + markdown, flush=True)


if __name__ == "__main__":
    main()
