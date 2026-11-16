# Permanent project scope (user instruction, 2026-11-16)

BTC Market Lab is a BTC / cryptocurrency **signal-analysis and quantitative-research system**, not an automated trading system.

- Never implement real order placement/cancellation/closing, exchange trading API access, account trading authorization, leverage operations, or automated live execution. Do not add unnecessary trade-management modules for feature completeness.
- Offline historical backtests, paper simulations, capital/risk accounting, win rate, net expectancy, Profit Factor, drawdown and research acceptance remain explicitly authorized. Simulation must never send orders.
- Decisions use only already closed 1H / 4H / 1D candles, causal as-of evidence and explicit timestamps. No future information or subhour decision feeds.
- Signal outputs should state long / short / watch, entry price/range, SL, supported TP1 / TP2, net cost-adjusted RR, evidence, required confirmations and invalidation. Missing evidence means **watch**; do not fabricate levels or confirmations.
- Research independent entry policies. Do not force every indicator to agree for all policies. A combined filter may be a separately labelled comparison, not the only permitted strategy.
- Evaluate win rate together with net expectancy. A higher win rate with worse expectancy or preserved capital solely from fewer trades is not a successful edge.
- Preserve formal `mtf-bos-ob-retest-v1`, P0 / Genesis, frozen data, main and existing PRs. Work in isolated research branches; never automatically merge Draft PRs.

These instructions apply to P1-05 and subsequent development. Prefer signal quality and backtest verification over execution features.
