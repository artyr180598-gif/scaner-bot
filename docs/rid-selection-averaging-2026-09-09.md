# RID: selection and bounded averaging research

Decision: no production change. No tested combination justifies live enablement.
Only RID research scripts/tests/docs changed in this iteration.

## Selection ablation

March-August 2026, existing Binance 15m archives. Seven tradeable test symbols:
ETH/SOL/XRP/LINK/NEAR/ADA/AVAX, BTC benchmark only. Identical underlying screenshot
hypotheses and structural-stop/2R/24h exits, next-open execution, 10 bps per side.
Every variant requires the same aligned eight-symbol data coverage. Filters act
before entry, and each arm independently tracks position occupancy. No future
bars used for selection. No market-wide historical universe or survivorship audit.

Relative strength: positive last-4h return, above BTC, top two of the eight-asset
panel. Turnover: last-1h quote turnover >=1.5 times mean hourly turnover during
the preceding 24h. Combined requires both. Thresholds fixed before this run.

| Variant | Trades | Net R | Mean R | Positive trades |
|---|---:|---:|---:|---:|
| No selection filter | 198 | -58.406 | -0.295 | 32.83% |
| Relative strength | 77 | -9.745 | -0.127 | 37.66% |
| Turnover | 9 | +0.830 | +0.092 | 44.44% |
| Both | 6 | -1.567 | -0.261 | 33.33% |

Relative strength was +2.622R in March-June but -12.368R in July-August.
Turnover's apparent edge rests on just nine trades, including two winning trades
in July-August. This is not enough for promotion. July-August was already seen
in previous research and must not be described as untouched out-of-sample data.

## Bounded averaging counterfactual

Same 198 baseline entry opportunities, different EXIT policy: TP +3% from average
fill, hard stop -9% from first entry, 24h maximum. Compare no add, one equal-coin
quantity add at initial price -3%, two at -3%/-6%. These are explicit experimental
assumptions, NOT recovered RID rules and NOT unlimited martingale. Price percent,
not leveraged ROI. Maximum planned cash loss (including costs) normalized equally
across policies by reducing initial quantity when adding is allowed.

| Policy | Net R, 10bps/side | Positive trades | Net R, 15bps/side |
|---|---:|---:|---:|
| No adds | -1.836 | 48.48% | -3.951 |
| One add | -0.303 | 52.53% | -1.872 |
| Two adds | -0.422 | 53.03% | -1.754 |

The combined-selection six-entry subset with one/two adds yielded +0.050/+0.042R
at 10bps, erased to approximately -0.001R at 15bps. Neither size nor robustness
supports a profitability claim. Higher win rate alone is not an edge.

Stop is checked before target; crossed resting adds are charged on stop candles.
No same-candle favorable add-then-target assumption. Gap stops may exceed -1R.
No funding or actual historical bid/ask, order book, leverage/liquidation model.
Counterfactual opportunities may overlap because their holding periods differ;
this is not an executable portfolio backtest. Do not compare total R between the
structural-stop selection study and fixed-9% averaging study as if the risk
definition and exit policy were the same. Original RID production is not replayed.

## Reproduction

```
python -m scripts.rid_selection_study CACHE_PARENT
python -m scripts.rid_averaging_study CACHE_PARENT 10
python -m scripts.rid_averaging_study CACHE_PARENT 15
```

Archives expected in `research-cache-v31` and `frozen-transfer-cache`, as specified
by the loader. Unit tests cover closed-history invariance, aligned coverage,
fixed risk normalization, gap losses and ambiguous add/target ordering.
