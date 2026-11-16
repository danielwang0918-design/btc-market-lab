# P0-ORDER-STATE-04｜94 筆 BOS 不一致的首次變化週期

2026-11-16。**只研究 1H／4H／1D 已收盤 K 線的原策略狀態**；1m、5m、15m 不用於做單、研究判斷或輸入本稽核。研究-only，不登入交易帳戶、不下單、不修改 `mtf-bos-ob-retest-v1`、不改動 PR #6 所指向的 `main`。

## 原始已證實基準

- 完成的 10 幣種固定規則 Run `43820018876`：8 個具備足夠歷史的合格幣種，Test 331 筆；原策略合併觀察平均 R 為負。
- 已完成 `P0-ORDER-STATE-01` Run `43827207508`：BTC／ETH／SOL Test 519 個接受訊號，123 筆模擬成交，396 筆取消（不是錯失的 396 筆獲利）。
- 已完成 `P0-ORDER-STATE-03` Run `43831710147`：136 個 `pending_signal_changed_cancel`，其中 **94 個**被原策略「多週期 BOS 方向不一致」規則拒絕（BTC 23、ETH 36、SOL 35）。其餘 42 筆原因另已分類，本研究不修改。

## 本階段檢驗內容

1. 載入三批原始 GitHub Artifact，核對 `multi_asset_coverage.json`、原始 1H／4H／1D K 線、上一階段交易狀態與取消原因的 SHA256，並把 94 個取消逐筆對回 136 個原始掛單生命週期。
2. **只採用原策略 `_strategy_events` 所計算的 BOS**：確認左右兩根的 swing、結構突破、原始有效證據與 200 根 K 線視窗，再依「已收盤」的 UTC 時刻逐小時重建當時三週期的最後確認 BOS 方向。
3. 以接受訊號時的多週期一致方向為基準，記錄第一個改變的週期、是否同時多個週期、距接受訊號多少小時；另列取消當下各週期最終方向。
4. 嚴格把**新反向 BOS**與**舊 BOS 因資料窗口滾動而暫無可見 BOS**分開；若混合狀態重建失敗、來源不同、或首次不一致早於原來取消，就停止且不發績效結論。
5. 逐幣輸出 `*_bos_timeline.json`、每筆 `*_bos_timeline.csv` 和按小時的 `*_bos_path.csv`，三幣均通過才生成 `bos_timeline_summary.json`／`.md`。

## 界線及研究解讀

- 這裡的「第一個 BOS 不一致」是原策略的**時間序列描述**，不是損益因果關係或替代策略。
- 取消掛單沒有已驗證成交，不可當成被漏掉的贏單。
- 2025–2026 的 Test 已反覆研究，不能把發現的新規則回頭套用同一 Test 然後聲稱獨立驗證。
- 如要調整掛單持有策略，必須在假設事前固定後另取得未參與調整的未來驗證期，並評估成交現實與資金風險。

## 使用方式

```bash
python -m unittest -v
python order_state_bos_timeline.py analyze BTCUSDT \
  --data-dir data/multi_asset \
  --replay-dir research/order_state_04/replay \
  --causes-dir research/order_state_04/causes \
  --baseline-dir research/order_state_04/baseline \
  --output-dir research/order_state_04/results
python order_state_bos_timeline.py aggregate --results-dir research/order_state_04/results
```

GitHub Actions：`.github/workflows/order-state-bos-timeline.yml`，只在研究程式更新時啟動，重用三批凍結資料，**不重新下載 K 線、也不重跑八幣種正式策略**。