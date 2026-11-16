# EXECUTION-AUDIT-02｜P0 成交可信度實證（2026-11-16）

研究用途、不下單、不修改 `mtf-bos-ob-retest-v1`，不修改原策略交易績效。正式 GitHub PR #6 保持 Draft、未合併 main。

## 第一關：來源一致

由 GitHub Actions Run `43820018876` 取得同一個凍結選幣與 1D/4H/1H 歷史 Artifact `multi-asset-input-43820018876`，以及個別 BTCUSDT、ETHUSDT、SOLUSDT 的正式固定日曆回測 Artifact。比對各幣種 `1h.csv` 的 SHA256 與凍結報告、選幣覆蓋表三方一致；任何不符即終止。只分析正式回測**已記錄的交易**，不增加或挑選策略條件。交付 ZIP 附帶實際執行產生的 1H 逐筆 CSV / JSON / Markdown。

## 第二關：成交時間代理與進場當根停損

原回測對 OB 區間的 wick-touch 採用整根 1H OHLC 判定是否觸及，並以該小時的開盤時間充當進場時間；`entry_time_utc` 不是觀測到的實際成交時間。對每筆模擬交易計算：

- 原始進場參考價是否介於該根 1H 的 high / low；不能將「在範圍內」當成已實際成交。
- 當根或兩根內的停損比例；開盤是否在參考價的進場方向外側。
- 若當根停損且開盤已在參考價另一側，單靠 OHLC 無法證明停損與參考價觸及的先後；明確標記不確定。
- 若開盤在進場方向外側、當根觸及停損，**在價格連續穿越且真實委託存在的條件下**，要到達停損價會先穿越進場參考價。這仍不代表實際成交、滑價或報價深度可行。
- 只用已記錄的交易會漏掉回測引擎在同根「OB 區間與止盈目標同時觸及」時取消的掛單候選；要評估完整成交機會，下一階段必須重建全部候選掛單。

### 已觀測 Test（BTC + ETH + SOL）

| 幣種 | Test 交易 | 當根停損 | 兩根 1H 內停損 | 參考價在 1H 範圍外 | 當根停損且開盤在參考價另一側 |
|---|---:|---:|---:|---:|---:|
| BTCUSDT | 36 | 12 | 20 | 0 | 0 |
| ETHUSDT | 44 | 14 | 18 | 0 | 0 |
| SOLUSDT | 43 | 10 | 12 | 0 | 0 |
| **合計** | **123** | **36** | **50** | **0** | **0** |

這仍是從已被反覆查看的 Test 做的描述性研究，不是全新樣本外驗證，也沒有證據可推論策略會變成正期望值。

## 第三關：可選的 Binance 1m 事後時間順序檢查

`execution_audit_02.py --minute-verify` 只對 Test 的每筆*進場時段*，讀取 Binance Spot 公開 API 的 60 根 1 分鐘 K 線（不使用 1m 做新交易訊號），先逐根驗證時間、OHLC，並要求 1m 加總與凍結 1H 完全相同。

- 1m 首次觸及參考價在首次停損前：`reference_minute_before_stop_minute`；反向為 `stop_first_observed_before_reference_minute`；同分鐘為 `same_1m_bar_order_unresolved`。
- 若缺少 1m、API 失敗或 OHLC 不吻合，一律保留 `not verified` 狀態，不拿它修正交易績效。
- 若停損先出現，不代表之後不會再次觸及停損；研究 CSV 保留「進場參考價分鐘後是否再觸及停損」欄位。
- 1m 不能提供 tick-level 先後、交易量可否填單、排隊優先權、當時買賣盤與實際成交價，絕不能宣稱真實成交率。
- 下載的每個 1m 時段存至 `1m_evidence/SYMBOL_ENTRYMS_1m.json` 並記錄 SHA256，方便重查。

GitHub `.github/workflows/execution-audit-02.yml` 會**重用**上述 `43820018876` 凍結 Artifact，不重新下載十幣種 1h/4h/1d 歷史、不執行正式策略回測；只針對三幣 Test 的最多 150 個進場時段做 1m 查核。工作流程單獨、限制權限只讀，不與十幣種基準回測共用取消佇列。

## 本地使用

```bash
python -m unittest -v test_execution_audit_02
python execution_audit_02.py \
  --history-zip MA02_FROZEN_INPUT_43820018876.zip \
  --report-zip MA02_BTC_43820018876.zip \
  --report-zip MA02_ETH_43820018876.zip \
  --report-zip MA02_SOL_43820018876.zip \
  --symbols BTCUSDT,ETHUSDT,SOLUSDT --output-dir research/execution_audit_02
# 在可連線 Binance 的環境添加 --minute-verify
```

## 不在此階段宣稱

不宣稱已修正策略勝率、不修改停損、止盈、OB/1H/4H/1D 訊號規則；不會把已知結果拿來事後挑選交易，不觸碰實盤 API Key、帳戶或委託。