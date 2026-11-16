# P0-ORDER-STATE-06｜取消後原 OB 觸價與新確認 BOS 追蹤

研究日期：2026-11-16（台灣）；研究-only。正式交易規則 `mtf-bos-ob-retest-v1`、資料源、交易成本、進出場、停損和最低 RR 完全不變。**所有做單判斷只使用 1H／4H／1D 已收盤 K 線。** 本分析僅做後見事件描述，不使用 1m／5m／15m，不下單。

## 固定樣本與來源
- 同一批資料 Binance Multi-asset Run `43820018876` 的 `multi-asset-input` 1D／4H／1H CSV 與 coverage manifest。
- 原始取消事件 Run `43831710147`、原 1H 反向 BOS Run `43836895973`、OB 距離研究 Run `43838687913`。
- **研究母體固定**：BTC 23／9、ETH 36／17、SOL 35／16，合計 **94 筆 1H 反向 BOS 取消**及 **42 筆其他訊號改變而取消**。不是所有掛單取消，也不是新增交易。
- SHA256 逐幣驗證 1H／4H／1D、Stage03 取消明細、Stage04 BOS 事件，還需每幣 Stage05 個別彙總與總表逐字一致。固定 OB high/low 必須與當時原始形成的 1H K 線相同。

## 分析定義（事先固定，不依績效調整）
1. 觀察起點：取消所屬 1H K 線**收盤後的下一根 1H**。取消當根不當成未來。
2. 觀察期：**24／72／168 小時**，必須連續且完整落在先前已凍結的 Test 截止時間內。缺資料或尾端不足：`right_censored`，不算「未觸及」。
3. 觸及原 OB：未來任一完整 1H K 線的高低價與**原始** OB `[low,high]` 相交；另外記錄是否有 1H 收盤落在 OB，兩者不能混為「委託成交」。
4. 新確認原方向 1H BOS：原策略一致的 2 根左側、2 根右側嚴格 pivot 確認，之後由**已收盤 1H close**突破未曾被突破的 swing。重建的反向 BOS 必須與 Stage04 **94 筆確認時間、方向、突破參考價**吻合才可繼續。
5. 若 OB 觸及和新 BOS 發生於同根 1H，標示 `same_hour_order_unresolved`；不推測盤中先後，更不模擬買賣。
6. 與 42 筆其他原因取消相比只供描述；不做統計顯著性、因果解釋、篩選新最佳規則，也不把「原方向 BOS」誤認為完整 1H＋4H＋1D 對齊。

## 本地重現

```bash
python -m unittest -v test_order_state_post_cancel.py
python order_state_post_cancel.py symbol BTCUSDT --history input/history --stage03 input/stage03 --stage04 input/stage04 --stage05 input/stage05 --out results
python order_state_post_cancel.py symbol ETHUSDT --history input/history --stage03 input/stage03 --stage04 input/stage04 --stage05 input/stage05 --out results
python order_state_post_cancel.py symbol SOLUSDT --history input/history --stage03 input/stage03 --stage04 input/stage04 --stage05 input/stage05 --out results
python order_state_post_cancel.py aggregate --input results --out results
```

GitHub workflow `.github/workflows/order-state-post-cancel.yml` 透過 `gh run download` 取得**原有 Artifact**，不呼叫 Binance、不重跑十幣種回測；獨立跑三幣，合併時要求三組全部齊全。這次結果仍屬**舊 Test 的後見研究**，不可據此操作實盤。