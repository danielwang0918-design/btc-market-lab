# P1-08C｜前瞻研究基礎建設驗收

2026-11-17（Asia/Taipei）。基準為P1-08B `330c87cc62252f8c1ee52756712954d5e0dfed30`／Draft PR #22；獨立分支`codex/p1-08c-forward-foundation`。先以`8a8caef`提交研究規格，再提交失敗測試與實作。完整程式Commit由本PR及Artifact的`artifact_manifest.json`／`p1_08c_manifest.json`記錄，不用自我引用的文件hash冒充獨立時間證明。

**本輪最小研究基礎與合成驗收完成；真實前瞻候選0、獨立交易證據0、啟動正式假說0、新策略0。** 沒有重新回測、改參數、提高歷史績效或修改P1-08B財務結果。可以驗收本地格式與防呆工具，**尚不能把它視為已營運、具有可信外部時間證明的完整前瞻收集系統**。

## 一、完成內容與重用範圍

先查既有`prospective_validation.py`、`prospective_daily_archive.py`及`prospective_candle_append.py`。P0已有原料封存、prefix SHA及checkpoint，但不含一般候選的形成／確認／實際接收與後續label版本。新層重用其SHA256與UTC工具，不呼叫P0模擬、lock／Genesis或資料封存寫入，不建立第二套回測框架。

| 需求 | 本次交付 |
|---|---|
| 完整候選格式 | `P1_08C_SCHEMA.md`、嚴格標準庫驗證器及明標合成的protocol JSON |
| 時間與完整性 | 形成／收盤／來源發布／系統接收／登記／標籤完成分開；future、未知、延遲、缺口與版本防護 |
| 候選不可覆寫 | 獨立JSON紀錄、全域序號與SHA256鏈、排他寫入、唯讀檔案、外部checkpoint核對 |
| 後續結果隔離 | candidates與labels實體分目錄；label只引用確切候選hash，不能修改候選 |
| 成本資料可行性 | 官方來源版本與hash、四次真實公開GET探測、時間／歷史／權限／容量／條款報告 |
| 未來驗證方法 | 固定日曆與P1-08A操作下限、群聚／BTC分開、成本／經濟驗證與power限制 |
| 可重現交付 | 全測試、離線合成驗收、原P1-08B核對、CSV、原始JSON、SHA清單及Actions Artifact |

新增三個小程式：`p1_08c_registry.py`登記／驗證，`p1_08c_demo.py`離線合成驗收與來源核對，`p1_08c_public_probe.py`四次有界公開資料探測。沒有常駐程序、排程、介面、大型AI、交易管理或自動委託。

## 二、候選登記與可得時間

protocol先凍結完整假說、程式／規則版本、研究臂、特徵定義與lookback、label、窗口、成本及固定日曆審查。P1-08C只有`SYNTHETIC_ONLY`範例，正式假說須另行審查與凍結；沒有把原失敗假說偷偷啟用。

每筆候選有唯一event_id、revision、幣種、1H／4H／1D、形成／確認時間、資料可得上界、登記時間、方向／watch、規則版本、完整已知OHLCV的hash引用、特徵值與依賴、預先固定label／窗口、缺口／延遲／未知原因。event_id含完整protocol SHA，避免同研究名但不同假說共用ID；等價UTC字串不會製造第二事件。不同命名的上游事件仍需對帳，不能單靠hash宣稱市場樣本獨立。

pending／invalid必須watch且confirmed_at=null。pending後取得新閉棒才能另版確認，舊版不補值；研究臂不可改派，confirmed不可透過revision翻轉方向，只能追加失效，失效不能復活。沒有正式可交易規則時不編造entry／SL／TP。

source publication未知保留null，不當成收盤即發布。系統received_at由寫入器產生，不能由使用者回填；這是本層ingest時刻，**不是已完成真實網路接收器整合**。已知特徵須引用登記前已存在的同幣閉棒、連續lookback、receipt≤known_at≤registration；不足或零量填未知並附原因。當期尚未閉棒量價及明確的MFE／MAE／PnL／exit欄位不能當特徵。外部特徵計算仍標`feature_calculation_verified=false`，時點與schema通過不等於公式語意被證明正確。

觀察起點固定為登記後下一個1H邊界，不是回填原形成時間，也**不是假設以該時價格成交**。窗口必須嚴格晚於登記，local seal前再檢查；協議也須在研究起點前完成本地seal。時鐘倒退直接拒絕。這是本地clock／fsync層的保護，可信真實時間仍要外部留存與時鐘監控。

## 三、不可覆寫與安全限制

每筆canonical JSON有全域連續序號、前項hash、系統記錄時間與自身SHA。暫存先fsync，再用排他hard-link建立正式檔及fsync目錄，沒有replace／刪除／覆寫API。單寫入鎖遇衝突或殘留直接拒絕；完整鏈檢查後才追加。寫入器程式hash與原凍結版不符時不能繼續追加，須新protocol／registry。此v1依賴支援hard-link及directory fsync的本地檔案系統。

**唯讀權限與hash鏈不是物理WORM，也不是可信第三方時間戳。** 若攻擊者能改整個未留存journal，仍可重建一條自洽鏈；沒有已知外部checkpoint時，刪掉尾段也可能無法察覺。必須在觀察開始前另行留存每批checkpoint並以可信外部副本驗證。工具可核對指定checkpoint仍存在且一致，卻不自行認證副本的外部時間與保存權限；`external_timestamp_attestation_verified=false`始終明示。

本次Actions Artifact是在合成測試後產生，不能用它把2030合成時間「公證」為前瞻。尚未部署外部WORM、保護分支留存、簽章／第三方時間或實際閉棒receiver，沒有聲稱已解決這些營運前置。

## 四、後續label與合成端到端結果

label在分開目錄追加，引用確切候選版本SHA；窗口未結束不能寫。complete須同幣完整1H觀察帶且無零量；缺棒／零量／缺值只能incomplete、values全null及原因。OHLC棒內順序維持unknown，不接受「一定先高後低」等成交宣稱。本層封存外部計算label但不認證算式，`calculation_verified=false`。本v1沒有label覆寫／更正API；錯誤時保留原件、停止評分、另凍結修正版流程。

| 合成驗收項目 | 結果 |
|---|---:|
| 固定八幣事件 | 8 |
| 候選版本 | 9（含BTC pending→confirmed） |
| 完整6h診斷label | 6 |
| 缺棒／零量未知label | 2 |
| 登記checkpoint一致性核對 | 9 |
| 最長窗口跨幣重疊配對 | 28 |
| 時間重疊連通分量 | 1 |
| 證明相互獨立的樣本數 | 無法由此確認（null） |
| 真實前瞻交易證據／新本金曲線 | 0／0 |

所有合成K線、protocol、紀錄、CSV及manifest均標synthetic，不混入公開真實成本探測。範例label只有未來診斷收盤／高點，沒有虛構交易、R、勝率或PnL。8事件不是8個已證明獨立機會；28對為所有版本與最長凍結窗口的保守包絡，**不是實際持倉曝險**。各版本不是新增樣本，連通分量也不等於獨立群聚的證明。

## 五、公開成本與流動性研究

詳見`P1_08C_LIQUIDITY_FEASIBILITY.md`。2026-11-17 11:04:42～11:04:46台北時間，四次公開GET（time、BTC bookTicker、depth20、recent trades limit5）均HTTP200且基本schema有效。只有當時少量快照；未連WS、未遍歷八幣歷史、未驗證全年Spot L2供應。raw HTTP與hash保留在`P1_08C_PUBLIC_PROBE.json`，不把它們稱為前瞻交易事件。

L1可算該瞬間spread，有限L2可計可見notional，逐筆有成交時間／ID；不能由此得到真實排隊、價格衝擊或實現滑價。REST book/depth與WS bookTicker沒有可靠發布時間時維持unknown；HTTP Date、伺服器時間樣本及receipt不能取代。Vision的每日／每月排程與微秒單位也不等於每個歷史檔當年立即可得。

容量表明列假設而非量測：八幣每小時一個0.5KB L1及2KB有限L2約480KB/日；不建全市場高頻平台。成本固定10bps fee及5／15bps slip不因本次小價差而調低。OHLCV的close×volume、非流動性代理不改名真spread／depth／CVD。免費可讀不等於任意再散布；按查核到的官方Vision條款記錄非商業與署名／相同授權限制，不替所有REST/WS作相同授權推定。

## 六、未來獨立驗證規格與樣本

`P1_08C_FORWARD_VALIDATION.md`給出可重現流程：新假說與程式先凍結及外部留存，之後新候選、再成熟label、再到固定日曆做readout。所有既有Test維持歷史探索，不重新標OOS。

沿用P1-08A操作地板：**至少24個月、20個非空共同28日區塊；每臂200合資格機會、30淨正／30淨負及30 BTC**。這不是power保證；下一真實假說須按最小經濟效果、變異及相關性另訂power與失敗條件。未達量或無可靠label就明報不足，不放寬門檻，不用跨幣重複或五個期限湊數。

未來需同時列BTC／其他七幣及各幣、機會／拒絕／watch／成交、成熟率、淨勝負、SL率、平均淨R、PF、交易成本、PnL／回撤、曝險與共同時間區塊不確定性。原則延用1000次／seed808作規劃基準，仍須揭露跨28日塊依賴。單倉紙上本金1000USDT／2%風險及5／15bps成本情境使用既有離線引擎，假想空頭另列，不能由孤立MFE重建盈利帳戶。本輪沒有新的經濟指標可驗收，也沒有挑獲利分組。

## 七、測試、資料版本及完整性

新增65項（registry53、public probe8、離線demo4）。固定Python3.12依賴完整套件 **697／697通過，無跳過**。一般標準庫CI為695通過及2項既有ML依賴測試跳過；專用Actions安裝固定套件執行全部697，沒有新AI訓練。P1-08B既有P0日期fixture及所有舊測試檔保持逐字不變。

測試涵蓋：未收盤／未來／小週期／錯幣／OHLC幾何／非有限值；未知發布與quote額；接收／特徵／觀察先後；缺棒lookback；pending不可假確認；同事件UTC別名、不同protocol身份與研究臂／方向不可改派；早寫label、缺棒／零量不可完整；候選結果分離；排他鎖、clock rollback、晚seal、覆寫／截斷／canonical／外部checkpoint；公開probe免Key、allowlist、HTTP錯誤／過大內容保留、不繞限流；合成八幣及禁止網路／重播的端到端驗收。

發現的錯誤皆限本輪新工具，先以失敗測試提交，再最小修正（例如`c9b732d`時間別名／未知欄位、`cbb092c`晚seal與首筆契約、`076dfe0`寫入版號／clock、`c1a6914`protocol seal、`6668797`完整protocol ID／不可改派）。不需要修改舊研究介面、原料或財務紀錄。修正前後不是「績效提高」；原P1-08B損益完全相同。

專用Actions只讀原P1-08B Artifact14286501992，ZIP SHA256：`75093e26e3dd7737d713037dd145edc5a425fae2b579d9558110b5afcab07305`。核對其24份輸出、2份繼承程式、160個原帳戶、24檔凍結原料／717638棒的已釘來源版本；來源ZIP逐byte不變，不重算策略或收益。

事前PLAN SHA256：`941d815152c3ee086f204af167e53a3e7b648c8f3bd5ef930f42d462536f9e07`，CI與`8a8caef`逐字比較。合成protocol SHA256：`83b73d5310ac2f137992c50e2f0990aefb6ca0a17bbfec263abbf96e05dc8b75`。程式、每筆紀錄、CSV、測試log、文件、原始公開probe及來源ZIP均在兩層manifest核對。原凍結OHLCV的hash在輸入ZIP既有manifest中，沒有新下載或覆寫K線。

## 八、重現與Artifact

固定環境：Python3.12、matplotlib3.10.8、numpy2.3.5、scipy1.17.0、scikit-learn1.8.0。後四項用於完整既有測試；新登記器與probe只用標準庫。

```sh
python -m unittest -v
python p1_08c_demo.py --out-dir NEW_EMPTY_DIR --baseline-zip ORIGINAL_P1_08B_ARTIFACT.zip
python p1_08c_registry.py verify --root NEW_EMPTY_DIR/synthetic_registry --anchor NEW_EMPTY_DIR/registration_checkpoints_external_copy.json
```

最後一行需由操作者在候選登記時獨立保存的單一checkpoint物件；demo內`registration_checkpoints.json`是陣列，驗收逐項核對，**不是已受公證的外部副本**。如只重現本地合成測試，可省略`--baseline-zip`，但不得因此聲稱已驗證原財務來源。輸出目錄不能已存在，防止覆蓋研究證據。UUID與檔案hash可隨獨立示範run不同，時間與內容契約／計數可重現；SHA驗證針對該run的確切內容。

完整Artifact含原P1-08B ZIP、合成journal全紀錄、登記checkpoint、候選CSV與事後label CSV、時間重疊稽核、所有中文規格／驗收、真實公開probe與文件來源版本、程式／測試、完整unittest log、獨立acceptance及全包file SHA256。Actions停用舊引擎入口並禁止demo網路呼叫；原繼承檔案逐字比較，無策略重播。

## 九、剩餘限制與下一步

1. 外部可信時間／不可變留存及真實閉棒receipt adapter尚未營運；必須先完成再開始真正前瞻。人工輸入source與外部features/labels仍需程式、raw bytes對照與語意審查，不能拿schema通過當來源真實或公式正確證明。
2. 全鏈逐次重驗適合小量研究；沒有大型收集、連續quote/depth修復、備份復原自動化或label更正機制。斷電若留下暫存或lock，fail closed、人工稽核，不默默刪除。
3. 真實成本只有四次BTC公開探測，完整八幣／歷史quote與L2、真正成交價格及單向延遲仍不足；不降低原成本、不宣稱精確實現滑價。
4. 目前沒有合格新假說或新樣本。先完善來源／時鐘／漏登監控與外部留存，再另提新機制及固定前瞻規格；不要因基礎建設完成就強行啟動失敗假說。

本次不修改main、P0／Genesis、正式`mtf-bos-ob-retest-v1`、任何既有PR或P1-08B數字。只用閉棒1H／4H／1D作候選決策證據，公開短週期資料僅成本可用性研究；沒有真實委託、交易授權、槓桿或自動合併。
