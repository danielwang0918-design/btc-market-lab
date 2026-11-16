# Codex 任務：P1-01 加密貨幣 K 線型態獨立研究

Repo: `danielwang0918-design/btc-market-lab`
Base: `codex/p0-integrated-mtf-backtest`（Draft PR #6 保持未合併）
Research branch: `codex/p1-01-candlesticks`

## 優先工作

1. Review `p1_candlestick_research.py`, `test_p1_candlestick_research.py`, `P1_01_CANDLESTICK_RESEARCH.md` and P1-01 GitHub Action; run all tests.
2. Check the exact Binance Spot SHA256 locked 1H/4H/1D 8-symbol dataset and reproducibility from Run `43820018876`.
3. Find and fix potential research methodology bugs (time-based duration math, data gaps, split boundaries, fee and slippage calculations, duplicate/correlated signal interpretation). Make focused PR commits with tests.
4. Review no-gap crypto candlestick definitions and propose **new versions**, not silent rewrites; preserve all previous reported outputs and research hashes.
5. Propose P1-02 support/resistance *as a separate experimental module*, never a change to `mtf-bos-ob-retest-v1` without a new verification proposal.

## Non-negotiables

- ONLY closed 1h, 4h, 1d bars for trading decisions; no subhour decision inputs.
- Research only, no real orders, no credentials, no leverage assumptions. Spot short performance is hypothetical.
- No optimization using previously viewed 2025–2026 Test results and no cherry-picking winner patterns/horizons. Do not claim video 82%/79% as Binance trade win rates.
- Do not alter `main` or merge Draft PR #6. Keep P0-07/08 forward hash chain untouched.
- Preserve Train/Validation/Exploratory-Test chronological boundaries; report data gaps/censored horizon outcomes.
- Provide all tests and exact evidence artifacts; call out any unresolved methodology issues.