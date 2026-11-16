# P1-07｜研究規格與優先順序（績效計算前固定）

以P1-06B PR #19 `df203cca3adbb37b11118c1725cf36cbc9fffe41`另建`codex/p1-07-signal-quality`。原A、S_ATR1及六舊候選完全凍結，不重新開發／重跑舊研究；正式策略、P0／Genesis、main與舊PR只讀。遵守AGENTS.md，無真實委託、交易所授權或槓桿執行。

來源為Run43902320256／Artifact14242585512，ZIP SHA256 `2b416875856defb20e714d366d3e4b61a22283cac8432e0f11131fdab00ece00`。先提交本規格與失敗測試，再計算新訊號績效；不等待逐項參數確認。

## 優先一：P1-07A，僅讀封存結果拆解機制

使用全部4583原A登記、1090可評估事件，不只446個已成交交易。精確分解零含入每機會PnL差＝共同成交效果＋僅A被排除效果＋僅S新增效果；共同成交再拆固定qA的報價／成本差與重新q項。成本／正常風險比例及R分母改變不等於方向預測改善；持有期是事後結果，不當入場特徵。全部模型、時段、成本路徑、BTC／其餘七幣／各幣完整輸出。不得新增退出、搜尋ATR倍數或把反事實加總當共享本金。

## 優先二：P1-07B，可行性篩選後只測兩個獨立假設

選**真正20日價位失敗收復**與**同價位突破延續**，共用全部首次突破母體。Wyckoff先保留可行性：目前沒有凍結的range／phase／secondary-test標籤，單根影線不足以宣稱完整Spring/Upthrust，不額外開第三策略。

每個UTC日只用之前已收盤且連續的20根1D高低，排除當日；極值重複時取最近一次出現日，需至少4個UTC日前。這是crypto24/7日的原版20日概念改編，不是20小時I_SWEEP20。每幣、日、上／下邊界只登記第一個已收盤1H突破，保留age不足、雙邊同棒、缺口及沒有收復的事件。

- **T20_CONT**：首次突破1H收在level外，下一根連續1H開盤順向；開盤仍須在level外。
- **T20_FAIL**：當日第一次已收盤1H收回level內，下一根連續開盤反向；未收復也保留，開盤仍須在level內。可與延續臂在同事件不同時間各自形成訊號，不能等知道會失敗才取消原延續訊號。

SL使用**截至確認時已知的當日累積極值**，外加0.1個確認前1H ATR14；不能偷用當日最終1D高低。當日已觀察1H前綴缺口／零量、下一根缺棒／零量、開盤越SL、跨當日／資料切段界線則不成交。TP固定為主5bps成本後精確2R，最多持有72h；不沿用原版盤中ticks入場、移動SL或重入。僅重用既有資金、路徑及SL/TP/時間/日曆/缺口退出工具，不加入P0 BOS候選或其歷史窗口退出條件。

先每事件1000USDT、2%預算的孤立兩臂及零含入／共同成交比較，再做真正共享本金、全球單倉、最多1000筆、相同日曆的帳戶。兩臂成交時點不同且共同成交條件於後來收復，不宣稱單因素因果改善；孤立重疊事件不能當同時可執行交易或計其投資組合MDD。

## 優先三：P1-07C，先資料稽核再最小Meta-Labeling

R3-01／02／03在本地、已取遠端研究樹及GitHub搜尋尚未取得，不冒稱已讀或擅自補造其協議。先查所有4583機會、1090合資格、3493缺經濟標籤；未成交／未觸及不是虧損。每事件只取一個source_stop_first紙上標籤，另外的路徑情境不是新訓練樣本。

僅在Train可用標籤≥200且正負各≥30、退出嚴格早於2023分界時，執行**條件式純多紙上Meta-Labeling**。四個已知特徵為SL/ATR、入場淨RR、正常風險成本占比、已知等待時數log1p；所有補值／標準化只fit Train。Logistic Regression固定C=1、liblinear、random_state=0、max_iter1000、無class_weight，接受閾值0.5；不調參、不用Validation/Test校準。對照讀取封存accept-all以及預先固定成本占風險≤25%的新篩選；不重跑原策略。

未來路徑／缺口不確定標籤只能排除於訓練或可信分類評分，**不能用來剔除當時原本不知道的入場機會**。預測與新篩選帳戶需保留這些機會並另報不確定性。若資料門檻或as-of欄位不足，輸出具體缺口，不訓練大型網路或自行放寬標準。

## 統一成本、帳戶與判定

1000USDT、正常模型風險≤入場前淨值2%、全球單倉；費用10bps/side、滑價5與15bps，15bps按15bps重新換算q。真實歷史最小委託／深度／借幣規則缺失，僅條件式純多與獨立假想多空。共同日曆2020-10-15～2026-11-16 UTC；Train至2023、Validation至2025，其後已開啟Test，分段帳戶獨立起始。既有可行悲觀／樂觀／原stop-first慣例保留，不捏造棒內概率或真實成交。

全部候選列機會、成交、拒絕、SL率、淨虧損率、勝率、淨R、PF、PnL、MDD、在市、BTC／其他七幣／各幣／各時段及四情境。相對改善需n≥30、SL下降、勝率及淨R提高、PF不降且MDD不升；正期望前瞻資格還需全期與既有Test正、PF>1、終值>1000、BTC／其他七幣正及15bps不負。即使達標仍非新OOS，必須規格固定後全新前瞻資料驗證。未達標回報0，不改標準。

## 理論來源與可用性

- Connors Group[官方Street Smarts章節書目](https://store.tradingmarkets.com/products/street-smarts-immediate-download-selected-by-technical-analysis-of-stocks-and-commodities-magazine-as-one-of-the-classics-for-trading-books-written-in-the-20th-century)；[可讀原書OCR鏡像第12–13頁](https://djvu.online/file/mRdyd29nGPERu)非作者主機。原版20日、極值至少4sessions、盤中stop入場、當時當日極值SL及trailing／重入；本案next-open、2R、72h必須明列改編。
- Wyckoff機構[range／測試與Upthrust教學](https://www.wyckoffstockmarketinstitute.com/wyckoff_articles/Selling/selling_tests_upthrusts-2.htm)：尚缺完整凍結標註，本輪不宣稱完整Wyckoff或吸收／CVD。
- 已讀P1-06B方法與防過擬合文獻；知名理論只是動機，不是Binance勝率或獲利證據。
