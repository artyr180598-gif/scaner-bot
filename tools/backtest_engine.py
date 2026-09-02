"""
tools/backtest_engine.py — walk-forward проверка движка `app/` на свечах.

Зачем: офлайн-тесты и selftest показывают, что конвейер работает, но не
отвечают на главный вопрос — **что происходит с сигналами дальше**. Этот скрипт
прогоняет реальный `SignalEngine` по истории окнами, исполняет план сделки
по следующему бару и считает исход по тем же правилам, что и журнал бота
(стоп приоритетнее цели на одном баре, истечение по горизонту).

Данные:
  --source DIR      каталог с CSV вида SYMBOL.csv (колонки: timestamp,open,
                    high,low,close,volume; timestamp — ms или ISO)
  --synthetic REGIM  синтетика для смоук-проверки (accumulation,pumped,…),
                    используется, когда реальных данных нет

Важно про интерпретацию:
  * на синтетике это проверка КОРРЕКТНОСТИ (частота сигналов, распределение
    уверенности, что исходы считаются), а НЕ доказательство прибыли;
  * на реальных данных — уже аргумент, но только walk-forward и с издержками.

Примеры:
  python -m tools.backtest_engine --synthetic breakout,range --seeds 1,2,3
  python -m tools.backtest_engine --source data/csv --limit 20
  python -m tools.backtest_engine --source data/csv --out backtest/results/engine_v2.md
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

from app.config.settings import Settings
from app.domain.models import (Candles, Direction, MarketContext, MarketSnapshot,
                               Timeframe, TickerInfo)
from app.presentation.format import fmt_pct
from app.signals.engine import SignalEngine

# Издержки round-trip (taker вход + taker выход), как в бектесте v4.
DEFAULT_COST_PCT = 0.16


# ---------------------------------------------------------------------------
# Данные
# ---------------------------------------------------------------------------

def load_csv(path: Path) -> pd.DataFrame:
    """CSV со свечами → DataFrame с колонками open/high/low/close/volume."""
    frame = pd.read_csv(path)
    frame.columns = [c.strip().lower() for c in frame.columns]
    if "timestamp" not in frame.columns:
        raise ValueError(f"{path.name}: нет колонки timestamp")
    stamp = frame["timestamp"]
    if pd.api.types.is_numeric_dtype(stamp):
        index = pd.to_datetime(stamp, unit="ms", utc=True)
    else:
        index = pd.to_datetime(stamp, utc=True)
    frame.index = index
    frame = frame[["open", "high", "low", "close", "volume"]].astype("float64")
    return frame[~frame.index.duplicated(keep="last")].sort_index()


def synthetic_frames(regimes: Sequence[str], seeds: Sequence[int],
                     bars: int) -> List[Tuple[str, pd.DataFrame]]:
    """Синтетические серии для смоук-прогона (без реального рынка)."""
    from app.data.synthetic import make_candles

    out: List[Tuple[str, pd.DataFrame]] = []
    for regime in regimes:
        for seed in seeds:
            candles = make_candles(f"{regime.upper()}{seed}/USDT", Timeframe.H1,
                                   regime=regime, seed=seed, bars=bars)
            out.append((f"{regime}#{seed}", candles.df))
    return out


def slice_window(frame: pd.DataFrame, end: int, tf: Timeframe,
                 lookback_bars: int, base_tf: Timeframe) -> Optional[Candles]:
    """
    Срез истории до ``end`` (не включая будущие бары) с ресемплингом в ``tf``.

    Ровно то, что делает продакшен: базовый ТФ качается, старшие строятся
    из него, дневной берётся длинной историей.
    """
    start = max(0, end - lookback_bars)
    chunk = frame.iloc[start:end]
    if len(chunk) < 30:
        return None
    candles = Candles(frame.attrs.get("symbol", "SYM/USDT"), base_tf, chunk,
                      last_closed=True, source="backtest")
    if tf is base_tf:
        return candles
    try:
        return candles.resample(tf)
    except ValueError:
        return None


def reanchor(candles: Candles, now: datetime) -> Candles:
    """
    Сдвигает метки времени так, будто последний бар закрылся только что.

    Без этого гейт свежести данных честно отвергает любое историческое окно
    («данные устарели на N часов») и бектест не видит ни одного сигнала.
    На индикаторы сдвиг не влияет: они работают по порядку баров.
    """
    lag = timedelta(minutes=candles.timeframe.minutes)
    shift = now - lag - candles.last_timestamp
    frame = candles.df.copy()
    frame.index = frame.index + shift
    if "timestamp" in frame.columns:
        frame["timestamp"] = frame.index
    return Candles(candles.symbol, candles.timeframe, frame,
                   last_closed=True, source=candles.source)


def build_snapshot(symbol: str, frame: pd.DataFrame, end: int, settings: Settings,
                   base_tf: Timeframe = Timeframe.H1) -> Optional[MarketSnapshot]:
    """Снимок рынка на момент ``end`` — без заглядывания в будущее."""
    now = datetime.now(timezone.utc)
    candles: Dict[Timeframe, Candles] = {}
    for tf in settings.analysis_timeframes:
        lookback = settings.bars_daily * 24 if tf is Timeframe.D1 else settings.bars_base
        got = slice_window(frame, end, tf, lookback, base_tf)
        if got is not None and len(got) >= 30:
            candles[tf] = reanchor(got, now)
    if base_tf not in candles or len(candles[base_tf]) < settings.min_bars_required:
        return None

    base = candles[base_tf]
    price = float(base.close.iloc[-1])
    day = base.close.iloc[-min(len(base), 24):]
    ticker = TickerInfo(
        symbol=symbol, last=price, quote_volume=50_000_000.0,
        base_volume=50_000_000.0 / max(price, 1e-9),
        change_pct=float((price / float(day.iloc[0]) - 1) * 100),
        high=float(base.high.iloc[-24:].max()),
        low=float(base.low.iloc[-24:].min()),
        bid=price * 0.9995, ask=price * 1.0005,
    )
    return MarketSnapshot(symbol=symbol, base=symbol.split("/")[0],
                          quote=symbol.split("/")[1] if "/" in symbol else "USDT",
                          exchange="backtest", candles=candles, ticker=ticker,
                          universe_stats={"change_24h_median": 0.0,
                                          "change_24h_std": 2.5})


# ---------------------------------------------------------------------------
# Исполнение плана
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Trade:
    symbol: str
    direction: str
    confidence: float
    entry: float
    stop: float
    targets: List[float]
    outcome: str
    r_result: float
    bars_held: int
    setup: str = ""


@dataclass(slots=True)
class Walk:
    """Один проход по истории одной серии."""

    signals: int = 0
    skipped_no_plan: int = 0
    unfilled: int = 0
    trades: List[Trade] = field(default_factory=list)


def simulate_plan(direction: Direction, entry_low: float, entry_high: float,
                  stop: float, targets: Sequence[float], future: pd.DataFrame,
                  horizon_bars: int, cost_pct: float) -> Optional[Trade]:
    """
    Исполнение плана по следующим барам.

    Правила (те же, что в журнале бота):
      * вход — если диапазон следующего бара накрывает зону входа, иначе
        сделка не состоялась (``unfilled``);
      * на одном баре задеты и стоп, и цель — засчитывается СТОП;
      * горизонт прошёл — выход по закрытию.
    """
    if future.empty:
        return None
    entry = (entry_low + entry_high) / 2
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    sign = 1 if direction is Direction.LONG else -1
    limit = min(horizon_bars, len(future))
    filled_at = 0
    filled = False

    for i in range(limit):
        row = future.iloc[i]
        high, low = float(row["high"]), float(row["low"])
        if not filled:
            # Лимитный вход: ждём, пока цена придёт в зону.
            if (low <= entry_high and high >= entry_low) if sign > 0 \
                    else (high >= entry_low and low <= entry_high):
                filled, filled_at = True, i
            continue
        stop_hit = (low <= stop) if sign > 0 else (high >= stop)
        hits = [k for k, tp in enumerate(targets)
                if (high >= tp if sign > 0 else low <= tp)]
        if stop_hit:
            return _trade(direction, entry, stop, targets, "STOP", -1.0,
                          i - filled_at + 1, cost_pct, risk)
        if hits:
            idx = max(hits)
            return _trade(direction, entry, stop, targets, f"TP{idx + 1}",
                          float(idx + 1), i - filled_at + 1, cost_pct, risk)

    if not filled:
        return None
    close = float(future.iloc[limit - 1]["close"])
    r = sign * (close - entry) / risk
    return _trade(direction, entry, stop, targets, "EXPIRED", r,
                  limit - filled_at, cost_pct, risk)


def _trade(direction: Direction, entry: float, stop: float, targets: Sequence[float],
           outcome: str, r_result: float, bars_held: int, cost_pct: float,
           risk: float) -> Trade:
    # Издержки в R: комиссия делится на расстояние до стопа в %.
    stop_pct = abs(stop / entry - 1) * 100 if entry else 0.0
    cost_r = (cost_pct / stop_pct) if stop_pct > 0 else 0.0
    return Trade(symbol="", direction=direction.value, confidence=0.0, entry=entry,
                 stop=stop, targets=list(targets), outcome=outcome,
                 r_result=r_result - cost_r, bars_held=bars_held)


# ---------------------------------------------------------------------------
# Прогон
# ---------------------------------------------------------------------------

def run_walk(symbol: str, frame: pd.DataFrame, engine: SignalEngine,
             settings: Settings, *, window_step: int = 24,
             horizon_bars: int = 48, cost_pct: float = DEFAULT_COST_PCT,
             context: Optional[MarketContext] = None) -> Walk:
    """Шагаем окном по истории, на каждом шаге — реальный анализ движком."""
    walk = Walk()
    base_tf = settings.base_timeframe
    minimum = settings.bars_base
    end = minimum
    while end < len(frame) - 2:
        snapshot = build_snapshot(symbol, frame, end, settings, base_tf)
        if snapshot is None:
            end += window_step
            continue
        signal = engine.analyze(snapshot, context or MarketContext())
        walk.signals += 1
        plan = signal.plan
        if signal.direction is Direction.WAIT or plan is None or not plan.is_valid():
            walk.skipped_no_plan += 1
            end += window_step
            continue
        future = frame.iloc[end:end + horizon_bars]
        trade = simulate_plan(signal.direction, plan.entry_low, plan.entry_high,
                              plan.stop, [t.price for t in plan.targets], future,
                              horizon_bars, cost_pct)
        if trade is None:
            walk.unfilled += 1
        else:
            trade.symbol = symbol
            trade.confidence = round(signal.confidence, 2)
            trade.setup = signal.setup
            walk.trades.append(trade)
        end += window_step
    return walk


# ---------------------------------------------------------------------------
# Отчёт
# ---------------------------------------------------------------------------

def summarise(walks: Sequence[Walk], label: str) -> Dict[str, object]:
    trades = [t for w in walks for t in w.trades]
    signals = sum(w.signals for w in walks)
    if not trades:
        return {"label": label, "signals": signals,
                "trades": 0,
                "note": "ни одна сделка не исполнилась — смотреть нечего"}
    wins = [t for t in trades if t.r_result > 0]
    gross_win = sum(t.r_result for t in wins)
    gross_loss = -sum(t.r_result for t in trades if t.r_result < 0)
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        equity += t.r_result
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    buckets: Dict[str, Dict[str, float]] = {}
    for lo, hi in ((0, 5), (5, 6.5), (6.5, 8), (8, 10.1)):
        group = [t for t in trades if lo <= t.confidence < hi]
        if not group:
            continue
        buckets[f"{lo:g}–{hi:g}"] = {
            "n": len(group),
            "win_rate": round(sum(1 for t in group if t.r_result > 0) / len(group) * 100, 1),
            "avg_r": round(sum(t.r_result for t in group) / len(group), 3),
        }
    return {
        "label": label,
        "signals": signals,
        "trades": len(trades),
        "unfilled": sum(w.unfilled for w in walks),
        "wait_or_no_plan": sum(w.skipped_no_plan for w in walks),
        "fill_rate": round(len(trades) / max(1, signals - sum(w.skipped_no_plan for w in walks)) * 100, 1),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "avg_r": round(sum(t.r_result for t in trades) / len(trades), 3),
        "total_r": round(sum(t.r_result for t in trades), 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "max_dd_r": round(max_dd, 2),
        "avg_bars": round(sum(t.bars_held for t in trades) / len(trades), 1),
        "calibration": buckets,
    }


def render_report(rows: Sequence[Dict[str, object]], source: str) -> str:
    lines = [
        "# Бектест движка app/ (walk-forward)",
        "",
        f"Источник данных: {source}",
        "Правила исполнения: лимитный вход в зону, стоп приоритетнее цели на одном",
        "баре, выход по горизонту; издержки round-trip включены в R.",
        "",
        "| выборка | анализов | сделок | исполнено | winrate | avg R | сумма R | PF | maxDD (R) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        if not row.get("trades"):
            lines.append(f"| {row['label']} | {row['signals']} | 0 | — | — | — | — | — | — |")
            continue
        lines.append(
            f"| {row['label']} | {row['signals']} | {row['trades']} | "
            f"{row['fill_rate']}% | {row['win_rate']}% | {row['avg_r']} | "
            f"{row['total_r']} | {row['profit_factor']} | {row['max_dd_r']} |")
    lines.append("")
    lines.append("## Калибровка по уверенности")
    lines.append("")
    lines.append("| выборка | корзина | n | winrate | avg R |")
    lines.append("|---|---|---|---|---|")
    for row in rows:
        for name, bucket in (row.get("calibration") or {}).items():
            lines.append(f"| {row['label']} | {name} | {bucket['n']:.0f} | "
                         f"{bucket['win_rate']}% | {bucket['avg_r']} |")
    lines.append("")
    lines.append("> Уверенность — мера согласия независимых групп аргументов, а не")
    lines.append("> вероятность прибыли. Таблица калибровки нужна именно для того,")
    lines.append("> чтобы это проверять, а не для того, чтобы обещать доходность.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="walk-forward бектест движка app/")
    parser.add_argument("--source", help="каталог с CSV-свечами")
    parser.add_argument("--synthetic", help="режимы синтетики через запятую")
    parser.add_argument("--seeds", default="1,2,3", help="сиды синтетики")
    parser.add_argument("--bars", type=int, default=3000, help="длина синтетической серии")
    parser.add_argument("--step", type=int, default=24, help="шаг окна, баров")
    parser.add_argument("--horizon", type=int, default=48, help="горизонт сделки, баров")
    parser.add_argument("--cost", type=float, default=DEFAULT_COST_PCT,
                        help="издержки round-trip, %")
    parser.add_argument("--limit", type=int, default=10, help="максимум серий")
    parser.add_argument("--confidence", type=float, default=None,
                        help="переопределить MIN_CONFIDENCE")
    parser.add_argument("--min-rr", type=float, default=None, help="переопределить MIN_RR")
    parser.add_argument("--out", help="куда сохранить markdown-отчёт")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    settings = Settings()
    settings.bars_base = min(settings.bars_base, 600)
    if args.confidence is not None:
        settings.min_confidence = args.confidence
    if args.min_rr is not None:
        settings.min_rr = args.min_rr
    engine = SignalEngine(settings)

    frames: List[Tuple[str, pd.DataFrame]] = []
    if args.source:
        folder = Path(args.source)
        if not folder.is_dir():
            print(f"нет каталога {folder}", file=sys.stderr)
            return 2
        for path in sorted(folder.glob("*.csv"))[:args.limit]:
            try:
                frame = load_csv(path)
            except Exception as exc:  # noqa: BLE001
                print(f"{path.name}: пропущен ({exc})", file=sys.stderr)
                continue
            frame.attrs["symbol"] = path.stem.upper().replace("-", "/")
            frames.append((path.stem.upper(), frame))
        source = f"CSV из {folder} ({len(frames)} серий)"
    elif args.synthetic:
        regimes = [r.strip() for r in args.synthetic.split(",") if r.strip()]
        seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
        frames = synthetic_frames(regimes, seeds, args.bars)[:args.limit]
        source = f"синтетика {regimes} × seeds {seeds} (НЕ реальный рынок)"
    else:
        print("нужно --source DIR или --synthetic regimes", file=sys.stderr)
        return 2

    if not frames:
        print("данных нет — бектест не запущен", file=sys.stderr)
        return 2

    rows: List[Dict[str, object]] = []
    all_walks: List[Walk] = []
    for symbol, frame in frames:
        walk = run_walk(symbol, frame, engine, settings, window_step=args.step,
                        horizon_bars=args.horizon, cost_pct=args.cost)
        all_walks.append(walk)
        rows.append(summarise([walk], symbol))
        print(f"  {symbol:<18} анализов {walk.signals:>4} · сделок {len(walk.trades):>3}"
              f" · без плана {walk.skipped_no_plan:>4} · не исполнилось {walk.unfilled:>3}")

    rows.append(summarise(all_walks, "ИТОГО"))
    report = render_report(rows, source)
    print()
    print(report)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"\nотчёт сохранён: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
