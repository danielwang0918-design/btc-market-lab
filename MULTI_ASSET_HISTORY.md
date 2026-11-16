# MULTI-ASSET-01 | Binance Spot USDT historical universe and credibility gate

**Purpose.** Expand BTC-only *observations* to verified Binance Spot USDT instruments while preserving existing formal strategy `mtf-bos-ob-retest-v1` and its order, target, stop, fee and slippage settings. This is data acquisition and integrity audit, **not** evidence of a higher win rate or any trading advice. Only public market-data API is used: no API keys, account access or orders.

## How it operates

- Calls Binance public market-data-only `/api/v3/exchangeInfo`, `/api/v3/time`, and `/api/v3/ticker/24hr` to select currently trading USDT Spot pairs ranked by **current** 24-hour USDT quote volume (default 10, configurable 1–50). Filters stablecoin/fiat bases and leveraged token suffixes. Explicit comma-separated Spot USDT symbols override the ranking, and validation rejects inactive/unlisted pairs. This is **not** a historically reconstructed universe.
- Downloads **all available closed** Binance Spot `1d`, `4h`, `1h` candles via the repository's existing paginated `historical_backtest.download_to_csv` implementation. Requests start on `2017-08-17` but each instrument has its own *first observed* date. It is incorrect to infer that the first observed date is necessarily its actual listing date.
- CSVs remain separate as `data/multi_asset/{SYMBOL}_{INTERVAL}.csv`. No gap filling; invalid timestamps, duplicates, out-of-order data, unclosed bars and source mismatches are reported or rejected. Records UTC first/last observed dates, gap counts, SHA-256 and source for each CSV.
- A versioned `universe_selection.json` freezes the chosen symbols and selection timestamp; the resulting `multi_asset_coverage.json` captures per-symbol outcomes and partial failures. Use `--universe-file` to reuse a frozen choice in another run. CLI supports `--symbols` for any active Binance Spot USDT pair, e.g. `ETHUSDT,SOLUSDT,XRPUSDT,LINKUSDT,AVAXUSDT`.
- A GitHub Actions PR check downloads the default ten highest-volume eligible pairs and uploads the full dataset and audit report as a 14-day artifact. Run the workflow with `workflow_dispatch` to request a named list or different top-N. Binance is polled **when the workflow runs**; this document does not assert what pairs are most liquid now.

## CLI

```
python multi_asset_history.py --top-volume 10 --data-dir data/multi_asset
python multi_asset_history.py --symbols BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT --data-dir data/multi_asset
python multi_asset_history.py --universe-file data/multi_asset/universe_selection.json --data-dir data/multi_asset
python multi_asset_history.py --top-volume 20 --coverage-only
python -m unittest -v
```

## Why more K-lines do not guarantee a valid strategy

BTC history in the 2026-11-16 diagnostic artifact spans about 2017-08-17 through 2026-11-16 with **80,945 1H**, **20,252 4H**, and **3,378 1D** bars. The original OB variant produced **162 historical trades (104 train, 22 validation, 36 test)**. This is low effective trade sample for high-confidence win-rate claims. Cross-coin BTC/ETH/SOL price movements often co-move, hence five simultaneous coin trades can behave like fewer than five independent statistical samples.

## Required second gate before cross-coin performance claims

1. Apply the **same absolute UTC calendar cutoffs** across assets, with instrument-specific valid listing/observation start and delisting awareness. Do not recompute arbitrary `60/20/20` per-coin date cuts and mix their metrics as one test.
2. **As-of universe selection** is needed for an unbiased historical universe. A list selected by 2026 current liquidity suffers survivorship and look-ahead bias for 2018/2021 backtests; delisted and inactive assets are absent. Until that is fixed, treat the selected universe as retrospective exploratory only or use it for new prospective walk-forward observations.
3. Predeclare any entry-quality hypothesis, cost/slippage assumptions, and per-symbol liquidity thresholds. Correlated/coincident trades must be grouped by calendar/time block when computing uncertainty. Avoid using the same held-out Test repeatedly to pick filters.
4. Audit the existing OB wick-touch intrabar fill path, zero-observable fill timestamps and hourly MFE/MAE exit boundary. Current Binance Spot prices do not prove short-selling fills or Binance derivatives costs. Do not combine spot/futures mechanics without a separate model.
5. Only after these gates, run the unchanged baseline independently on each asset, report per-asset Train/Validation/Test counts, Expectancy, Profit Factor, max drawdown and cluster-aware confidence intervals. Show results **even if zero trades**. Never call the sum of correlated trades an independent sample size.

## References

- Binance public-only market data: https://github.com/binance/binance-spot-api-docs/blob/master/faqs/market_data_only.md
- Binance Spot REST API: https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md