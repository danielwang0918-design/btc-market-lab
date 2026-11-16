# Pattern-as-of trade observation study

Research-only addition. Does **not** modify `mtf-bos-ob-retest-v1` entries, stops, targets, position sizing, cost assumptions, or test-set parameters.

Run after `stop_loss_research.py` and `trade_diagnostics.py`:

    python -u pattern_trade_study.py --data-dir data/historical --output-dir data/historical

Outputs:
- `BTCUSDT_pattern_trade_study.json`: train/validation/test summary per stop variant, pattern family and timeframe, original vs interior-only MFE audit, source SHA256 hashes and predeclared parameters.
- `BTCUSDT_pattern_trade_features.csv`: one row per recorded trade, with latest pre-decision confirmed Elliott/harmonic candidate for 1h/4h/1d plus separate path-ambiguity fields.
- `BTCUSDT_1h_pattern_candidates.csv`, `BTCUSDT_4h_pattern_candidates.csv`, `BTCUSDT_1d_pattern_candidates.csv`: event evidence and causal confirmation times.

Only patterns with `usable_from_open_time <= signal_time_utc` are eligible as **observed features**. A pattern is marked recent when its confirmation is within 168h of that decision timestamp (predeclared from original strategy's setup max age; not optimized on validation/test). No role in order placement.

The original MFE from `trade_diagnostics.py` can include full extremes of both entry and exit candles, even if they happened before actual fill or after actual exit. `mfe_r_interior_lower_bound` uses **only complete hourly candles strictly between** entry and exit hourly candles. It is a conservative lower bound, not a replacement for tick-level intrabar truth. When no complete interior candle exists, the lower bound is zero, **not** evidence that no favorable movement happened. If the full report exceeds +1R but the interior lower bound does not, the +1R observation remains ambiguous.

Source provenance is recorded with sha256 per K-line input and diagnostics JSON. Baseline results from the original artifact (`43734048475`, artifact ID `14169702604`) are in `NEXT_STAGE_REPORT_20261116.md`; different later downloads may change sample counts. Watch for multiple comparisons, overlapping candidate labels, low subgroup sizes and curve-fitting. No 80% win-rate claim.