# MULTI-ASSET-02 — Fixed-calendar formal strategy baseline

**Research-only / 2026-11-16.** This stage does not change `mtf-bos-ob-retest-v1`, does not merge PR #6, does not place orders, and does not introduce a new indicator or optimize on the Test period.

## Why
BTC has ~80k historical 1H candles but the formal strategy produces only dozens of out-of-sample trades. The prior multi-asset download successfully audited ten currently liquid Binance Spot USDT pairs. Historical market prices are not the same as independent strategy signals. A cross-asset baseline can reveal generalization failures, but selecting today's high-volume symbols retroactively introduces survivorship/hindsight universe bias.

## Deterministic method
- Reuses `historical_backtest._prepare_snapshots` and `historical_backtest._simulate_slice` **unchanged**, including 1D/4H/1H confirmed swing/BOS/OB logic, a 1H OB wick-touch entry proxy, an OB-edge stop, nearest confirmed swing target, SL first when SL and TP share a candle, 10 bps/side fee, 5 bps/side slippage, minimum net RR 1.5, and 168-hour max setup age.
- **Global UTC calendar** for all symbols: Train ends **2023-01-01T00:00Z**, Validation ends **2025-01-01T00:00Z**, Test ends at the *last complete UTC day* before frozen selection timestamp. No per-symbol fractional splits and no trades may carry between periods.
- Eligibility: at least **1,000 observed 1H candles per segment** in Train, Validation and Test; short-lived instruments remain reported but are not added to the performance sample. This is a data coverage gate, NOT an optimized filter. Subgroup trading sample sizes are reported separately.
- Every per-symbol source dataset must match the archived `multi_asset_coverage.json` SHA256 values. Failed and missing results cause a partial-failure aggregate instead of silently excluding assets.
- Reports: each symbol's metrics, fixed UTC intervals, entry/exit times and source hashes, pooled **observational only** R-based summaries, unadjusted Wilson interval, and number of overlapping Test entry days (a simple correlation warning, NOT a dependency correction).

## How to run locally with previously downloaded Binance K-lines

```bash
python -m unittest -v
python multi_asset_baseline.py symbol BTCUSDT --data-dir data/multi_asset --output-dir research/multi_asset/individual
python multi_asset_baseline.py symbol ETHUSDT --data-dir data/multi_asset --output-dir research/multi_asset/individual
# Repeat for each symbol in data/multi_asset/universe_selection.json, including short-history assets.
python multi_asset_baseline.py aggregate --coverage data/multi_asset/multi_asset_coverage.json --results-dir research/multi_asset/individual --output-dir research/multi_asset/summary
```

The dedicated `.github/workflows/multi-asset-baseline.yml` automatically freezes and downloads a Binance public-market universe, runs symbols in parallel, and publishes aggregated JSON/Markdown/CSV reports. `workflow_dispatch` supports `top_n` or a user-supplied comma-separated symbol list. Requires no exchange account or API key.

## Interpretation and limitations
- **The current volume-selected universe is not a point-in-time historical universe.** All results are exploratory until historical listings/delistings, point-in-time liquidity and a new prospective sample are accounted for.
- Trade outcomes may be synchronized because BTC, ETH, BNB and altcoins are correlated. Pooled trades are not independent Bernoulli trials, the naive Wilson intervals are not valid simultaneous or dependence-adjusted confidence limits, and pooled R does not include concurrent-position capital constraints: **not a portfolio equity curve**.
- Instrument classification needs a further universe policy: ticker-based rankings can include tokenized/nonstandard instruments. The policy must be fixed without inspecting returns; short history alone is not proof of asset type.
- OHLC-based wick-touch execution lacks real order fill timing, spreads, exchange-specific slippage, futures funding or borrow costs. Those are separate P0 execution research obligations.
- **Do not promote a winning coin or indicator based on this report.** Predeclare future hypotheses and validate on a genuinely new forward period. Do not interpret small-sample 80% win rates as evidence.