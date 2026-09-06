from types import SimpleNamespace

import numpy as np
import pytest

from scripts.indicator_quality_study import confirms


def test_directional_filters_mirror_and_reject_missing():
    f = SimpleNamespace(cmf20=np.array([0.1] * 5), vwap_distance_atr=np.array([-0.5] * 5))
    assert confirms("cmf", f, 4, 1)
    assert not confirms("cmf", f, 4, -1)
    assert confirms("vwap", f, 4, -1)
    assert not confirms("vwap", f, 4, 1)
    f.cmf20[4] = np.nan
    assert not confirms("cmf", f, 4, 1)


def test_squeeze_uses_only_preceding_bars():
    f = SimpleNamespace(keltner_squeeze_ratio=np.array([2.0, 2.0, 2.0, 0.9, 2.0, 0.5]))
    assert confirms("squeeze", f, 4, 1)
    f.keltner_squeeze_ratio[3] = 2
    assert not confirms("squeeze", f, 4, 1)


def test_rvol_rejects_climax_and_unknown_rule():
    f = SimpleNamespace(relative_volume20=np.array([2.0] * 5))
    assert confirms("rvol", f, 4, 1)
    f.relative_volume20[4] = 5
    assert not confirms("rvol", f, 4, 1)
    with pytest.raises(ValueError):
        confirms("unknown", f, 4, 1)
