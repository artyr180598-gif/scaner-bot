# First impulse radar — experimental protocol

This is an independently shortlisted 5-minute event detector, NOT a calibrated
trading strategy and NOT the recovered RID strategy. No entry/stop/size advice
or automated trade alerts are generated. Existing trading strategy is unchanged.

Button: `🔬 Старт импульса`; command: `/impulse`. Uses the existing liquidity
universe and scan lock; four concurrent symbol tasks. Fresh tickers are requested
after candle collection. Missing symbols or data failures cannot produce events.

Frozen initial hypothesis, 2026-09-05:
- 24 completed baseline candles, preceded by 24 older candles.
- Baseline high-low range <=80% of older high-low range.
- Event candle closes outside baseline, opens inside baseline.
- Event volume >=1.5 times baseline median volume.
- Event range <=2 baseline ATR (14 true ranges, no event-bar leakage).
- Directional candle body >=50% of its high-low range.
- Close and refreshed ticker remain beyond level but <=0.5 baseline ATR away.
- Latest completed candle must be less than five minutes old; complete contiguous
  history and finite valid OHLCV required. Identical mirrored short conditions.

The 5-minute close implies detection latency; scan runtime adds latency. Snapshot
REST data do not provide instant intrabar detection or guarantee executable prices.
Repeated manual scans may repeat the same event; no automatic monitoring added.

Validation completed: synthetic regressions for long/short, exclusion of future
bars, stale/missing data, absent volume expansion, overshoot, retracement, large
event candle and gap opening. Full local suite: 22 tests passed.

NOT completed: historical profitability, event precision/recall, market-regime
robustness, production load test or independent holdout. Do not assign a success
percentage. Do not present this as proven profitable or enable automated trading.
Next study should freeze costs/exits and test up to 6–12 recent months, preserving
an untouched chronological holdout and comparison with unfiltered breakouts.
