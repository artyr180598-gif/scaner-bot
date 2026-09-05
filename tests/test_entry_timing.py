from cryptopilot.entry_timing import timing_vetoes


def test_reported_conflicts_are_vetoed():
    assert timing_vetoes(True, 85.2, 1, -1, 77.1, 68, 57.1, 5.56)  # LINK
    assert timing_vetoes(True, 51.1, 1, 1, 72.7, 80.5, 79.3, 1.77)  # NEAR
    assert timing_vetoes(True, -10.5, -1, 1, 41.9, 55.9, 64.4, 1.05)  # TAO


def test_normal_trend_is_not_automatically_vetoed():
    assert not timing_vetoes(True, 15, 1, 1, 60, 60, 60, 1.5)
    assert not timing_vetoes(False, -15, -1, -1, 40, 40, 40, 1.5)


def test_short_is_exact_mirror():
    assert timing_vetoes(True, 51, 1, 1, 72, 81, 79, 1.77) == timing_vetoes(
        False, -51, -1, -1, 28, 19, 21, 1.77
    )
