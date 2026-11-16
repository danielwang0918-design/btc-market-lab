# P0-ORDER-STATE-05 — 1H 反向 BOS 前的 OB 掛單等待與價格距離

2026-11-16｜研究用途，原 `mtf-bos-ob-retest-v1` **完全不變**，不使用低於 1H 的 K 線做單，也不實際下單。

## 已凍結來源
- Binance Spot 1D、4H、1H 歷史資料與 SHA256：Multi-asset baseline Run `43820018876`。
- 原策略掛單接受／取消完整事件：P0-ORDER-STATE-01 Run `43827207508`。
- 取消第一個未通過條件的已收盤快照：P0-ORDER-STATE-03 Run `43831710147`。
- 94 筆首次反向 BOS，均是 1H 新確認方向：P0-ORDER-STATE-04 Run `43836895973`。
- 本階段只新增 `order_state_ob_distance.py` / `test_order_state_ob_distance.py` / `.github/workflows/order-state-ob-distance.yml`。

## 方法
- 使用每筆已接受掛單的 `setup_id` 中原 OB 形成 K 線的時間定位原始 1H OB Candle，取得當時高低價；驗證原交易方向與 ID 相同。
- 訊號接受價：標記「接受訊號」的 1H Candle 收盤價；僅在該 Candle 已收盤時可用。
- 反向 BOS **前一小時**：取消 K 線前一根完整 1H 收盤價；在該時點尚未看到取消 K 線的反向 BOS。
- BOS 確認時：取消 K 線已收盤後的收盤價；**不可倒灌回前一根 K 線**。
- OB 距離：收盤價與舊 OB 區間最近邊緣的絕對價格距離，若收盤位於區內則 0；bps 依收盤價格正規化。ATR14 為過去 14 根**完全收盤**1H True Range 的簡單平均，每根 TR 需要前一根收盤，並要求 15 根連續 K 線。
- 同樣計算另外 42 筆因 RR、無目標或無 OB 而取消的原有候選單，僅作描述性比較。差別不代表因果性。
- 逐筆核對既有 SHA256、94 筆 BOS 取消的精確 1H 新反向 BOS 事件、136 筆全部訊號取消／接受配對與時間戳。無缺漏、無重複才輸出報告。

## 限制與風險
- 這是**已檢視 Test** 的條件樣本。不能拿這次 ATR14 門檻、等待時間或 OB 距離反過來調原 Test，再宣稱勝率提升。
- ATR14 在這裡只用於歷史尺度描述，不是新的進場、停損、出場或取消決策。
- 取消單不能視為應成交的獲利；BTC/ETH/SOL 常同時受大盤影響，個案不是獨立隨機樣本。
- 本研究沒有評估任何替代策略，也不推論保留原掛單的反事實績效。

## 執行

```bash
python -m unittest -v
python order_state_ob_distance.py symbol BTCUSDT --data-dir data/multi_asset --stage03 research/order_state_05/stage03 --stage04 research/order_state_05/stage04 --replay-dir research/order_state_05/replay --output-dir research/order_state_05/individual
python order_state_ob_distance.py aggregate --input-dir research/order_state_05/individual --output-dir research/order_state_05/summary
```

GitHub Actions 透過已完成的凍結 Artifact 直接使用資料，**不重新下載全歷史、不查 1m/15m**。Run 成功且三幣種結果全齊時，才可稱「歷史事件重建稽核成功」。