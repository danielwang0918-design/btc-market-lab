# MULTI-ASSET-02 — 執行前資料稽核（2026-11-16）

來源：已成功完成的 GitHub Actions `Binance multi-asset historical coverage` Run [43813994484](https://github.com/danielwang0918-design/btc-market-lab/actions/runs/43813994484)，Artifact `binance-multi-asset-history-43813994484`；檔案內 `multi_asset_coverage.json` / `universe_selection.json` 及原始 Binance Spot 1H/4H/1D CSV。

## 統一 UTC 切分
- Train：觀測起點～2022-12-31 23:59 UTC
- Validation：2023-01-01～2024-12-31 23:59 UTC
- Test：2025-01-01～2026-11-15 23:59 UTC（排除 2026-11-16 當日尚未完整的日資料）
- 最少每段 1,000 根 1H 收盤 K 線，否則只列資料覆蓋、不計入策略績效；這是預先固定的資料門檻。

| 幣對 | Train 1H | Validation 1H | Test 1H | 回測納入資格 |
|---|---:|---:|---:|---|
| BTCUSDT | 46,981 | 17,543 | 16,416 | 符合 |
| ETHUSDT | 46,981 | 17,543 | 16,416 | 符合 |
| SOLUSDT | 20,927 | 17,543 | 16,416 | 符合 |
| ZECUSDT | 33,111 | 17,543 | 16,416 | 符合 |
| XRPUSDT | 40,777 | 17,543 | 16,416 | 符合 |
| BNBUSDT | 45,044 | 17,543 | 16,416 | 符合 |
| NEARUSDT | 19,392 | 17,543 | 16,416 | 符合 |
| DOGEUSDT | 30,569 | 17,543 | 16,416 | 符合 |
| ASTERUSDT | 0 | 0 | 9,732 | **不足：缺 Train／Validation** |
| NVDABUSDT | 0 | 0 | 3,774 | **不足：缺 Train／Validation** |

**符合日期資料門檻：8/10 幣對**。這還不是 8 個幣對的策略交易筆數，且不代表正期望值。以上筆數由 Artifact 內原始 1H 開盤時間實際逐列切分計算。

## 本次交付與驗證
- 專用 `multi_asset_baseline.py`：每個幣對重用正式 `historical_backtest` 的 `_prepare_snapshots` 和 `_simulate_slice`，不更動策略。
- `test_multi_asset_baseline.py`：9 項離線測試通過。
- 真實歷史 Artifact 上的 ASTER / NVDAB 資料門檻與 SHA256 驗證已實測成功，兩者確實被標為 `excluded_insufficient_calendar_coverage`。
- 透過 `.github/workflows/multi-asset-baseline.yml` 自動重新凍結選幣快照、下載歷史 K 線，最多同時回測三種幣，再輸出每幣 Test/Validation/Train 報表及描述性合併報表。

**尚未宣稱：** 8 個幣對的正式策略回測結果、勝率提升、正期望值、獨立有效樣本量。原有 BTC 的 Test 區間已被過往研究查看過，新一輪跨幣種 2025–2026 Test 不能視為全新未探索的未來資料。

**偏誤與未解決問題：** 本宇宙根據 2026-11-16 當日成交額選出，具有生存者與事後選幣偏誤；不同幣種同一天同步進場的交易不獨立；現貨 1H K 線無法確認 wick-touch 實際成交時序。不得把所有交易直接當作一套可執行的投資組合績效。