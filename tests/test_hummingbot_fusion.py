from cryptopilot.hummingbot_fusion import evaluate
from cryptopilot.models import Side, Ticker


def ticker(**kwargs):
    base = dict(
        symbol="BTCUSDT",
        last=100.0,
        bid=99.99,
        ask=100.01,
        turnover_24h=50_000_000,
        volume_24h=500_000,
    )
    base.update(kwargs)
    return Ticker(**base)


def test_hummingbot_fusion_supports_aligned_flow_and_book():
    result = evaluate(
        Side.LONG,
        ticker(taker_buy_ratio=0.60, orderbook_imbalance=0.18),
    )
    assert result.score_delta > 0
    assert result.data_points == 2
    assert result.reasons


def test_hummingbot_fusion_penalizes_opposed_microstructure():
    result = evaluate(
        Side.LONG,
        ticker(taker_buy_ratio=0.35, orderbook_imbalance=-0.20),
    )
    assert result.score_delta < 0
    assert result.risks


def test_hummingbot_fusion_never_trades():
    result = evaluate(
        Side.NO_TRADE,
        ticker(taker_buy_ratio=0.70, orderbook_imbalance=0.50),
    )
    assert result.score_delta == 0


def test_hummingbot_fusion_uses_near_book_and_execution_depth():
    result = evaluate(
        Side.LONG,
        ticker(
            taker_buy_ratio=0.60,
            orderbook_imbalance=0.10,
            near_book_imbalance=0.20,
            buy_slippage_10k_bps=2.5,
            sell_slippage_10k_bps=3.0,
        ),
    )
    assert result.score_delta > 0
    assert result.data_points >= 4
