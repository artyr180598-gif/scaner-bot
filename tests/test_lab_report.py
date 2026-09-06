from cryptopilot.lab_report import format_statistics, statistics


def row(net, time, **kwargs):
    return dict(
        version="v1", status="CLOSED", net_r=net, stress_r=net - 0.1, closed_ms=time, **kwargs
    )


def test_drawdown_uses_close_order_and_does_not_clip_losses():
    result = statistics([row(2, 3), row(-3, 2), row(1, 1)], "v1")
    assert result["drawdown_r"] == 3
    assert result["net_r"] == 0
    assert result["wins"] == 2


def test_censored_invalid_and_other_versions_never_become_wins():
    records = [
        row(2, 1),
        row(float("nan"), 2),
        dict(version="v1", status="CENSORED_GAP"),
        dict(version="v1", status="OPEN"),
        dict(version="old", status="CLOSED"),
    ]
    result = statistics(records, "v1")
    assert result["closed"] == result["wins"] == 1
    assert result["censored"] == result["invalid_closed"] == result["other_versions"] == 1
    assert result["open"] == 1


def test_empty_report_does_not_invent_success_rate():
    text = format_statistics([], "v1")
    assert "пока нет" in text
    assert "0%" not in text
