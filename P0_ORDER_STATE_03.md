# P0-ORDER-STATE-03｜1H／4H／1D 取消掛單快照原因研究

本階段不更動 `mtf-bos-ob-retest-v1` 進出場、停損、目標、成本、時間框架與既有回測，亦無帳戶登入或下單。研究對象限定 BTC、ETH、SOL 在 **2025-01-01 UTC 起** Test 區間的原策略 `pending_signal_changed_cancel` 事件：分別 32、53、51 筆，共 136 筆。

## 與上一階段不同
- 上階段只證明 136 個掛單在等待期間「新快照不再與原 OB 相同」。
- 本階段在原本接受訊號的 1H K 線**收盤後**、以及發生取消的 1H K 線**收盤後**，重建當時原規則可見的 1D／4H／1H 結構與候選訊號。
- 重建使用原正式回測程式內的 `_strategy_events`、`_make_windows` 與 `market_research.build_analysis_from_records`，只運算需要的時間點，不重新回測所有 K 線或調參。
- 重新取得的「接受訊號」若不是與舊掛單**完全相同的 OB setup ID**，或「取消後候選」反而仍是相同的 OB setup ID，直接失敗；這避免用不一致的資料編造解釋。
- 原訊號失效分類是該小時**收盤之後**的第一個不符合條件；僅供描述，不能當成單一因果因素、漏掉的獲利或新的出入場規則。

## 資料與防護
- 只採用前次已成功的 `43820018876` 同批凍結 1D／4H／1H K 線、逐幣正式基準，以及 `43827207508` 完整重播 CSV／JSON，核對 SHA256。
- 重新核對全部原始 1H／4H／1D CSV 的 SHA256，不只信任 coverage.json 的雜湊；提供逐幣 baseline 時也要求各週期與 baseline 雜湊完全一致。
- 使用固定的 10bps/side 手續費、5bps/side 滑價、最低淨 RR1.5、OB 最長 168h，沒有更動任何參數。
- 不讀取 1m、5m、15m，不連接交易帳戶，**只用已收盤 1H、4H、1D 進行交易規則分析**。
- 結果分為多週期 BOS 不一致、無新 OB、OB 已回踩、目標波段不存在、RR 不足、收盤價穿越原止盈／停損、更新 OB 等互斥的原規則首個拒絕項目。未知原因會記為 unclassified，不會自行猜測。
- Test 已經反覆觀察，不能當作新的獨立樣本外證據；不可用這 136 筆直接挑選有利的新參數。

## 操作

```bash
python -m unittest -v
python order_state_cause_audit.py causes BTCUSDT --data-dir data/multi_asset --replay-dir research/order_state_03/replay --baseline-dir research/order_state_03/baseline --output-dir research/order_state_03/results
python order_state_cause_audit.py aggregate --output-dir research/order_state_03/results
```

已建 GitHub Actions `.github/workflows/order-state-cause-audit.yml`，在對應研究程式更新時獨立執行，不啟動耗時的十幣種策略基準重算。

## 尚未完成的實證門檻
直到 GitHub Actions 產生**全部三幣種的已核對快照原因檔**，不得把 136 筆細分類的結果宣稱已完成；任何一幣的快照與原受理／取消事件不一致則整個跨幣結論 FAIL CLOSED。