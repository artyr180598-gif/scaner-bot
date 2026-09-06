# Forward-paper squeeze laboratory

Enabled by default; disable with `SQUEEZE_LAB_ENABLED=false`. Telegram `/lab`
is registered on the existing access-controlled router and command menu.
No real orders, trading alerts, or integration into production confidence scores.
Main signal strategy is unchanged. Data goes to a separate `squeeze_lab` table
in the existing SQLite database; restart persistence requires persistent storage
on Railway, just as the main signal journal does.

Fixed BTC/ETH/SOL/BNB/XRP/DOGE universe, chosen to match research, NOT a whole
exchange scan. Sequential exchange requests share the existing concurrency
limiter. Polling every 60 seconds plus request/processing duration; not instant.
Only closed 15m/1h/4h candles. Detect within 90 seconds after the 15m close.
Entry uses a newly fetched ask for LONG/bid for SHORT, rejects HTTP retrieval
over 5 seconds, excessive spread, inadequate turnover or displacement >0.25 ATR.
Ticker timestamps are local receipt times, not proof of exchange quote freshness.

Rules: EMA20 reclaim after pullback, RSI ranges 35–55 then 45–65 mirrored for
SHORT, directional scores >=25 on 1h/4h, 1h ADX >=20, BTC not opposing below
−25, signal range <=1.5 ATR, EMA distance <=1 ATR, prior four bars include
BB/KC <1. Stop at 18-bar extreme /1.45 ATR, 2R target, net RR >=1.8 using
6 bps per side. No averaging. Version `squeeze-reclaim-forward-v1` is distinct
from historical research: 260-bar rolling calculation, current spread/turnover
filters and actual observation delay can change accepted signals.

One virtual position per coin. Existing positions are reconciled from closed
1m candles before searching. Dedup survives restart. Entry-minute SL/TP touches
are censored because they may precede entry. Missing minute history is censored;
never inferred as a win or loss. Both barriers in one later minute: stop first.
Adverse stop gaps use worse open, losses are not clipped. Non-aligned 72-hour
timeout boundary is censored rather than using an unknowable boundary fill.
No minimum number of signals is forced.

Closed records contain model net and stress R with 6/12 bps per side and proxy
funding 1 bp/8h, not actual historical funding or measured execution. Ask/bid
entry plus cost allowance is deliberately conservative. Partial minute ambiguity
and censored trades create selection bias: do NOT assess profit from closed
records alone. `/lab` shows status counts, not a fabricated success probability.
No portfolio capital/leverage simulation; simultaneous correlated positions
remain a limitation. Runtime heartbeat key: `squeeze_lab_heartbeat`.

HTTP 401/403/418/429 surfaced to the laboratory pauses its task until restart;
it never changes endpoints or credentials. Existing shared HTTP client's retry
behavior is unchanged. Other data errors are logged and retried next cycle.

## Descriptive report

`/lab` now shows per-version open/closed/censored/invalid counts, profitable
closed count, total and mean net R, stress-cost total R, and maximum drawdown
of cumulative closed-trade R ordered by close time. This is not mark-to-market
portfolio drawdown or a probability forecast. Open and censored positions are
not assigned invented outcomes; their exclusion can bias all closed-only results.
Unknown versions are displayed separately, never pooled into current statistics.
