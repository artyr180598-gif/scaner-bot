# RID screenshot hypotheses: initial rejection, 2026-09-09

The screenshots suggest two testable hypotheses, not knowledge of RID's private
rules: a compressed base breakout, and a quiet pause in an established trend.
Both were implemented separately from production RID entry authorization.

## Method

Existing Binance 15m candle archives, March-August 2026. Parameters fixed before
this run; July-August reported separately, not used to tune parameters. LONG only.
Next candle open, structural stop, 2R target, maximum 24-hour hold, stop first on
ambiguous candles, adverse gap fills, combined fee/slippage assumption 10 bps per
side. One position per symbol across both hypotheses. No averaging. Incomplete
future windows and discontinuous data are excluded. R is initial price risk.

This is NOT a production backtest: no funding, historical spread/order-book,
cross-exchange confirmation, market-wide selection or production exit policy.
The July-August subset is a fixed diagnostic split, not proof of independent
validation of all earlier project decisions. The price archives were already
present locally; data authenticity was not independently re-audited in this run.

## Results: trades / net R

| Symbol | Base full | Pause full | Base Jul-Aug | Pause Jul-Aug |
|---|---:|---:|---:|---:|
| BTC | 13 / -4.703 | 11 / -8.606 | 2 / +0.247 | 5 / -5.399 |
| ETH | 24 / -13.402 | 10 / -2.409 | 8 / -4.869 | 3 / -2.499 |
| SOL | 21 / -6.430 | 7 / -1.578 | 11 / -2.493 | 2 / +3.111 |
| XRP | 15 / +3.747 | 6 / -4.157 | 4 / -2.848 | 4 / -1.898 |
| LINK | 13 / +1.253 | 12 / -2.824 | 5 / +5.256 | 3 / -3.732 |
| NEAR | 25 / -8.232 | 7 / +0.880 | 7 / -5.167 | 4 / -1.659 |
| ADA | 23 / -4.916 | 8 / -6.868 | 7 / -2.614 | 2 / -2.528 |
| AVAX | 17 / -9.849 | 10 / -3.622 | 7 / -5.991 | 4 / -5.238 |

## Decision

Do not enable these rules for trading. Isolated positive subsets are too small
and inconsistent. Research observations require an explicit opt-in setting,
default false, have no trade plan and cannot enter automatic notification or
production paper calibration. Do not claim profitability or a recovered RID
indicator. PRIME, the main scanner and early radar are unchanged.

Replay: `python -m scripts.rid_hypothesis_replay CACHE SYMBOL [SYMBOL ...]`.
The next meaningful experiment needs independent coin-selection features and
time-stamped pre-entry flow data, not threshold tuning until these winners fit.
