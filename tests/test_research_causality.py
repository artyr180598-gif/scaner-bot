from dataclasses import replace

import numpy as np

from cryptopilot.research import feature_arrays


def test_entry_features_do_not_use_future_bars(candle_factory):
    bars = candle_factory(count=320, interval="15", direction=1)
    changed = bars[:260] + [
        replace(
            b,
            open=b.open * 2,
            high=b.high * 2,
            low=b.low * 2,
            close=b.close * 2,
            volume=b.volume * 10,
        )
        for b in bars[260:]
    ]
    original = feature_arrays(bars)
    perturbed = feature_arrays(changed)
    for field in (
        "score",
        "ema20",
        "rsi14",
        "atr14",
        "cmf20",
        "vwap_distance_atr",
        "keltner_squeeze_ratio",
        "relative_volume20",
    ):
        np.testing.assert_allclose(
            getattr(original, field)[:260], getattr(perturbed, field)[:260], equal_nan=True
        )
