# P1-06B｜事前固定的改善候選研究

以 P1-06A `5d4235d0ac22677d7040bc5e510a3082defdca18` 建立獨立 `codex/p1-06b-stop-improvement`。所有正式模組、P0／Genesis、main及舊PR均只讀；候選僅為離線訊號／紙上研究，不含帳戶交易授權、槓桿操作或實盤委託。

事前規格與失敗測試先提交 `2df7d75`，再計算任何候選績效。`P1_06B_PROTOCOL.json` SHA256：`78c4d4b6ba4ef909880700619825781d6f5050f11abd18849dcee45d4f22c173`。只有六個候選，每個一套固定規則，無參數網格、沒有看完Test後重新調整。

## 基準、機會與比較順序

1. 核對P1-06A ZIP SHA256 `67fa9e644825bb849354cf315d6b9b69a6dd00e1578522a126668d4ead905723`，其中P1-05 ZIP SHA256 `828ec78907a33e30afdb5ae1d397e80b135a38e015fd8c7678a8d155ccc0d596`。逐檔核對原24個凍結來源、canonical tapes及既有程式，不下載細於1H資料。
2. 以P1-05 A的全部登記事件為共同母體，包括未成交。保留其首次合資格OB觸及、原本的target/zone歧義、缺口及候選失效防護。這是**條件於既有執行防護的母體**，不是所有市場OB；只改止損也不重新開啟先前被原規則取消的事件。
3. 先逐筆重現P1-05 A純多／假想多空各四個時段帳戶（同時訊號排序、數量、成本、退出、終值、回撤），再進行路徑可信度稽核。
4. 先做孤立事件比較：每個事件獨立以1000 USDT、風險預算2%及現金上限換算數量；不共享本金，允許事件重疊，不能稱為可同時交易或提供投資組合MDD。輸出全部機會含未交易0報酬，以及雙方均成交的配對子集，顯示樣本流失。
5. 之後才做完整帳戶：1000 USDT、全球單倉、每run至多1000次建倉、相同日曆截止、entry hour／symbol／setup ID因果排序，同小時不重新進場。預先產生結果只作歷史資料索引，**接受與排序不讀出場績效**；忙碌訊號不使用未來平倉資金計算新單。

## 六個固定候選

| 名稱 | 唯一主要改動 | 入場／SL／TP與失效 |
|---|---|---|
| E_CLOSE | 僅進場 | 原首次觸及後3根已收盤1H內，同向實體收在OB區外；下一根連續開盤。保留原SL與TP1；等待中原SL/TP已觸、缺口、零量、過期、跨界或實際入場淨RR<1.5均拒絕。 |
| E_BREAK_RETEST | 僅進場 | 觸及前最後200棒內已2L/2R確認的1H swing high（多）／low（空）；3棒內同向收盤新越過，再3棒內觸及該level且同向收回，下一根開盤。原SL/TP1及共同失效。這是微結構突破回踩代理，不宣稱完整通用MSS/CHOCH。 |
| S_SWING | 僅SL | 原入場；多SL取原OB與最新事前確認1H低點−0.1事前ATR的較遠者，空頭鏡像。無pivot/ATR拒絕，原TP1不縮短；重算數量、風險與RR，RR<1.5拒絕。 |
| S_ATR1 | 僅SL | 原入場，SL至少距原參考價1個事前1H ATR14，只擴不縮；原TP1不變，重算數量及淨RR≥1.5。ATR沿用P1-03已固定算法，不新選週期。 |
| F_4H | 僅環境 | 原A全規則加最後已收盤4H MA200與20棒MA斜率同向。無其他ATR/RVOL堆疊條件；波動、其他週期僅完整分層報告。 |
| I_SWEEP20 | 獨立訊號 | 過去20根連續已收盤1H的高／低（排除當根）被影線越過，當根同向實體收回；兩邊都掃拒絕。下一根連續開盤，SL在確認極值外0.1事前ATR，TP按5bps成本計精確2淨R，最多72h。無OB要求，不能與原A視為相同交易的因果比較。 |

對照A完全保留原P1-05價位及模擬成交慣例。所有受控候選不新增持有上限，沿用A原SL/TP、原完整性退出及日曆退出；這避免把E原先72h同時混進「僅改進場」。原TP2未定義仍留空。

獨立掃蕩為Spring/Upthrust/Turtle Soup概念啟發的**20小時收復代理**，不是原20日Turtle Soup、完整Wyckoff階段或真實流動性流向。沒有CVD、吸收或機構掃止損的證據宣稱。

## 同棒路徑：可行性而非捏造先後

採有限狀態圖，節點包含O/H/L/C、入場價、SL、TP；相鄰價位連續移動、必須到過H與L並終於C。狀態記錄是否已進場／退出及極值是否已訪問，列舉所有可行終態，不只挑O-H-L-C與O-L-H-C兩條假路徑。

這仍是**連續價格、參考價首次被觸及即成交**的條件模型，不包含棒內跳價、深度、排隊與部分成交。若多單O在入場價之上，到較低SL必然先穿過入場價，部分原先粗略標為不確定的同棒SL在此模型內其實不可避免。若O已在入場價之下，參考價觸及模型與真正掛限價可於開盤改善成交的解讀不同，另列數量，不宣稱所有105筆都能避開。

- `source_stop_first`：原研究慣例，稱中性參考而非機率中位數。
- `feasible_lower`／`feasible_upper`：每個事件在上述條件模型中可行終態的最小／最大淨損益，屬事後不確定性分析，不作訊號、接受門檻或排序。不同退出改變後續資金佔用，**各自帳戶情境不是真正全局投資組合上下界**。
- 缺口／零量時，沿用原模型下一個可觀察報價退出／估價並標記，有限的已觀察路徑範圍不能當成真實路徑上界。若終點無法可靠平倉，程式須停止並報告，不補造終點成交。

## 成本、風險與成功／失敗

費用單邊10bps、主滑價5bps。SL變更後，以含正常出入場成本的單位風險重新換算q，並取全額現金可支付數量；正常模型風險≤進場前淨值2%。15bps壓力情境按15bps重新預算正常停損風險，不刻意沿用5bps倉位超額。跳空仍可能超額，逐筆揭露。原P1-05嚴格Spot缺歷史最小交易量／數量步進，現階段仍無可認證真實Spot成交；假想空頭獨立，不包含借幣／融資真實成本。

主情境訊號資格按事前5bps成本固定。15bps壓力不重新以RR篩掉較差事件，保留同一合資格機會；數量、正常風險及顯示的cost_scenario_target_RR改按15bps計算，原5bps planned_net_RR另存。可用現金扣保留的1x抵押與費用，空頭賣出款不增加購買力。

歷史改善旗標需樣本≥30、SL率降低、勝率提高、平均淨R提高、PF不降、含退出成本的1H收盤MDD不升，均相對同模型、時段、路徑情境A。少交易、只降低SL率、縮短TP或惡化期望不成功。更嚴格的前瞻候選門檻另需全期與已開啟Test均正期望、PF>1、最終>1000、BTC與其他七幣均正，且15bps平均R不負；這也不是新OOS認證。

若無候選達標，明確回報0個已證明優勢候選；最多可將E_CLOSE／E_BREAK_RETEST／I_SWEEP20列為**已事前指定的訊號可觀測性假設收集**，不是依Test選出的獲利策略。所有結果、零組、拒絕原因、年度及Train／Validation／已開啟Test完整保留。

## 方法來源（2026-11-16查閱）

- Bailey、Borwein、López de Prado、Zhu，2017，[The Probability of Backtest Overfitting](https://escholarship.org/uc/item/4w1110bb)：多重嘗試與回測過度擬合的研究背景；本研究不藉此宣稱算出可靠PBO或獨立顯著性。
- 同文[作者公開2015年修訂稿](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)已查閱；校方書目頁可能有JavaScript驗證，未將無法讀取的頁面當成已讀全文。
- Carol Osler，[Stop-Loss Orders and Price Cascades in Currency Markets](https://www.newyorkfed.org/research/staff_reports/sr150.html)，FRBNY Staff Report 150／2005期刊版：外匯止損集中與價格連鎖的實證動機，不能外推為Binance掃蕩策略有效。
- StockCharts ChartSchool，[The Wyckoff Method: A Tutorial](https://chartschool.stockcharts.com/table-of-contents/market-analysis/wyckoff-analysis-articles/the-wyckoff-method-a-tutorial)：Spring／Upthrust在交易區間的概念。本站教學不是本批加密貨幣策略績效證據；本研究未重建完整Wyckoff階段。
- 原P0／P1-03／P1-06A已固定的2L/2R、BOS、OB、MA200與ATR是其餘候選的可重現實作來源。0.1ATR、三棒確認與1ATR保護屬本次事前研究假設，不宣稱為學術驗證的最佳值。

## 重現

`python -m unittest -v`；`python -u p1_stop_study.py --source-zip p1-06a-source.zip --out-dir results`。Artifact包含完整來源ZIP、候選／拒絕清單、每事件路徑結果、孤立事件及配對對照、完整帳戶交易／曲線、中文報告、測試及全部來源與程式SHA256。
