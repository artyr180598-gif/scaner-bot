import pytest

from cryptopilot.economics import net_reward_risk


def test_gross_two_r_is_not_net_two_r():
    assert net_reward_risk(True, 100, 99, 102, 0) == 2
    assert net_reward_risk(True, 100, 99, 102, 6) == pytest.approx(1.8788 / 1.1194)
    assert net_reward_risk(True, 100, 99, 102, 6) < 1.8


def test_worse_fill_and_more_cost_reduce_ratio():
    base = net_reward_risk(True, 100, 98, 104, 6)
    assert net_reward_risk(True, 100.1, 98, 104, 6) < base
    assert net_reward_risk(True, 100, 98, 104, 12) < base
    assert net_reward_risk(False, 100, 102, 96, 6) > 1.8


def test_invalid_plan_is_rejected():
    assert net_reward_risk(True, 100, 101, 102, 6) == 0
    assert net_reward_risk(True, float("nan"), 99, 102, 6) == 0
