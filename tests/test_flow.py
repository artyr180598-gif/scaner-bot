from __future__ import annotations

from types import SimpleNamespace

from cryptopilot.flow import FlowTracker
from cryptopilot.live_radar import active_flow_candidates
from cryptopilot.models import Side


def _add_oi_history(tracker: FlowTracker, now_ms: int) -> None:
    samples = [
        (now_ms - 230_000, 100_000_000),
        (now_ms - 130_000, 100_050_000),
        (now_ms - 110_000, 100_060_000),
        (now_ms - 5_000, 100_500_000),
    ]
    for ts_ms, value in samples:
        tracker.add_ticker(
            "TESTUSDT",
            ts_ms,
            last_price=100.0,
            open_interest_value=value,
        )


def _add_directional_flow(tracker: FlowTracker, now_ms: int) -> None:
    # Baseline minute: balanced and deliberately smaller.
    for index, offset in enumerate((115, 105, 95, 85, 75, 65)):
        tracker.add_trade(
            "TESTUSDT",
            "Buy" if index % 2 == 0 else "Sell",
            99.50,
            10,
            now_ms - offset * 1_000,
        )

    # Current minute: growing aggressive buy flow, still below structural trigger 100.
    for index, offset in enumerate((55, 45, 35, 25, 15, 5)):
        tracker.add_trade(
            "TESTUSDT",
            "Buy",
            99.72 + index * 0.015,
            50,
            now_ms - offset * 1_000,
        )


def test_flow_snapshot_detects_cvd_volume_burst_and_oi_acceleration() -> None:
    now_ms = 1_000_000
    tracker = FlowTracker()
    _add_directional_flow(tracker, now_ms)
    _add_oi_history(tracker, now_ms)

    snapshot = tracker.snapshot("TESTUSDT", now_ms)

    assert snapshot is not None
    assert snapshot.fresh
    assert snapshot.delta_ratio_60s > 0.9
    assert snapshot.cvd_ratio_5m > 0.5
    assert snapshot.volume_burst_ratio is not None
    assert snapshot.volume_burst_ratio > 3
    assert snapshot.oi_change_2m_pct is not None
    assert snapshot.oi_change_2m_pct > 0.3
    assert snapshot.oi_acceleration_pct_per_min is not None
    assert snapshot.oi_acceleration_pct_per_min > 0
    assert snapshot.trade_count_60s == 6


def test_pre_bos_pressure_requires_confluence_and_stays_before_trigger() -> None:
    now_ms = 1_000_000
    tracker = FlowTracker()
    _add_directional_flow(tracker, now_ms)
    _add_oi_history(tracker, now_ms)

    # If price has already crossed the level, the pre-BOS engine must stand down.
    assert (
        tracker.pressure_event(
            "TESTUSDT",
            Side.LONG,
            99.80,
            min_notional_60s=1_000,
            delta_threshold=0.16,
            burst_threshold=1.4,
            min_oi_change_pct=0.10,
            min_score=70,
            now_ms=now_ms,
        )
        is None
    )

    event = tracker.pressure_event(
        "TESTUSDT",
        Side.LONG,
        100.0,
        min_notional_60s=1_000,
        delta_threshold=0.16,
        burst_threshold=1.4,
        min_oi_change_pct=0.10,
        min_score=70,
        now_ms=now_ms,
    )

    assert event is not None
    assert event.bias is Side.LONG
    assert event.event_type == "FLOW_BUILDUP"
    assert event.score >= 70
    assert event.price < event.trigger_price
    assert len(event.reasons) >= 3


def test_buy_absorption_can_be_detected_before_long_breakout() -> None:
    now_ms = 2_000_000
    tracker = FlowTracker()

    # Older buy pressure keeps 5m context constructive without inflating the previous minute.
    for offset in (260, 220, 180, 150):
        tracker.add_trade("TESTUSDT", "Buy", 99.80, 150, now_ms - offset * 1_000)

    for offset in (115, 105, 95, 85, 75, 65):
        tracker.add_trade("TESTUSDT", "Buy", 99.88, 8, now_ms - offset * 1_000)

    # Aggressive sellers hit bids, but price barely moves: passive buy absorption.
    for index, offset in enumerate((55, 45, 35, 25, 15, 5)):
        tracker.add_trade(
            "TESTUSDT",
            "Sell",
            99.90 - index * 0.003,
            45,
            now_ms - offset * 1_000,
        )
    _add_oi_history(tracker, now_ms)

    snapshot = tracker.snapshot("TESTUSDT", now_ms)
    assert snapshot is not None
    assert snapshot.absorption == "BUY_ABSORPTION"

    event = tracker.pressure_event(
        "TESTUSDT",
        Side.LONG,
        100.0,
        min_notional_60s=1_000,
        delta_threshold=0.16,
        burst_threshold=1.4,
        min_oi_change_pct=0.10,
        min_score=70,
        now_ms=now_ms,
    )
    assert event is not None
    assert event.event_type == "ABSORPTION"
    assert event.price < event.trigger_price


def test_active_flow_candidates_include_preselected_but_cap_load() -> None:
    preselected = {
        f"COIN{index}USDT": (Side.LONG, 100.0 + index)
        for index in range(30)
    }
    result = active_flow_candidates(
        early_report=None,
        smart_report=None,
        now_seconds=1_000,
        max_age=600,
        preselected=preselected,
        limit=24,
    )

    assert len(result) == 24
    assert "COIN0USDT" in result
    assert "COIN24USDT" not in result

    # Strong report candidates get priority over lower-level preselection.
    smart = SimpleNamespace(
        finished_at=SimpleNamespace(timestamp=lambda: 1_000),
        setups=[
            SimpleNamespace(
                symbol="TOPUSDT",
                bias=Side.SHORT,
                trigger_price=50.0,
            )
        ],
    )
    prioritized = active_flow_candidates(
        None,
        smart,
        1_000,
        600,
        preselected,
        limit=2,
    )
    assert list(prioritized)[0] == "TOPUSDT"


def test_live_spread_gate_blocks_illiquid_pre_bos_spike() -> None:
    now_ms = 3_000_000
    tracker = FlowTracker()
    _add_directional_flow(tracker, now_ms)
    _add_oi_history(tracker, now_ms)
    tracker.add_ticker(
        "TESTUSDT",
        now_ms - 1_000,
        last_price=99.85,
        open_interest_value=100_500_000,
        bid=99.0,
        ask=100.0,
        funding_rate=0.0001,
    )

    snapshot = tracker.snapshot("TESTUSDT", now_ms)
    assert snapshot is not None
    assert snapshot.spread_bps is not None
    assert snapshot.spread_bps > 12

    event = tracker.pressure_event(
        "TESTUSDT",
        Side.LONG,
        100.0,
        min_notional_60s=1_000,
        delta_threshold=0.16,
        burst_threshold=1.4,
        min_oi_change_pct=0.10,
        min_score=70,
        max_spread_bps=12,
        now_ms=now_ms,
    )
    assert event is None


def test_directional_funding_penalty_does_not_fake_a_confirmation() -> None:
    now_ms = 4_000_000
    normal = FlowTracker()
    hot = FlowTracker()
    for tracker in (normal, hot):
        _add_directional_flow(tracker, now_ms)
        _add_oi_history(tracker, now_ms)
    normal.add_ticker(
        "TESTUSDT",
        now_ms - 1_000,
        last_price=99.85,
        open_interest_value=100_500_000,
        bid=99.84,
        ask=99.86,
        funding_rate=0.0001,
    )
    hot.add_ticker(
        "TESTUSDT",
        now_ms - 1_000,
        last_price=99.85,
        open_interest_value=100_500_000,
        bid=99.84,
        ask=99.86,
        funding_rate=0.0012,
    )

    normal_event = normal.pressure_event(
        "TESTUSDT",
        Side.LONG,
        100.0,
        min_notional_60s=1_000,
        delta_threshold=0.16,
        burst_threshold=1.4,
        min_oi_change_pct=0.10,
        min_score=50,
        max_spread_bps=12,
        max_directional_funding_pct=0.08,
        now_ms=now_ms,
    )
    hot_event = hot.pressure_event(
        "TESTUSDT",
        Side.LONG,
        100.0,
        min_notional_60s=1_000,
        delta_threshold=0.16,
        burst_threshold=1.4,
        min_oi_change_pct=0.10,
        min_score=50,
        max_spread_bps=12,
        max_directional_funding_pct=0.08,
        now_ms=now_ms,
    )

    assert normal_event is not None
    assert hot_event is not None
    assert hot_event.score == normal_event.score - 10


def test_trade_id_deduplication_prevents_reconnect_double_count() -> None:
    now_ms = 5_000_000
    tracker = FlowTracker()
    for _ in range(2):
        tracker.add_trade(
            "TESTUSDT",
            "Buy",
            100.0,
            10.0,
            now_ms - 1_000,
            trade_id="same-trade-id",
        )

    snapshot = tracker.snapshot("TESTUSDT", now_ms)

    assert snapshot is not None
    assert snapshot.trade_count_60s == 1
    assert snapshot.notional_60s == 1_000.0
