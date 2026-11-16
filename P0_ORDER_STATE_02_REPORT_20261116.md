# P0-ORDER-STATE-02｜只用 1H／4H／1D 的掛單閉環驗收

日期：2026-11-16（台灣時間）

**研究來源：** GitHub Actions Run [43827207508](https://github.com/danielwang0918-design/btc-market-lab/actions/runs/43827207508) 的三個完整 Replay Artifacts（BTC/ETH/SOL）及聚合 Artifact；正式基準 Run [43820018876](https://github.com/danielwang0918-design/btc-market-lab/actions/runs/43820018876)。兩個 Run 均已完成成功。本報告來自具名、逐筆可驗證的 CSV/JSON，未加入新的交易規則或參數。

## P0 閉環資料的稽核結果（Test，2025-01-01 UTC 起）

| 幣種 | 接受訊號 | 模擬成交 | 未成交取消 | 未結 | 同根 OB+目標價取消 |
|---|---:|---:|---:|---:|---:|
| BTCUSDT | 153 | 36 | 117 | 0 | 1 |
| ETHUSDT | 188 | 44 | 144 | 0 | 0 |
| SOLUSDT | 178 | 43 | 135 | 0 | 0 |
| **三幣種合計** | **519** | **123** | **396** | **0** | **1** |

取消原因（Test）：

- **259 筆**：`pending_target_without_zone_cancel`，當根觸及目標價，但不觸及 OB。
- **136 筆**：`pending_signal_changed_cancel`，等待期間，下一個已收盤快照的有效訊號不再匹配。
- **1 筆**：`pending_target_and_zone_same_hour_cancel`，同 1H 同時觸及 OB 與目標；時序不明，保守取消。分類為 `intrabar_target_vs_zone_order_unknown`，不能當成已成交或獲利。

**逐筆閉環已核對：每個 accepted 訊號都有且只有一個 terminal；每個完成模擬成交都與凍結正式回測逐筆配對。** 對照：Train = 1,173 訊號／281 模擬成交／892 取消；Validation = 520／108／412；Test = 519／123／396。三段皆無未結掛單。

## 多幣種績效穩健性（8 幣種）

來源：同一固定曆期基準 Run 43820018876 的彙總 Artifact，透過既有 `multi_asset_robustness.py` 做 UTC 週聚類 2,000 次重抽樣（種子 20261116）。

- Test 331 筆，62 勝，勝率 18.7311%，平均期望值 -0.348098R，Profit Factor 0.571671。
- 2025 年 183 筆，期望值 -0.372669R；2026 年 148 筆，期望值 -0.317716R。
- 94 個週有交易，其中 **77 週出現多幣種交易**；不可假設這 331 筆全部獨立。
- 排除任一幣種後的平均 R **全為負值**（最接近 0 也約 -0.301R）。
- 描述性 UTC 週聚類 Bootstrap：平均 R 的 95% 重抽樣區間約 [-0.516, -0.159]R。**不是**獨立樣本外預測區間。
- 目前選幣使用事後「最新成交額」有生存者偏差；Test 亦經反覆查看，不再當作全新樣本外驗證。

## 驗收方式與防止誤報

`order_state_evidence_gate.py` 只讀取 `order_state_audit.py` 已產生的三幣種 JSON/CSV，不讀取或查詢任何低於 1H 的 K 線，不呼叫交易所 API，也不下單。它會：

1. 驗證三個來源的稽核版本、1H／4H／1D 決策週期、正式策略未變、無真實成交證據。
2. 按照 Split + Setup ID 驗證 accepted 後恰好一個 pending 結果（模擬成交／取消／未結），發現遺漏、重複、未知狀態便失敗。
3. 確認 CSV 與 JSON 的訊號數、成交數、取消數與同根時間分類一致。
4. 對「同根 OB＋目標價」不推算獲利，也不修改原本同根取消的保守策略。
5. 對 1H 參考價的 wick-touch **僅**稱為「模擬成交」，不宣稱掛單真的成交。

本工具在真實凍結輸出上完成閉環驗收，並已通過 13 項合成資料防呆測試。

## 下一關建議

**P0-ORDER-STATE-03：為何大多數訊號還沒回踩 OB 就失效？** 不使用 <1H 資料；針對已收盤 1H 的掛單存續、取消理由、1D/4H/1H 結構轉換時間與距離，先做非參數化、研究性描述。**不以被取消訊號假想出場獲利**；後續若提出新規則，須使用真正新的 forward 資料驗證。正式 `mtf-bos-ob-retest-v1`、PR Draft 與 `main` 均不變。