from __future__ import annotations

from cryptopilot.liquidity import LiquidityTracker


def test_persistent_wall_replenishment_and_liquidations() -> None:
    tracker = LiquidityTracker()
    t0 = 1_000_000

    tracker.update_orderbook(
        "TESTUSDT",
        "snapshot",
        [
            ["99.90", "1000"],
            ["99.80", "100"],
            ["99.70", "100"],
            ["99.60", "100"],
            ["99.50", "100"],
        ],
        [
            ["100.10", "100"],
            ["100.20", "100"],
            ["100.30", "100"],
            ["100.40", "100"],
            ["100.50", "100"],
        ],
        t0,
    )
    tracker.update_orderbook(
        "TESTUSDT",
        "delta",
        [["99.90", "1300"]],
        [],
        t0 + 5_000,
    )
    tracker.add_liquidation(
        "TESTUSDT",
        "SHORT",
        100.0,
        400.0,
        t0 + 7_000,
    )
    tracker.add_liquidation(
        "TESTUSDT",
        "LONG",
        100.0,
        100.0,
        t0 + 8_000,
    )

    snapshot = tracker.snapshot("TESTUSDT", t0 + 12_000)

    assert snapshot is not None
    assert snapshot.bid_wall_ratio is not None
    assert snapshot.bid_wall_ratio > 5
    assert snapshot.bid_wall_persistence_seconds >= 10
    assert snapshot.bid_replenishment_usdt_60s > 25_000
    assert snapshot.short_liquidation_usdt_60s == 40_000
    assert snapshot.long_liquidation_usdt_60s == 10_000
    assert snapshot.imbalance_top10 is not None
    assert snapshot.imbalance_top10 > 0


def test_new_wall_resets_persistence_clock() -> None:
    tracker = LiquidityTracker()
    t0 = 2_000_000
    tracker.update_orderbook(
        "TESTUSDT",
        "snapshot",
        [["99.90", "1000"], ["99.80", "100"], ["99.70", "100"]],
        [["100.10", "100"], ["100.20", "100"], ["100.30", "100"]],
        t0,
    )
    first = tracker.snapshot("TESTUSDT", t0 + 11_000)
    assert first is not None
    assert first.bid_wall_persistence_seconds >= 10

    tracker.update_orderbook(
        "TESTUSDT",
        "delta",
        [["99.90", "100"], ["99.80", "1500"]],
        [],
        t0 + 12_000,
    )
    second = tracker.snapshot("TESTUSDT", t0 + 13_000)

    assert second is not None
    assert second.bid_wall_price == 99.80
    assert second.bid_wall_persistence_seconds <= 1.5
