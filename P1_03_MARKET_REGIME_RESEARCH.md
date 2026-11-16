# P1-03｜MA200 × ATR 波動環境與 K 線型態的獨立量化研究

**2026-11-16｜BTC Market Lab｜研究專用，未批准交易策略**

## 固定研究來源

- 八幣種：BTC、ETH、SOL、ZEC、XRP、BNB、NEAR、DOGE 的 Binance Spot USDT。
- 使用 `43820018876` 封存的 **已收盤 1H／4H／1D** OHLCV，24 份原始 CSV 全部經 P0 前瞻鎖定檔 SHA256 與 P1-01 原始 Manifest 雙重核對。
- 唯一型態事件與原 24／72／168 小時固定觀察結果沿用 P1-01 GitHub 成功 Run `43852683913`；固定事件 CSV 與 Manifest SHA256，**不使用後續 Codex 修訂產物回填歷史結果**。
- `mtf-bos-ob-retest-v1` 既有策略、PR #6、前瞻 Genesis、`main` 不修改。本研究不下單。

## 因果性市場狀態定義：絕不使用型態當根與其後資料

決定市場狀態的截點固定在**型態第一根 K 線開盤之前**；在該時間點以前已收盤的同週期 K 線才有資格計入 MA／ATR。所有 rolling 指標遇資料斷裂必須重新累積，並將歷史不足與分布不足保留為明確分類。

### MA200 趨勢類別

- 以型態開始前 200 根完整 K 棒的收盤均價作 `MA200`，和前 20 根時點的 MA200 比較。
- **多頭 `bullish`**：前一根收盤高於 MA200 且 MA200 的 20 根斜率為正。
- **空頭 `bearish`**：前一根收盤低於 MA200 且斜率為負。
- **過渡／盤整 `transition_or_range`**：其他可判定情況。
- **歷史不足 `insufficient_prior_ma`**：無 220 根連續歷史、無有效前置 ATR 或不可核對的窗口。
- 另記錄 `MA200 斜率／ATR14`、`前收盤距 MA200／ATR14`，僅供研究比較，不是新進出場門檻。

### ATR 相對波動類別

- `ATR14` 使用 Wilder 前導 True Range 定義的簡單 14 根均值（**非 Wilder 指數平滑 ATR**）；所有資料在型態開始前。
- 將 `ATR14／同根收盤價` 與此前完整連續 **200 根**同定義 ATR 比率比較，使用包含最新觀察值的 mid-rank 百分位，平手給中間名次，避免完全相等的樣本被錯判高波動。
- 低波動 `low`：百分位 ≤ 25%；高波動 `high`：百分位 ≥ 75%；其餘為 `normal`。
- 缺少完整 ATR14 歷史／200 根比率分布則為 `insufficient_prior_atr_distribution`，不轉成「低波動」。

以上數值是**本版固定描述性定義**，不是已驗證交易最佳參數；不准依既有 2025–26 Test 的績效再調整。

## 初步真實歷史資料計數

- P1-01 唯一型態事件 **106,155**，固定 24／72／168h 觀察 **318,465** 列；其中多個期限、幣種／時框存在統計依賴，不可當獨立成交。
- MA200：bullish **39,228**、bearish **39,961**、transition_or_range **20,686**、insufficient_prior_ma **6,280**。
- ATR：low **29,104**、normal **46,055**、high **24,876**、insufficient_prior_atr_distribution **6,120**。
- 各幣種 × 各週期 × 型態 × Train／Validation／已開啟 Test × 固定持有期 × MA200 × ATR 組別全部保留，未挑單一最佳組別。

## 交付與測試標準

- `p1_market_regime.py`：離線 SHA256 驗證、因果 MA／ATR、缺棒處理、唯一事件鍵與三期限統計。
- `test_p1_market_regime.py`：27 項獨立 regression tests：高低趨勢、波動分布、缺棒重置、因果性、未完成觀察與範圍限制等。
- `p1_03_regime_evidence.csv`：每個型態**只一列**，包含型態之前可用資料的數值與分類。
- `p1_03_regime_summary.csv`：每個原始幣種、時框、型態、分段、觀察期限、趨勢及波動組別的評估／截尾與紙上結果，分類依賴不可當獨立抽樣。
- `p1_03_regime_counts.csv`、`p1_03_manifest.json`：狀態總表與封存來源 SHA256。
- GitHub Workflow 會跑 repo 全測試、從既有兩個成功 Run 取回資料、重新產生全部結果並驗證固定總數。

## 限制（不可省略）

1. 本階段分類**不是**正式訊號，且回報舊有 Test 僅具探索性，不能為新策略宣稱真正樣本外正期望值。
2. P1-01 原始固定期限紙上報酬不是停損／止盈／流動性／可執行的完整交易績效；**現貨模擬做空無法直接在現貨市場照單執行**。
3. 相同型態存在跨時框、同幣種多時段及三觀察期限重疊；統計不獨立，不能把 318,465 列稱為成交筆數。
4. 不能從這份資料挑選 MA200 斜率、ATR 百分位或型態的最佳組合，再把 2025–26 Test 稱作未見新資料。
5. 後續若提出候選規則，必須在新版本固定後才使用未來資料驗證，並且不改動既有 P0 前瞻監控的固定核心策略。

### 執行

```bash
python -m unittest -v test_p1_market_regime
python p1_market_regime.py --frozen /path/to/frozen-input --p1-artifact /path/to/p1-01-artifact --lock PROSPECTIVE_LOCK_V1.json --out-dir research/p1_03/results
```