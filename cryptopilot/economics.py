"""Trade-plan arithmetic; costs are assumptions, never guarantees of fills."""

import math


def net_reward_risk(
    long: bool, entry: float, stop: float, target: float, one_way_cost_bps: float
) -> float:
    if not all(math.isfinite(x) for x in (entry, stop, target, one_way_cost_bps)):
        return 0.0
    if min(entry, stop, target) <= 0 or one_way_cost_bps < 0:
        return 0.0
    sign = 1 if long else -1
    risk = sign * (entry - stop)
    reward = sign * (target - entry)
    if risk <= 0 or reward <= 0:
        return 0.0
    fraction = one_way_cost_bps / 10000
    return max(0.0, (reward - (entry + target) * fraction) / (risk + (entry + stop) * fraction))
