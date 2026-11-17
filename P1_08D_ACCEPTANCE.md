# P1-08D｜真實公開資料接收與外部留存驗收

2026-11-17（Asia/Taipei）；原始研究時間全為UTC。固定基準P1-08C `987af42806989b2ceb89f3b07dc0994785e35417`，獨立分支`codex/p1-08d-real-receiver`、Draft PR #24。先提交`19e832d`固定規格，再提交失敗測試與必要實作。

**已完成最小真實接收、另程序重啟、從GitHub親代快照復原、獨立job核對與有限成本觀測。可開始受監督的資料試收；尚不建議宣告正式、無人值守、可長期獨立驗證的收集系統已就緒。** 沒有啟動價格／交易假說，候選0、未來價格labels0、策略0、真實委託0。`external_timestamp_attestation_verified=false`，沒有WORM或法律等級公證。

## 一、架構與重用

新增`p1_08d_receiver.py`與`p1_08d_retention.py`，只用標準庫及既有P1-08C工具。原`p1_08c_registry.py`是唯一正規化證據／候選格式，原檔未改；transport批次只是HTTP／品質／checkpoint封套，不是第二套候選登記器。所有繼承檔案、main、P0／Genesis、正式策略、原歷史與財務結果保持逐字不變。

receiver contract固定Binance／Spot、BTCUSDT及ETHUSDT、1H／4H／1D、程式與PLAN hash、實際啟動時刻及C genesis checkpoint。符號驗證可延伸原八幣，但本輪真實收集只用BTC／ETH。C schema強制的hypothesis欄位僅放`infrastructure-conformance-only`資料格式標記，不是啟用正式交易／價格假說；接收器禁止出現任何候選或價格label，不能利用該欄偷開策略研究。

資料分層：原HTTP bytes與receipt→逐row閉棒／schema／來源hash核對→C Registry證據→只讀raw close投影。候選與labels仍是原格式的分開目錄、本輪皆空；沒有加入ATR、MA、RVOL或新入場規則。

## 二、真實公開接收結果與適用範圍

以固定程式`28b56b0c2c0b7c25bef86670f6fdef93b812fedd`的[親代Run44022284590](https://github.com/danielwang0918-design/btc-market-lab/actions/runs/44022284590)為外部恢復基礎：完整739測試、真實接收及獨立留存三job全部成功。親代兩個程序、24份真實HTTP receipt，12根新閉棒、12次重複觀察、12次未閉棒排除、8份L1／有限L2。這些數字已由下載封包核對。

本PR最終run用`P1_08D_PARENT.json`釘親代Artifact、平台digest、Commit及checkpoint；在另一runner復原到新目錄，另執行兩次collect，再由第三job重新下載核對。親代前兩批須仍符合原checkpoint，不能僅看最新鏈自稱連續。最終完整raw及逐批計數可由`independent-verification/verified_receiver/batches/*/summary.json`、`processed.json`及external receipt交叉重算；來源更新時以該run封包為準，不把兩個run當兩組獨立市場樣本。

| 真實資料驗收母體 | 數量／狀態 |
|---|---|
| 幣種／市場 | BTCUSDT、ETHUSDT；Binance Spot |
| 閉棒feed | 每幣1H、4H、1D，共6個 |
| 親代新正規化閉棒 | 12根，每feed2根 |
| 親代HTTP實際回應 | 24份，兩個程序各最多12 GET |
| 親代限流／網路錯誤／無效／修訂／缺口 | 本次皆0，不代表長期不會發生 |
| 親代未閉棒 | 12次，只留raw／分類，不進確認證據 |
| 親代C evidence checkpoint | sequence13：1個資料contract及12根evidence |
| 真實候選／價格labels／交易委託 | 0／0／0 |

首次已過收盤才收到的棒是`bootstrap_backfill`，**沒有當成當時就已收到**。接收器啟動後才閉棒也另分按時觀察／late catch-up；發布時間依然未知。12根包含跨幣共同波動及1H／4H／1D巢狀期間，不是12個獨立經濟樣本。重複請求與重啟不是新事件或新增獨立樣本。

這是有限分鐘範圍的操作驗收，不是長期穩定性或新的OOS。未等待未來月份；沒有讀歷史Test挑條件，也沒有計算新回測績效。

## 三、原始來源、時間與語意核對

只用`https://data-api.binance.vision`的公開GET：time、klines（limit3、UTC）、bookTicker及depth20，不接受任意host、重導向、交易／帳戶端點、API Key或私人權限。來源與公開欄位依既有`P1_08C_SOURCES.json`所釘官方文件版本，並以本輪實際HTTP schema再驗證。來源真實性依固定HTTPS接收程式、預設CA驗證及獨立runner紀錄；**不是交易所簽章資料，也不能單靠local hash證明源頭**。

每份raw body有完整SHA256、長度、來源URL、symbol／frame、HTTP狀態／header／錯誤、request_started_at、完整body讀完的system_received_at、monotonic耗時與process session。正常回應保留原bytes；超過1MB只留有明標truncated的前綴、停止確認，不能因該前綴仍可解析JSON就拿來當完整證據。

原REST kline open／inclusive close皆毫秒；核對close=open+frame−1後才轉exclusive close。以前置公開serverTime−1000ms保守確認，且close已早於K線請求開始／receipt；後置serverTime必須可相容且不倒退。市場時間、收盤時間、確認receipt、來源發布時間、C ingest與遠端留存各自保存，不混為同一時點。

工程閘門：server offset interval與本地±2秒相容、wall/monotonic相差不逾2秒、單請求不逾10秒；無法通過時不寫確認證據。這只是來源clock一致性檢查，並非經獨立認證的NTP／硬體時間。REST不給可靠最早發布時間，source_published_at=null；HTTP Date與serverTime不填進發布欄，也不據此宣稱精確單向延遲。

每根以raw檔名／row index／整個12欄row SHA連到C evidence hash。獨立核對器重新從raw計算symbol、frame、open、exclusive close、完整OHLCV與quote volume，與C資料逐項比對，再核對raw receipt≤registry ingest。raw close投影只做這個特定的語意驗證，不能推廣成任意技術指標／外部公式都正確。

## 四、外部留存方式與可信程度

三job依序為完整測試、真實capture、另一runner的independent-retention。第三job重新經GitHub API取得artifact ZIP及metadata，核對平台SHA256、repo ID、Run、Commit、expiry／size，解出原snapshot到新目錄，重驗raw／normalized值／完整C鏈與checkpoint，另產生`external_retention_receipt.json`。

已核對的親代外部證據：

- capture Artifact14300934473，SHA256 `421a359bf0af8b27e0af3214708733fc53f1bb07219fc4c483239ca0934e214b`。
- 完整親代驗收Artifact14302270078，SHA256 `b40064b18d1834e64abbf734bc2b5a012ba666a6b4787b73a7dc092c6dc2f1a2`。
- GitHub所記capture留存時間為2026-11-17 13:11:57台北（研究UTC05:11:57），另一job核對時間為13:12:06.890台北（UTC05:12:06.890）。不是K線發布時間或法律時間戳。
- 親代checkpoint：batch2、hash `6a47951aa55a53253165715dd839adc6e60dfc51016b206968e7f5f76e78b202`；C sequence13／hash `73c9edab641f16c3d6060cd6e3ffe964d108c270d11c2b9aca7f7e7d8366c29f`，完整registry UUID及contract在PARENT檔。

這提供與本地目錄分開、可由另一執行環境核對的留存。**GitHub metadata沒有第三方簽章，repo管理員可刪Artifact／改未來workflow，45天會過期；不是WORM、不是法律公證，也不足以抵抗同權限管理員。** Artifact created/updated與job時刻只是平台紀錄，不把普通Commit或Actions時間當完整可信第三方時間證明。

因此`platform_artifact_digest_verified=true`及`receiver_checkpoint_verified=true`與`external_timestamp_attestation_verified=false`同時成立，不互相矛盾。所有checkpoint對照候選hash清單皆空，因本輪沒有正式候選；沒有用合成候選包裝真實外部驗收。

## 五、中斷、異常與復原

| 情境 | 處理／驗收類型 |
|---|---|
| 正常重新啟動 | 真實另一程序接續同root，重複棒不增加evidence |
| 跨runner恢復 | 真實下載已釘親代ZIP／metadata，在新目錄恢復，最終再核對親代checkpoint |
| 網路中斷、HTTP錯誤／HTTP200錯誤payload | 合成fixture保留raw／錯誤、partial，不假裝0根連續成功 |
| 429／418 | 合成fixture立即停止；跨程序遵守Retry-After；缺失／無效時不猜冷卻時間、不自動繞過 |
| 漏棒／延遲 | 合成fixture保留gap及收集lag；bootstrap／late catch-up不回填可得時間 |
| 重複／來源修訂 | 真實重複不重登；合成OHLCV及非OHLCV欄位修訂留新raw、原evidence不變、停止相關readiness |
| 未閉1H／4H／1D | 真實未閉棒排除；合成高週期／邊界測試不提前確認 |
| 零量／無效幾何／非有限／錯單位 | 合成fixture零量明標、無效不進確認；不補造資料 |
| 時鐘倒退／wall跳動／server倒退 | 合成fixture停止確認，保留診斷；未做真實故障或NTP公證 |
| raw漂移、registry超前、pending／stale lock、checkpoint截斷 | 合成fixture在新網路I/O前fail closed，不默默清除或覆寫 |
| ZIP穿越／錯hash／舊目錄恢復 | 合成fixture拒絕；正常snapshot保存空的C目錄與所有原始內容 |

少量真實操作未碰到所有故障，故障列的通過是合成測試，**不宣稱已做真實停機／機房斷電演練**。原件唯讀、批次排他鎖、先fsync再提交，仍非物理不可改寫；中途不一致要保留壞副本，從已核對外部副本恢復到新root。接收器程式改版不能在舊凍結版本上追加；須另凍結版本。

本版每feed只查最近3根。超過覆蓋能力的中斷可能留下缺口，不是完整歷史自動補抓器；補抓須另建可追溯操作，receipt仍為實際補抓時點。外部C CLI若在接收器之外寫證據或新增候選，也會因raw binding／本階段禁用候選而停止，不悄悄接受無來源證據。

## 六、有限成本與流動性

每次BTC／ETH各一份L1及depth20。保存各自receipt／raw hash；L1計中間價正常化spread，L2計可見價位數／notional。不同GET不是同步簿，最多20檔不能當整體可成交量；取消、隱藏單、排隊、網路延遲、價格衝擊及真實執行滑價均未驗證。`actual_slippage_bps=null`、`guaranteed_executable=false`。

親代8份快照只證明這些次數可取得，不代表全時段／八幣歷史覆蓋；後續run追加的觀測同樣不能推成保證成交價格。模型單邊10bps費用、5／15bps滑價、1000USDT與2%正常風險沒有調整，本輪根本沒有下單或重新回測。公開成交量不稱CVD，有限深度不稱實現impact。

## 七、測試、來源版本與可重現性

基準697項，新增42項（receiver36、retention6）；固定依賴完整環境 **739／739通過，無跳過**。一般標準庫CI為737項通過及2項既有ML依賴測試跳過，專用job安裝原固定套件執行全部739。新增程式只用標準庫，不訓練AI。正常真實I/O在capture job；單元測試HTTP回應都為明標合成fixture。

發現的錯誤限本輪新工具，先新增失敗測試再修正：未完成批次中的自讀、snapshot漏空目錄、斷線後舊readiness、未綁raw serverTime、C／receiver模式不一致、server倒退、跨重啟限流、已知truncated但仍可解析的prefix被接受。未改閉棒工程閘門來湊成功，未修改C或任何原資料／績效。失敗CI與修正Commit均保留，不只呈現最終成功。

PLAN SHA256：`1b03f89d4701498d7ca39ae10456ac26de14b8609a067a8218a67870b43d5905`，與`19e832d`逐字相同。receiver SHA256：`1a54cec18f40caab663bbecc1cf6cf4acb4566ac24f29d8e5a7d32ef70e8c5f0`；retention SHA256：`8b8f87ff4f1e152dd534c1d241529f3e9933635db918076fc711ea812dfbbb1f`。最終Commit、Run、完整file清單及hash在`artifact_manifest.json`，capture另有bundle manifest及原始batch manifest；不建立自我引用的假公證。

重現需要Python3.12；完整既有測試固定matplotlib3.10.8、numpy2.3.5、scipy1.17.0、scikit-learn1.8.0。執行`python -m unittest -v`。資料操作、offline snapshot／checkpoint驗證、mode／clock／catch-up語意詳見`P1_08D_OPERATIONS.md`。

新的公開請求價格自然不同，不能宣稱重抓會得到相同市場值；可重現的是對已留存raw做相同正規化、時點及hash核對。要逐byte重現某次結果，下載該次Artifact，核對平台digest／file manifest，再用對應Commit的retention verifier；不要拿未來資料覆蓋舊raw。

完整Artifact包含：739測試log、當次capture外層ZIP與GitHub metadata、原始snapshot、全部raw／receipt／逐row分類／summary／C Registry、完整checkpoint、獨立留存receipt、實際parent reference、程式／測試／方法／操作／驗收及全檔hash。親代capture digest及metadata已寫入當次capture_context，恢復後的前兩批可獨立核對；不是只存最新一張圖或摘要。

## 八、是否可以正式啟動及剩餘事項

**可：受監督、小規模、資料用途的試收與可重啟的有界接收。不可：宣稱已完成正式無人值守24個月收集、可信第三方時間公證，或已取得交易策略獨立驗證資格。**

後續優先：

1. 不改main前先決定真正排程營運位置與監控人。研究分支push驗收已跑；GitHub一般schedule依default branch，本輪未啟用無人值守排程。可用持久root CLI，或在平台支援的workflow_dispatch傳最新已釘parent；重用舊parent會形成明示分支，不會自動接在最新run後，不能把fork誤稱單一持續鏈。
2. 明定超過45天、覆蓋至少24個月的外部保留／備份政策，監控Artifact過期與刪除。真正不可覆寫儲存／第三方時間戳尚未部署；若需付費或新憑證，先另提可行性，不自行開通。
3. 完善時鐘校準、漏收／來源修訂告警、真實停機／斷電與復原演練，以及超過limit3的可追溯補抓；維持補抓時間誠實標示。
4. 在資料營運品質穩定後，才另行固定價格假說與原始來源到技術特徵的語意驗證。本輪沒有任何新交易假說可啟動；資料成功接收不等於正期望或可保證交易。

main、P0／Genesis、正式`mtf-bos-ob-retest-v1`、所有既有PR、歷史交易與回測績效均未修改。不調ATR／RR／TP／SL／AI，不新增價格結構策略，不交易授權／下單／槓桿，不自動合併Draft PR。
