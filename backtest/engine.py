"""
engine.py — честный бектестер связки «спот ↔ перп» (S>F и F>S).

ПРИНЦИПЫ ЧЕСТНОСТИ (нарушать нельзя — см. AI_AGENTS/PLAYBOOK.md):
  1. Один и тот же SignalEngine, что и в живом сканере (import strategy).
  2. Никакого заглядывания в будущее: сигнал считается по закрытию бара t,
     сделка исполняется по OPEN бара t+1.
  3. Комиссии taker на ОБЕ ноги И на вход, И на выход (round-trip).
  4. Проскальзывание на каждую ногу (half-spread эмуляция стакана).
  5. Funding начисляется по реальным историческим ставкам в моменты
     фактических выплат между входом и выходом.
  6. PnL считается точно по обеим ногам (не приближённо «спред минус fee»).

Сравниваются три стратегии на одних данных:
  * OLD  — плоский порог v2 (вход при NET≥порог, где NET = гросс − комиссии
           входа — так считал старый бот; но издержки считаем честно, round-trip);
  * NEW  — адаптивный квантовый движок (z-score + персистентность + funding
           edge + round-trip NET);
  * NEW_NOFUNDING — то же без funding edge (абляция: сколько даёт funding).

Запуск: .venv/bin/python backtest/run_backtest.py --help
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy import SignalEngine, StrategyConfig  # noqa: E402

DATASET = Path(__file__).resolve().parent / "data_cache" / "dataset"

BAR_4H_MS = 4 * 3600 * 1000


# ---------------------------------------------------------------------------
# Данные
# ---------------------------------------------------------------------------

def load_symbol(sym: str) -> dict[str, pd.DataFrame]:
    """Загрузка компактного кеша одного символа (схема prepare_data.py)."""
    out: dict[str, pd.DataFrame] = {}
    for kind in ("spot_1h", "spot_1d", "perp_1d", "perp_4h", "funding"):
        path = DATASET / kind / f"{sym}.csv.gz"
        if not path.exists():
            raise FileNotFoundError(path)
        df = pd.read_csv(path)
        out[kind] = df
    return out


def build_4h_frames(sym: str, data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """
    Единый 4-часовой фрейм символа: спот (ресемпл 1ч→4ч) + перп 4ч.

    Колонки: spot_open, spot_close, perp_open, perp_close (реальные цены,
    без среза ask/bid — срез добавляется при расчёте спреда через slippage).
    """
    spot = data["spot_1h"].copy()
    spot["bucket"] = (spot["ts"] // BAR_4H_MS) * BAR_4H_MS
    grouped = spot.groupby("bucket")
    spot4 = pd.DataFrame({
        "spot_open": grouped["open"].first(),
        "spot_close": grouped["close"].last(),
    })

    perp = data["perp_4h"].set_index("ts")[["open", "close"]].rename(
        columns={"open": "perp_open", "close": "perp_close"}
    )

    frame = perp.join(spot4, how="inner").sort_index()
    frame = frame.dropna()
    frame["sym"] = sym
    return frame


# ---------------------------------------------------------------------------
# Сделки и портфель
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    strategy: str
    key: str
    sym: str
    direction: str            # S>F | F>S
    entry_ts: int = 0
    exit_ts: int = 0
    entry_spread_pct: float = 0.0    # гросс спред на входе (с проскальзыванием)
    exit_spread_pct: float = 0.0
    fees_pct: float = 0.0            # round-trip комиссии, % от номинала
    funding_pct: float = 0.0         # накопленный funding (со знаком), %
    pnl_pct: float = 0.0             # итог, % от номинала позиции
    hold_hours: float = 0.0
    exit_reason: str = ""


@dataclass
class Position:
    key: str
    sym: str
    direction: str
    signal_class: str = "CARRY"   # CARRY | REVERSION | CARRY+REVERSION | LEGACY
    entry_ts: int = 0
    entry_spot: float = 0.0   # цена входа ноги спот
    entry_perp: float = 0.0   # цена входа ноги перп
    entry_spread_pct: float = 0.0
    fees_paid_pct: float = 0.0
    funding_pct: float = 0.0
    last_fund_idx: int = 0
    alloc: float = 0.10


@dataclass
class StrategyResult:
    name: str
    trades: list[Trade] = field(default_factory=list)
    equity: list[tuple[int, float]] = field(default_factory=list)  # (ts, equity)

    def metrics(self, alloc_fraction: float) -> dict:
        trades = self.trades
        eq = [e for _, e in self.equity]
        n = len(trades)
        wins = [t for t in trades if t.pnl_pct > 0]
        losses = [t for t in trades if t.pnl_pct <= 0]
        gross_win = sum(t.pnl_pct for t in wins)
        gross_loss = abs(sum(t.pnl_pct for t in losses))
        total_days = max(
            (self.equity[-1][0] - self.equity[0][0]) / 86400000.0, 1e-9
        ) if len(self.equity) > 1 else 1.0
        total_return = eq[-1] - 100.0 if eq else 0.0
        max_dd = 0.0
        peak = -1e18
        for v in eq:
            peak = max(peak, v)
            max_dd = max(max_dd, peak - v)
        holds = [t.hold_hours for t in trades]
        return {
            "strategy": self.name,
            "trades": n,
            "winrate_pct": round(100.0 * len(wins) / n, 1) if n else 0.0,
            "avg_pnl_pct": round(sum(t.pnl_pct for t in trades) / n, 4) if n else 0.0,
            "total_return_pct": round(total_return, 2),
            "annualized_pct": round(((eq[-1] / 100.0) ** (365.0 / total_days) - 1.0) * 100.0, 2) if eq else 0.0,
            "max_drawdown_pct": round(max_dd, 2),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0),
            "avg_hold_hours": round(sum(holds) / len(holds), 1) if holds else 0.0,
            "avg_funding_pct": round(sum(t.funding_pct for t in trades) / n, 4) if n else 0.0,
            "avg_fees_pct": round(sum(t.fees_pct for t in trades) / n, 4) if n else 0.0,
            "alloc_fraction": alloc_fraction,
        }


# ---------------------------------------------------------------------------
# Симулятор
# ---------------------------------------------------------------------------

class BacktestSimulator:
    """
    Прогоняет стратегии по 4ч-барам. Каждая стратегия получает СВОЙ экземпляр
    SignalEngine (чтобы истории не пересекались) и свой портфель.

    Стратегии:
      OLD          — плоский порог v2 (как старый бот);
      CARRY_NAIVE  — «наивный carry»: вход при funding ≥ порога, без статистики;
      NEW          — квантовый движок (z/persistence/round-trip/funding edge);
      NEW_NOFUNDING— абляция: NEW без funding-гейта (чистая сходимость спреда).
    """

    def __init__(
        self,
        frames: dict[str, pd.DataFrame],
        funding: dict[str, pd.DataFrame],
        *,
        fee_spot_pct: float = 0.10,
        fee_fut_pct: float = 0.05,
        slippage_bps: float = 2.5,
        alloc_fraction: float = 0.10,      # база (CARRY)
        rev_alloc_fraction: float = 0.10,  # аллокация класса REVERSION
        max_concurrent: int = 8,
        old_threshold_pct: float = 2.0,
        naive_funding_pct: float = 0.01,   # %/8ч — порог наивного carry
        new_cfg: Optional[StrategyConfig] = None,
        nofunding_cfg: Optional[StrategyConfig] = None,
        max_hold_hours: float = 720.0,
        z_exit: float = 0.0,
        take_profit_pct: float = 1.0,
        stop_loss_pct: float = 1.5,
        funding_flip_hours: float = 72.0,
        funding_flip_threshold_pct: float = 0.10,
        converged_min_pct: float = 0.5,
        max_hold_rev_hours: float = 240.0,
        converged_min_rev_pct: float = 0.2,
        start_ts: int = 0,
        end_ts: int = 0,
    ) -> None:
        self.frames = frames
        self.funding = funding
        self.fee_spot = fee_spot_pct
        self.fee_fut = fee_fut_pct
        self.slip = slippage_bps / 10000.0
        self.alloc = alloc_fraction
        self.carry_alloc = alloc_fraction
        self.rev_alloc = rev_alloc_fraction
        self.max_concurrent = max_concurrent
        self.old_threshold = old_threshold_pct
        self.max_hold_hours = max_hold_hours
        self.z_exit = z_exit
        self.tp_pct = take_profit_pct
        self.sl_pct = stop_loss_pct
        self.flip_window_h = funding_flip_hours
        self.flip_threshold_pct = funding_flip_threshold_pct
        self.converged_min_pct = converged_min_pct
        self.max_hold_rev_hours = max_hold_rev_hours
        self.converged_min_rev_pct = converged_min_rev_pct
        self.start_ts = start_ts
        self.end_ts = end_ts

        base = new_cfg or StrategyConfig(
            mode="adaptive",
            history_seconds=60 * 24 * 3600.0,   # окно ~60 дней
            min_history=90,                     # ~15 дней 4ч-баров
            min_persistence=2,
            z_entry=1.5,                        # REV-класс: аномалия ≥1.5σ
            z_entry_min=-1.0,                   # CARRY-класс: z не против нас
            pct_entry=0.0,
            min_net_roundtrip_percent=0.10,     # CARRY: ожидаемый итог
            min_funding_edge_percent=0.05,      # CARRY: вклад funding
            min_net_reversion_percent=0.20,     # REV: сходимость после комиссий
            horizon_hours=168.0,
            max_halflife_hours=0.0,
            spot_taker_fee_percent=fee_spot_pct,
            futures_taker_fee_percent=fee_fut_pct,
        )
        self.new_cfg = base
        carry_only = StrategyConfig(**{**base.__dict__, "enable_reversion": False})
        rev_only = StrategyConfig(**{**base.__dict__, "enable_carry": False})
        naive = StrategyConfig(
            mode="adaptive",
            history_seconds=7 * 24 * 3600.0,
            min_history=1,                        # статистика не нужна
            min_persistence=1,
            z_entry=0.0, z_entry_min=-100.0, pct_entry=0.0,
            min_net_roundtrip_percent=-100.0,     # порогов нет
            # порог переносится в funding edge за горизонт: ставка ≥ X%/8ч
            min_funding_edge_percent=naive_funding_pct * (168.0 / 8.0),
            horizon_hours=168.0,
            max_halflife_hours=0.0,
            spot_taker_fee_percent=fee_spot_pct,
            futures_taker_fee_percent=fee_fut_pct,
        )
        self.strategies: dict[str, SignalEngine] = {
            "NEW": SignalEngine(base),
            "NEW_CARRYONLY": SignalEngine(carry_only),
            "NEW_REVONLY": SignalEngine(rev_only),
            "CARRY_NAIVE": SignalEngine(naive),
        }
        # OLD — плоский порог; тоже через SignalEngine (fixed-режим),
        # чтобы сравнение было на одном каркасе
        old_cfg = StrategyConfig(
            mode="fixed",
            min_spread_percent=old_threshold_pct,
            history_seconds=60 * 24 * 3600.0,
            min_history=1,
            min_persistence=1,
            spot_taker_fee_percent=fee_spot_pct,
            futures_taker_fee_percent=fee_fut_pct,
        )
        self.strategies["OLD"] = SignalEngine(old_cfg)
        # Наивный carry: actionable только когда funding-ставка ≥ порога
        self._naive_funding_pct = naive_funding_pct

        self.results = {name: StrategyResult(name=name) for name in self.strategies}
        self.positions: dict[str, dict[str, Position]] = {n: {} for n in self.strategies}
        self.equity = {n: 100.0 for n in self.strategies}
        self.pending_entries: dict[str, list[tuple[str, str, str, str, float]]] = {n: [] for n in self.strategies}
        self._exits: dict[str, list[tuple[Position, int, str]]] = {n: [] for n in self.strategies}
        self._fund_cache: dict[str, tuple[list[int], list[float], list[float]]] = {}

    def _apply_pnl(self, name: str, pnl_pct: float, alloc: Optional[float] = None) -> None:
        """PnL позиции в % номинала → компаундинг equity (доля alloc от equity)."""
        frac = self.alloc if alloc is None else alloc
        self.equity[name] *= 1.0 + frac * pnl_pct / 100.0

    def _alloc_for(self, signal_class: str) -> float:
        """Аллокация по классу: быстрая REVERSION приоритетнее медленного CARRY."""
        if signal_class in ("REVERSION", "LEGACY"):
            return self.rev_alloc
        return self.carry_alloc

    # --- спреды -----------------------------------------------------------------
    def spreads(self, frame_row: pd.Series) -> tuple[float, float]:
        """
        (gross_S>F, gross_F>S) в % с эмуляцией стакана: ask = close×(1+slip),
        bid = close×(1−slip). slippage-параметр = half-spread обеих книг.
        """
        spot_ask = float(frame_row["spot_close"]) * (1 + self.slip)
        spot_bid = float(frame_row["spot_close"]) * (1 - self.slip)
        perp_ask = float(frame_row["perp_close"]) * (1 + self.slip)
        perp_bid = float(frame_row["perp_close"]) * (1 - self.slip)
        gross_sf = (perp_bid - spot_ask) / spot_ask * 100.0
        gross_fs = (spot_bid - perp_ask) / perp_ask * 100.0
        return gross_sf, gross_fs

    # --- funding ------------------------------------------------------------------
    @staticmethod
    def _funding_arrays(df: pd.DataFrame) -> tuple[list[int], list[float], list[float]]:
        """(ts[], rate[], cumsum[]) для O(log n) запросов по времени."""
        ts = df["ts"].astype("int64").tolist()
        rate = df["rate_pct"].astype(float).tolist()
        cum = [0.0]
        for r in rate:
            cum.append(cum[-1] + r)
        return ts, rate, cum

    def _fund_lookup(self, sym: str) -> Optional[tuple[list[int], list[float], list[float]]]:
        got = self._fund_cache.get(sym)
        if got is None:
            df = self.funding.get(sym)
            if df is None or df.empty:
                return None
            got = self._funding_arrays(df)
            self._fund_cache[sym] = got
        return got

    def funding_between(self, sym: str, start_ms: int, end_ms: int, short_perp: bool) -> float:
        """Сумма ставок funding в (start, end], % от номинала перпа (со знаком позиции)."""
        arrs = self._fund_lookup(sym)
        if arrs is None:
            return 0.0
        ts, _rate, cum = arrs
        import bisect
        lo = bisect.bisect_right(ts, start_ms)      # первый ts > start
        hi = bisect.bisect_right(ts, end_ms)        # включительно до end
        total = cum[hi] - cum[lo]
        return total if short_perp else -total

    # --- исполнение -----------------------------------------------------------------
    def _fill_prices(self, row: pd.Series, direction: str, is_entry: bool) -> tuple[float, float]:
        """(spot_price, perp_price) исполнения: open следующего бара ± slip."""
        spot_open = float(row["spot_open"])
        perp_open = float(row["perp_open"])
        if is_entry:
            if direction == "S>F":   # покупаем спот по ask, шортим перп по bid
                return spot_open * (1 + self.slip), perp_open * (1 - self.slip)
            return spot_open * (1 - self.slip), perp_open * (1 + self.slip)  # F>S
        # выход: S>F → продаём спот по bid, выкупаем перп по ask; F>S наоборот
        if direction == "S>F":
            return spot_open * (1 - self.slip), perp_open * (1 + self.slip)
        return spot_open * (1 + self.slip), perp_open * (1 - self.slip)

    def _position_pnl_pct(self, pos: Position, exit_spot: float, exit_perp: float,
                          funding_pct: float) -> float:
        """Точный PnL позиции в % от номинала, обе ноги + funding − fees."""
        if pos.direction == "S>F":
            spot_leg = (exit_spot - pos.entry_spot) / pos.entry_spot * 100.0
            perp_leg = (pos.entry_perp - exit_perp) / pos.entry_perp * 100.0
        else:
            spot_leg = (pos.entry_spot - exit_spot) / pos.entry_spot * 100.0
            perp_leg = (exit_perp - pos.entry_perp) / pos.entry_perp * 100.0
        return spot_leg + perp_leg + funding_pct - pos.fees_paid_pct

    # --- главный цикл ------------------------------------------------------------------
    def run(self) -> dict[str, StrategyResult]:
        # общий таймлайн
        all_ts = sorted({ts for f in self.frames.values() for ts in f.index})
        if self.start_ts or self.end_ts:
            all_ts = [t for t in all_ts
                      if (not self.start_ts or t >= self.start_ts)
                      and (not self.end_ts or t <= self.end_ts)]

        entry_fee = self.fee_spot + self.fee_fut
        exit_fee = self.fee_spot + self.fee_fut

        for ts in all_ts:
            for name, engine in self.strategies.items():
                positions = self.positions[name]
                pending = self.pending_entries[name]

                # 1) исполнение отложенных входов (по open текущего бара)
                still_pending = []
                for sym, key, direction, sig_class, _sig in pending:
                    slot = f"{key}#{'CARRY' if sig_class in ('CARRY', 'CARRY+REVERSION') else 'REV'}"
                    if len(positions) >= self.max_concurrent or slot in positions:
                        continue
                    frame = self.frames.get(sym)
                    if frame is None or ts not in frame.index:
                        still_pending.append((sym, key, direction, sig_class, _sig))
                        continue
                    row = frame.loc[ts]
                    spot_px, perp_px = self._fill_prices(row, direction, is_entry=True)
                    if spot_px <= 0 or perp_px <= 0:
                        continue
                    if direction == "S>F":
                        entry_spread = (perp_px - spot_px) / spot_px * 100.0
                    else:
                        entry_spread = (spot_px - perp_px) / perp_px * 100.0
                    positions[slot] = Position(
                        key=key, sym=sym, direction=direction, signal_class=sig_class,
                        entry_ts=ts, entry_spot=spot_px, entry_perp=perp_px,
                        entry_spread_pct=entry_spread,
                        fees_paid_pct=entry_fee + exit_fee,
                        alloc=self._alloc_for(sig_class),
                    )
                self.pending_entries[name] = still_pending

                # 2) оценка сигналов по закрытию текущего бара
                new_signals: list[tuple[str, str, str, str, float]] = []
                for sym, frame in self.frames.items():
                    if ts not in frame.index:
                        continue
                    row = frame.loc[ts]
                    gross_sf, gross_fs = self.spreads(row)
                    for direction, gross in (("S>F", gross_sf), ("F>S", gross_fs)):
                        key = f"{sym}|spot|perp|{direction}"
                        carry_slot = f"{key}#CARRY"
                        rev_slot = f"{key}#REV"
                        net = gross - entry_fee
                        fund_rate = self._current_funding(sym, ts)
                        a = engine.observe_and_assess(
                            key=key,
                            ts=ts / 1000.0,
                            net_spread_percent=net,
                            gross_spread_percent=gross,
                            direction_spot_to_fut=(direction == "S>F"),
                            funding_rate_percent=fund_rate,
                            fillable_usd=1_000_000.0,  # глубина в бектесте не моделируем
                            fresh=True,
                        )
                        pos = positions.get(rev_slot) or positions.get(carry_slot)
                        if pos is not None:
                            hold_h = (ts - pos.entry_ts) / 3600000.0
                            # funding с момента последней фиксации
                            fund_now = self.funding_between(
                                sym, pos.last_fund_idx or pos.entry_ts, ts,
                                short_perp=(pos.direction == "S>F"))
                            total_fund = pos.funding_pct + fund_now
                            # плавающий PnL по ценам закрытия текущего бара
                            exit_sp, exit_pp = self._fill_prices(row, pos.direction, is_entry=False)
                            unreal = self._position_pnl_pct(pos, exit_sp, exit_pp, total_fund)
                            capture = pos.entry_spread_pct - gross
                            carry_like = pos.signal_class in ("CARRY", "CARRY+REVERSION")
                            max_hold = self.max_hold_hours if carry_like else self.max_hold_rev_hours
                            conv_min = self.converged_min_pct if carry_like else self.converged_min_rev_pct
                            # funding «против нас» по трейлинг-окну: если за
                            # последние N часов позиция ПЛАТИТ больше порога — выходим
                            trail = self._funding_trailing(sym, ts, self.flip_window_h)
                            signed_trail = trail if pos.direction == "S>F" else -trail
                            adverse = carry_like and signed_trail < -self.flip_threshold_pct
                            reason = ""
                            if unreal >= self.tp_pct:
                                reason = "take_profit"
                            elif unreal <= -self.sl_pct:
                                reason = "stop_loss"
                            elif adverse:
                                reason = "funding_flip"
                            elif hold_h >= max_hold:
                                reason = "timeout"
                            elif (a.zscore <= self.z_exit and capture > 0.0
                                  and unreal >= conv_min):
                                reason = "converged"
                            if reason:
                                # выход по open СЛЕДУЮЩЕГО бара → отложенный выход
                                pos.funding_pct = total_fund
                                pos.last_fund_idx = ts
                                self._queue_exit(name, pos, ts, reason=reason)
                                positions.pop(rev_slot if pos.key == key and pos.signal_class in ("REVERSION", "LEGACY") else carry_slot, None)
                            # если REV-сигнал на паре, где уже открыт CARRY — откроем
                            # отдельный REV-слот (тот не мешает, оба в одну сторону)
                            if positions.get(carry_slot) is not None and a.actionable and "REVERSION" in (a.signal_class or "") and rev_slot not in positions:
                                new_signals.append((sym, key, direction, "REVERSION", gross))
                            continue
                        if a.actionable:
                            sig_class = a.signal_class or "REVERSION"
                            slot = f"{key}#{'CARRY' if sig_class in ('CARRY', 'CARRY+REVERSION') else 'REV'}"
                            if slot not in [f"{p[1]}#{'CARRY' if p[3] in ('CARRY', 'CARRY+REVERSION') else 'REV'}" for p in pending]:
                                new_signals.append((sym, key, direction, sig_class, gross))

                # 3) отложить новые входы (исполнение на следующем баре)
                for sig in new_signals:
                    if len(self.pending_entries[name]) + len(positions) < self.max_concurrent:
                        self.pending_entries[name].append(sig)

                # 4) mark-to-market: equity = кэш + плавающий PnL открытых
                self._close_due_exits(name, ts)
                floating_units = 0.0
                for pos in positions.values():
                    frame = self.frames[pos.sym]
                    if ts in frame.index:
                        row = frame.loc[ts]
                        sp, pp = self._fill_prices(row, pos.direction, is_entry=False)
                        fund = self.funding_between(
                            pos.sym, pos.entry_ts, ts, short_perp=(pos.direction == "S>F"))
                        pnl = self._position_pnl_pct(pos, sp, pp, fund)
                        floating_units += self.equity[name] * pos.alloc * pnl / 100.0
                self.results[name].equity.append((ts, self.equity[name] + floating_units))

        # закрыть всё в конце
        last_ts = all_ts[-1] if all_ts else 0
        for name in self.strategies:
            for key, pos in list(self.positions[name].items()):
                self._queue_exit(name, pos, last_ts, reason="eod")
                self.positions[name].pop(key, None)
            self._close_due_exits(name, last_ts)
            if self.results[name].equity:
                self.results[name].equity.append(
                    (last_ts, self.equity[name]))
        return self.results

    # --- отложенные выходы --------------------------------------------------------------
    def _queue_exit(self, name: str, pos: Position, signal_ts: int, reason: str) -> None:
        self._exits.setdefault(name, []).append((pos, signal_ts, reason))

    def _close_due_exits(self, name: str, ts: int) -> None:
        """Закрывает позиции, чей сигнал выхода был на предыдущем баре (fill по open ts)."""
        due = self._exits.get(name, [])
        remaining = []
        for pos, signal_ts, reason in due:
            frame = self.frames.get(pos.sym)
            if frame is None or ts not in frame.index or ts <= signal_ts:
                remaining.append((pos, signal_ts, reason))
                continue
            row = frame.loc[ts]
            spot_px, perp_px = self._fill_prices(row, pos.direction, is_entry=False)
            fund = self.funding_between(
                pos.sym, pos.last_fund_idx or pos.entry_ts, ts,
                short_perp=(pos.direction == "S>F"))
            total_fund = pos.funding_pct + fund
            pnl = self._position_pnl_pct(pos, spot_px, perp_px, total_fund)
            self._apply_pnl(name, pnl, pos.alloc)
            exit_spread = (
                (perp_px - spot_px) / spot_px * 100.0 if pos.direction == "S>F"
                else (spot_px - perp_px) / perp_px * 100.0
            )
            self.results[name].trades.append(Trade(
                strategy=name, key=pos.key, sym=pos.sym, direction=pos.direction,
                entry_ts=pos.entry_ts, exit_ts=ts,
                entry_spread_pct=round(pos.entry_spread_pct, 4),
                exit_spread_pct=round(exit_spread, 4),
                fees_pct=round(pos.fees_paid_pct, 4),
                funding_pct=round(total_fund, 4),
                pnl_pct=round(pnl, 4),
                hold_hours=round((ts - pos.entry_ts) / 3600000.0, 1),
                exit_reason=reason,
            ))
        self._exits[name] = remaining

    # --- утилиты --------------------------------------------------------------------------
    def _current_funding(self, sym: str, ts: int) -> Optional[float]:
        """Последняя известная ставка funding на момент ts (%/8ч)."""
        arrs = self._fund_lookup(sym)
        if arrs is None:
            return None
        tss, rate, _cum = arrs
        import bisect
        idx = bisect.bisect_right(tss, ts)
        if idx == 0:
            return None
        return rate[idx - 1]

    def _funding_trailing(self, sym: str, ts: int, hours: float) -> float:
        """Сумма ставок funding за последние `hours` часов до ts (абс. знак рынка)."""
        arrs = self._fund_lookup(sym)
        if arrs is None:
            return 0.0
        tss, _rate, cum = arrs
        import bisect
        lo = bisect.bisect_right(tss, ts - int(hours * 3600000))
        hi = bisect.bisect_right(tss, ts)
        return cum[hi] - cum[lo]
