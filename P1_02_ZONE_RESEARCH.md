# P1-02｜K 線型態與高週期支撐／壓力區的獨立研究

2026-11-16，BTC Market Lab，**歷史觀察研究**，不得將已開啟之 Test 當作新的獨立樣本外結果。

## 已使用的事前固定資料與研究底座

- `mtf-bos-ob-retest-v1` 正式進出場完全不改。本模組也不執行實盤或設定訂單。
- 歷史 Binance Spot 8 幣種 BTC/ETH/SOL/ZEC/XRP/BNB/NEAR/DOGE × 1H/4H/1D，來源 GitHub Run `43820018876`；每份 CSV 需符合 P0 前瞻鎖定協議的 SHA256。
- 型態事件與固定觀察報酬來自 P1-01 成功驗證 Run `43852683913`，`p1_01_events.csv` 固定 SHA256，並核對來源 manifest。P1-01 Codex 審查日後若改變判定方法，應另開版本，不得悄悄覆蓋此批觀察。
- 型態：看漲／看跌吞噬、晨星、三兵、三隻烏鴉，固定 24／72／168h 對應的 P1-01 紙上方向、扣成本後報酬與不完整標記；**不是實際成交交易**。

## P1-02 客觀位置定義

1. 1H 型態只看 **型態第一根開盤前已收完**的 4H；4H 型態只看前一日的 1D；1D 型態只看更早確認的 1D；不使用盤中未完成 4H/1D 的完整 high/low。
2. 支撐點：2 根左側／2 根右側嚴格確認的波段低點；壓力點對稱為波段高點。價格相同的高低點不任意視為兩個「確定」的 pivot。pivot **確認 K 線必須在整個型態開始前就已收盤**，而不是事後完成再貼標籤。
3. 只考慮過去 **180 日**且同一個連續歷史區段的 pivot。若高週期應收盤的一根資料缺失，標記 `reference_data_gap_or_stale`，不偷偷採用較早的資料。
4. 以型態之前的同週期 `ATR14` 作距離單位；取距型態最低價（看漲）／最高價（看跌）最近的既有 pivot 作錨點。
5. 若距錨點不超過 **0.75 ATR**，且錨點周圍 **±0.5 ATR** 內至少有 **2 個**早已確認的 pivot，標記 `near_confirmed_zone`。
6. 若距離滿足但只有一個 pivot，標記 `near_single_pivot_unconfirmed`；超過距離則 `far_from_nearest_pivot`。無足夠 ATR 或既有 pivot 者保留為不確定／無可用參考，不能歸類為「遠離」。
7. 這些值是明確指定的**第一版描述性幾何設定**，不是已驗證的最佳交易參數，絕不能因看完 2025–2026 Test 再調到想要的方向。

## 交付內容與驗收

- `p1_zone_context.py`：獨立的固定來源驗證、跨週期對齊、pivot 分群、型態分層及報表；不 import / 修改 P1-01 或正式策略。
- `test_p1_zone_context.py`：時序、缺棒、未收盤高週期、未完成 pivot、原始證據篡改、分母／分層等測試。
- `research/p1_02/p1_02_zone_evidence.csv`：**每個唯一型態**一列，區域分類可回溯。
- `research/p1_02/p1_02_zone_summary.csv`：保留所有幣種 × 週期 × 型態 × 原始日曆分段 × 觀察期限 × 位置分類。
- `research/p1_02/p1_02_near_vs_far_by_stratum.csv`：全部組別近區／遠區雙邊描述性比較，包括沒有足夠案例的空值。
- `research/p1_02/p1_02_descriptive_by_frame.csv`：分開 1H／4H／1D，避免混合不同週期時誤導。
- `research/p1_02/p1_02_manifest.json`：記錄來源 digest、參數與總數。

## 不足與研究紀律

- 波段 pivot 被確認後才可使用；一個支撐區有兩次 pivot 仍**不等於**預測上漲或成交掛單真相。
- 原 P1-01 固定期限紙上報酬已計入固定雙邊成本，但無逐筆保證成交／停損／資金使用模型，且模擬現貨做空不能直接在現貨交易所執行。
- 同幣種、不同週期、不同型態與三個期限高度相關；樣本筆數不能等同獨立試驗。
- 僅描述舊的 Train／Validation／已開啟 Test。不能在此基礎上選一個最好參數後向外宣稱「已驗證正期望」。未來須對候選規則另立固定版本，**先登記、再前瞻驗證**。
- 前瞻封存 A 線持續獨立，不因本研究而改動 Genesis、來源與交易規則。

## 使用方式

```bash
python -m unittest -v test_p1_zone_context
python p1_zone_context.py --frozen path/to/multi-asset-input --p1-artifact path/to/p1-01-study --lock PROSPECTIVE_LOCK_V1.json --out-dir research/p1_02
```

`--frozen` 與 `--p1-artifact` 均可提供 GitHub Actions 下載後的資料夾，或本地 zip；資料來源一律必須符合已固定的 SHA256。