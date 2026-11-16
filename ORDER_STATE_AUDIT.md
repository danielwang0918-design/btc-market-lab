# P0-ORDER-STATE-01｜只使用 1H、4H、1D 訊號的掛單狀態稽核

2026-11-16。研究-only；**不改交易規則、不使用低於 1H 的 K 線作出任何交易決策，也不進行真實下單。**

## 問題
現有 `historical_backtest._simulate_slice` 當掛單在同一根 1H K 線內觸及 OB 區與獲利目標時會保守取消。這樣避免以無法辨認的盤中走勢過度樂觀地判為成交，但同時也可能刪除實際先回踩 OB 再前往目標的交易。**只有 1H OHLC，不能從同一根 K 線判定 OB 和目標價先後**。現在不能將被取消的訊號直接算作成功交易。

## 本階段交付
- `order_state_audit.py`：逐一重播正式策略 1H 掛單狀態，記錄閉合 K 線訊號接受、掛單取消原因、OB wick-touch 的**模擬成交**、同根出場、資料缺口／資料完整性／切分結束等事件。
- 不更動 `historical_backtest.py`。稽核鏡像邏輯若與同次 GitHub Run `43820018876` 凍結的正式策略交易**任一筆不同**（setup、進出時間、原因、bars、R），即 FAIL CLOSED；不發績效結論。
- 同一根 1H 內同時觸及 OB 和目標價的三種標記：`target_already_at_open_before_zone_approach`、`zone_present_at_open_but_reference_fill_unproven`、`intrabar_target_vs_zone_order_unknown`。這些標籤是研究描述，不是交易訊號。
- `test_order_state_audit.py`：涵蓋長短方向、時序、缺口、取消、已消耗候選、停損、時區與稽核彙整的離線測試。
- GitHub workflow：自動在研究分支的稽核器首次提交時執行；**不重抓整段歷史，也不下載 1m / 15m**，沿用 Run `43820018876` 的同批 1H/4H/1D 凍結資料與三幣種逐筆基準。

## 使用方式

```bash
python -m unittest -v
python order_state_audit.py BTCUSDT --data-dir data/multi_asset --baseline-dir research/order_state_01/source --output-dir research/order_state_01/per_symbol
python order_state_audit.py ETHUSDT --data-dir data/multi_asset --baseline-dir research/order_state_01/source --output-dir research/order_state_01/per_symbol
python order_state_audit.py SOLUSDT --data-dir data/multi_asset --baseline-dir research/order_state_01/source --output-dir research/order_state_01/per_symbol
python order_state_audit.py aggregate --data-dir research/order_state_01/per_symbol --output-dir research/order_state_01/summary
```

## 方法限制與驗收
1. 所有決策訊號僅來自已收盤的 1D、4H、1H；**不允許 15m、5m、1m 做單**。1m 即使曾用於先前獨立的事後研究，本稽核也完全不讀取。
2. 1H OHLC 中 OB 的影線觸及，不代表實際在保守參考價成功成交；比對的是模型行為，不是委託簿成交真相。
3. 因 P0 會分析 2025 年以後的舊 Test，不當作獨立的樣本外證據；不增加勝率最佳化條件。
4. BTC／ETH／SOL 的本次對照基準分別 36、44、43 筆 Test；完整 10 幣種報告的合格幣種共 8 種、331 筆 Test，已有相關性與事後選幣偏差。
5. 完整執行至 GitHub Actions 聚合報告出來之前，不宣稱掛單失效的實際數量或修正後勝率。