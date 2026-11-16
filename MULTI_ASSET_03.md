# MULTI-ASSET-03｜跨幣種穩健性與樣本相依性稽核

## 研究狀態

- 建立於 2026-11-16，**只新增研究層**，並未更動 `mtf-bos-ob-retest-v1` 的訊號、停損、止盈、成本、交易週期或下單能力。
- 多幣種歷史基準回測 `MULTI-ASSET-02` 的 GitHub Actions Run `43818214949` 在開發本工具時仍在執行，**尚無八幣種實際績效數值**。本工具只有在收到完整、可校驗的上階段報表和逐筆交易 CSV 後，才會產生統計；不得填寫假結果。
- 此階段不做調參，不比較新的進場策略，也不能把已反覆查看的 Test 稱為新的獨立樣本外驗證。

## 目的：回答四個問題

1. **跨幣種一致性**：表面上合併的平均 R 是否被單一幣種主導？輸出各幣種 Train、Validation、Test 的交易數、勝率、單筆平均 R、PF。
2. **時間穩定性**：Test 是只在某一年有效，或不同曆年表現不同？以交易進場 UTC 年份分組。
3. **相關性與重疊**：幣種是否同週出現訊號，降低獨立樣本量？統計同週參與的不同幣種數、交易最多幣種的占比。`1 / sum(symbol_trade_share^2)` 僅是幣種集中度代理，**不是實際有效獨立樣本數**。
4. **不穩定性**：同一幣種是否一拿掉，合併結果就反轉？分別做 leave-one-symbol-out，不參與調參。對 Validation 和 Test 做 UTC 曆週共同重抽樣、固定 seed 20261116、2000 次、每次把**所有幣種同一週的交易一起抽**，包含沒有交易的曆週，呈現描述性勝率及平均 R 95% 分位區間。

## 執行

上階段完成且產生以下兩個檔案後：

```bash
python -m unittest -v test_multi_asset_robustness
python multi_asset_robustness.py \
  --baseline research/multi_asset/summary/multi_asset_fixed_calendar_baseline.json \
  --trades research/multi_asset/summary/multi_asset_fixed_calendar_trades.csv \
  --output-dir research/multi_asset/robustness
```

輸出 `multi_asset_robustness.json` 以及 `multi_asset_robustness.md`，包含輸入 SHA256、每一拆分期的績效與所有失效原因。若基準報告不完整、幣種缺漏、交易時序超出固定 UTC 期間、結果檔交易數/勝數與實際交易不一致、重複 setup、NaN/Infinity、缺乏時區，則拒絕產出結論。

## 嚴格限制

- **生存者偏差**：此批幣種按 2026 年當時的成交量選出，不能回推成 2017 年實際可買到、可賣空、流動性充足的完整歷史投資組合。
- **OHLC 路徑**：價格觸及 1H OB 區並不等於真的成交，現有成交時序仍需以更細的成交資料或不同成交假設另做稽核。
- **同方向相關性**：UTC 週聚類重抽樣只保留週內同時發生的交易，仍無法解決跨週市場持續性、共通因素、策略多重試驗和事後選幣偏差。
- **合併 R 非資金曲線**：沒有資金權重、持倉重疊限制、流動性與保證金分配，不應報告為可執行投資組合。
- **不能拿 Test 挑最佳幣種**：Test 已反覆被分析；必須重新宣告未來觀察期間，才有真正新證據。對任何含不足 100 筆 Test 的績效都應視為探索性，但即使 >=100 也不能忽略相依性。
- **不保證 80% 勝率**：所有區間與結果都是已觀察歷史的描述性穩健性稽核。

GitHub PR #6 保留 Draft；本階段僅新增獨立模組與測試，不更動正在執行的 `MULTI-ASSET-02` 工作流程，以免取消已開始的長時間回測。