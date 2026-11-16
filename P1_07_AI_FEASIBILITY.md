# P1-07C｜AI訊號品質／Meta-Labeling可行性

本階段遵守AGENTS.md，僅做資料稽核與一次事前固定的簡單分類研究；沒有大型神經網路、實盤API、交易授權、自動開平倉或槓桿執行。原P1-06B六個候選不重跑，原策略及所有來源保持不變。

## 研究方向文件可用性

依2026-11-17（Asia/Taipei）查核，工作樹及當時已取得的所有遠端refs，以`git grep`搜尋R3-01／R3-02／R3-03、Meta-Labeling／Logistic／AI研究文字無匹配；遠端branch清單沒有R3命名分支。`origin/p3-unified-research-pipeline`亦已讀取檔案清單並搜尋，沒有對應文件。另兩個遠端stage1／stage2研究分支只查tree檔名，未看到R3相關檔名；不將這視為全面全文查核。GitHub issues R3-01與PR R3搜尋亦無結果。

因此R3方向的原始文件目前**未取得**，不冒稱已讀或補造其規格。本次以使用者明定的Meta-Labeling方向與已提交P1-07 protocol為依據，來源缺口不阻塞可重現的第一批研究。

## 原始母體、標籤與時間

固定P1-06B Artifact ZIP SHA256：`2b416875856defb20e714d366d3e4b61a22283cac8432e0f11131fdab00ece00`。來源是全部A登記事件，**不是446筆已成交交易**。

| 範圍 | 機會母體 | eligible紙上結果 | 已知且無路徑／缺口旗標 | 未成交不可標為虧損 |
|---|---:|---:|---:|---:|
| 全多空方向 | 4583 | 1090 | 1087 | 3493 |
| 現貨可用long方向 | 2251 | 525 | 522 | 1726 |

只使用`policy=A, scenario=source_stop_first`的單一孤立結果；其他情境及S_ATR1不能重複增加標籤筆數。1090列中2列gap／zero-volume路徑未知、1列path-dependent，標籤留空。未到入場階段的3493個事件沒有等價經濟結果，不能當成loss或stop。

| long範圍 | eligible | 可信條件紙上標籤 | 正／負 |
|---|---:|---:|---:|
| Train | 204 | 201 | 46／155 |
| Validation | 168 | 168 | 36／132 |
| 已開啟Test | 153 | 153 | 26／127 |

訓練201列的label終點均嚴格早於2023 cutoff，達到事前≥200且各類≥30的最低操作門檻，但只是勉強通過，不是統計可靠性證明。來源label未跨split，不代表同段內獨立：long已知522列有146個事件與其他label重疊、97組重疊配對、最大同時4個；多空合計1087列有427個重疊事件、326配對、最大同時6個。不能隨機切分或以iid計顯著性。

## 固定LR，不按結果改規格

先依P1-07 protocol固定，才執行一次：

- 僅long eligible A，Train已知label且終點嚴格<2023；不依是否在單倉帳戶中已成交選樣。
- y為固定成本後紙上淨PnL>0；stop標籤另存，與淨盈利不能混稱。
- 四個numeric：SL距離／事前ATR、入場淨RR、正常停損風險的成本占比、log1p已知登記等待時間。
- SimpleImputer中位數、StandardScaler均只fit Train；Logistic Regression C=1、liblinear、random_state=0、max_iter=1000、class_weight=None、固定門檻0.5。
- 固定規則對照為事前正常風險成本占比≤0.25，另列accept-all；無門檻或C值搜尋。
- 所有525個long eligible都輸出預測。未來才知道的gap／path品質旗標只用於排除不可信訓練／分類標籤，**不作預測或資金模擬的拒單條件**。輸入特徵時間晚於進場則fail closed。
- Train分數只作樣本內回代診斷；全Train模型在2023 cutoff之前尚未訓練完成，ML帳戶一律觀望，只有其後才可依固定分數與閾值准入。固定成本對照無訓練時間限制。

本地固定依賴：numpy2.3.5、scipy1.17.0、scikit-learn1.8.0；模型JSON記錄中位數、標準化均值／尺度、係數、截距、版本及訓練筆數，毋須pickle。CI數字與版本以Artifact為準。

## 第一批結果與正確解讀

固定LR對525個機會全部給分，機率範圍0.053872～0.339394；**門檻0.5接受0／525**。固定成本規則接受266／525（Train131／Validation74／Test61，含3個未來品質未知事件）；不可因其未知標籤事後剔除入場。

| LR可信標籤分類 | Train | Validation | 已開啟Test |
|---|---:|---:|---:|
| AUC | 0.577139 | 0.552609 | 0.579346 |
| accuracy | 77.11% | 78.57% | 83.01% |
| 預測接受 | 0 | 0 | 0 |
| profitable recall | 0% | 0% | 0% |

此accuracy來自全數預測為不盈利，不是策略優勢；零交易保留1000 USDT不能稱正期望。AUC僅弱的探索性排序資訊，沒有新OOS或獨立樣本顯著性。完整帳戶結果由主研究以封存路徑、費用及倉位引擎模擬兩個新gate，不重跑原A。

分類目標是盈利機率，不直接最大化平均淨R；不同事件RR與收益幅度不同。這是目標限制，不是看到全拒絕後把門檻改成0.2或依Test挑cutoff的理由。固定成本gate也不能只按提高勝率驗收，仍須三目標、PF、MDD、曝險及跨幣／split共同判讀。

## 可用與不足的資料

足以做一次小型**conditional paper** LR可行性實驗，不能籠統說完全無法估計。仍不足以建立可信真實成交AI或完整母體的登記時點預測：

1. 未成交事件缺少統一登記時點的完整MA／ATR特徵與等價入出場label；本次525 long模型只適用於原A eligible入場階段。
2. eligibility本身沿用原OHLC執行防護與整棒觸價選樣，不能宣稱完整市場事件無選擇偏差。
3. source status／anchor_status、最終拒絕、全部exit、PnL／R、已實現費用、gap／path旗標、倉位／淨值及ID均禁止入模，避免未來與帳戶選樣洩漏。輸出join ID僅用對帳。
4. 缺口、深度、排隊、歷史最小交易量與借幣資格未取得；條件紙上label不是精確可成交結果。
5. 2025–2026 Test已反覆查看；任何歷史模型選擇與篩選只屬探索，須規格鎖定後全新前瞻資料。

建議先完善所有候選登記時點特徵快照、明確的可用時間與label品質，以及新的前瞻事件日誌。可保留固定LR作簡單基準，但本次無法以全拒絕結果認證AI策略；不開發大型網路或交易執行系統。

## 可重現輸出

`python -m unittest test_p1_07_ai_audit -v`；`python p1_07_ai_audit.py --source-zip P1-06B.zip --out-dir NEW_EMPTY_FOLDER`。

輸出：`ai_opportunity_audit.csv`完整4583機會、`ai_split_census.csv`、`ai_complete_case_features.csv`、`meta_predictions.csv`全部525預測、`meta_model.json`、`meta_classification.csv`、`AI_FEASIBILITY_RESULT_ZH.md`與`ai_audit_manifest.json`。所有主要輸出及程式有SHA256；19項測試涵蓋母體、重複label、未來特徵、未知品質、purging、Train-only預處理、未知label仍預測及模型生效時點。
