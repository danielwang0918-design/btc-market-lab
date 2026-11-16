# ENTRY-QUALITY-01 — OB 回踩後的 1H 收盤確認與進場品質研究

**研究性工具，不是正式策略。** `mtf-bos-ob-retest-v1` 的 1D／4H／1H 結構、OB 回踩進場、原始停損／止盈、費用、滑價與 Stop-first 均保持不變。`main` 不更動，不合併 PR。

## 研究邊界與必要警告

- 輸入為已完成的 `BTCUSDT_trade_diagnostics.json`（原始 OB 停損方案）及同次 Run 的 `BTCUSDT_stop_loss_research.json`、1H K 線。
- **配對實驗只涵蓋原策略實際成交的交易**。改成等收盤確認，可能使其他原本被佔用持倉而未出現的訊號成為新交易；因此這不是從第一根 K 線重新產生候選的完整策略回測，不可以把配對結果當正式新勝率。
- A1：OB 接觸 K 線完成收盤後，若方向有利（多單 `close > open` 且 `close > 原進場參考價`；空單反向）且整根 K 線沒有突破原停損，於**下一根 1H 開盤**考慮進場，使用原停損／原止盈；扣除單邊手續費 10 bps、滑價 5 bps，預計淨 RR 仍須 ≥ 1.5。
- A2：在 A1 之上，須突破上一根已收盤 1H 的高／低點。下一根開盤才進場。這些是**預先列出且固定**的觀察性假設，不是已驗證交易規則。
- 所有未取得完整閉市資訊前的指標都不可使用。入場 K 線使用已完成收盤後才可知的價格來決策**下一根**開盤；從不回填當根最低／最高價格當作即時可知。
- 對多空停損與止盈同根觸發採保守 Stop-first；資料缺口發生時強制以缺口前已收盤價平倉。進場時間、出場時間、費用與收益以原模擬器相同方式計算。若延後進場導致淨 RR 不合格，則不進場。
- 不能用同一個 Test 的事後結果去選門檻，選出後宣稱「Test 已驗證」。後續真正回測需重新走完整訊號產生器、重新定義後持有全新樣本外期間，並比較交易次數、扣成本後期望值、PF、回撤及信賴區間。

## B：OB 形成品質（訊號產生時已可得）

`ob_confirmed_age_at_signal_hours`（OB 確認到訊號時的年齡）、`ob_body_fraction`（OB 形成 K 線實體／全振幅）、`prior_touch_episodes_asof`（OB 區域從確認到訊號前已完成的觸及段數）、`stop_distance_over_1h_atr14_asof`（原始停損距離／當時已收盤的 ATR14）。全部僅由**訊號時間已完成的 K 線**計算。預先固定分桶：年齡 ≤24h / 24–72h / >72h；觸及 0/1/2+；實體比 <0.5 / ≥0.5；停損／ATR ≤1 / 1–2 / >2。

**注意：** 訊號到「實際回踩」的延遲、回踩該根 K 線的 OHLC、出場結果只允許作事後觀察；絕不可用成訊號當時的進場過濾。

## 資料與執行

```bash
python -m unittest -v test_entry_quality_research test_entry_execution_audit
python entry_execution_audit.py --data-dir data/historical --output-dir data/historical
python entry_quality_research.py --data-dir data/historical --output-dir data/historical
```

輸出四檔：

- `BTCUSDT_entry_execution_audit.json`／`.csv`：確認成交時間是觸及 K 線的開盤**代理時間**；非逐筆成交時間。
- `BTCUSDT_entry_quality_research.json`：逐時段基準績效、固定分組、配對候選；含每個輸入檔 SHA256。
- `BTCUSDT_entry_quality_features.csv`：每筆交易的事前 OB 特徵、原始結果與兩類下一根開盤對照結果。

研究設定及其全部限制保留在 JSON 的 `fixed_assumptions`、`critical_limitations`。**請勿把 1H 內路徑不明的 K 線極值當作已成交後的可實現浮盈。**