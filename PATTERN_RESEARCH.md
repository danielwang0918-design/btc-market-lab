# Causal Elliott / harmonic candidate annotations (research only)

This module is separate from the formal mtf-bos-ob-retest-v1 backtest. It does not modify entries, stops, targets, sizing, or live trading. No win-rate improvement is asserted.

## Scope and definitions
- Timeframes: 1h, 4h and 1d only. The strategy's main frame remains 4H.
- Elliott-style: confirmed 0-1-2-3-4-5 impulse-shaped candidates; wave 2 must stay inside wave 1 origin, wave 3 not shortest, wave 4 cannot overlap wave 1, and wave 5 must extend wave 3. These candidates are NOT complete Elliott wave counts or proofs of a nested impulse.
- Harmonic: confirmed XABCD candidate subsets for Gartley, Bat, Butterfly and Crab; B/XA, C/AB, D/XA and CD/BC ratios. Fixed default Fibonacci absolute-ratio tolerance 0.04. D is confirmed, not forecast; a tradable Potential Reversal Zone is not being asserted.
- Pivots: strict extremes using 2 left and 2 fully closed right candles. Consecutive same-kind confirmed pivots are compressed online to the more extreme. Ambiguous outside-bars with both extreme flags are excluded.
- Availability: a candidate is formed at its final swing, confirmed after the 2 right-side closed candles, and first usable at next candle open. Do not join on formation timestamps.
- Integrity: do not cross missing candle intervals or source changes. Invalid OHLC/timestamps/provenance fail closed.
- Evidence: each CSV row records source, version, timestamps, pivot prices and observed Fibonacci ratios. Labels are never live orders.

## Run on previously downloaded historical BTCUSDT candles
    python pattern_research.py data/historical/BTCUSDT_1h.csv data/historical/BTCUSDT_1h_pattern_candidates.csv
    python pattern_research.py data/historical/BTCUSDT_4h.csv data/historical/BTCUSDT_4h_pattern_candidates.csv
    python pattern_research.py data/historical/BTCUSDT_1d.csv data/historical/BTCUSDT_1d_pattern_candidates.csv

Output is observational; it does NOT enter the existing backtest. Freeze source data, parameters and version before evaluating.

## Validation gates
1. Complete existing MFE/MAE and 1D/4H trend diagnosis first.
2. Join candidates to trades ONLY when usable_from_open_time <= entry-open milliseconds; otherwise mark missing/unknown.
3. Compare unique-trade counts, win rate, expectancy after costs, profit factor, maximum drawdown, and confidence intervals separately across train/validation/test, timeframes and regime.
4. Correct for multiple comparisons and dependent/overlapping labels; keep a new forward-held-out period or walk-forward test untouched.
5. Never promote a pattern to a trading filter on small samples or retrospective pictures. An 80% win-rate target does not constitute evidence.

Wave conventions: https://www.elliottwave.com/waveopedia/impulse/
Harmonic conventions (definitions vary by author): https://research.titanfx.com/technical-analysis/fibonacci/what-is-harmonic-patterns
