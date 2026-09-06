from scripts.indicator_robustness import paired_day_difference


def test_identical_variants_have_zero_difference():
    rows = [{"time": "2026-06-01T10:00:00", "net_r": 2.0}]
    result = paired_day_difference(rows, rows)
    assert result["ci95_r_per_active_day"] == [0.0, 0.0]


def test_missing_candidate_day_is_zero_not_discarded():
    rows = [{"time": "2026-06-01T10:00:00", "net_r": 2.0}]
    result = paired_day_difference(rows, [])
    assert result["mean_difference_r_per_active_day"] == -2.0
    assert result["ci95_r_per_active_day"] == [-2.0, -2.0]


def test_empty_comparison_is_not_positive_evidence():
    assert paired_day_difference([], [])["ci95_r_per_active_day"] is None
