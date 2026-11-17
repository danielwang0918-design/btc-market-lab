# P1-08E｜受監督前瞻資料試收與營運可靠性驗收

驗收日期：台北2026-11-17；所有市場、接收、停機及研究紀錄採UTC。固定基準為P1-08D [`c3b27a6aaf33b8291b9b3c7559521433c3f44b99`](https://github.com/danielwang0918-design/btc-market-lab/commit/c3b27a6aaf33b8291b9b3c7559521433c3f44b99)。獨立程式分支為`codex/p1-08e-supervised-pilot`，獨立資料分支為`research/p1-08e-data-archive`。

## 驗收結論

| 項目 | 判定與適用範圍 |
|---|---|
| 第一級：工程及營運準備 | **通過本次已驗證範圍**：連續父版本檢查、單一遠端lease、保存後獨立讀回、監控、操作文件、真實程序中斷及新目錄復原均完成。可在指定持久環境由監督人啟動小規模試收；沒有宣稱無人值守服務已部署 |
| 第二級：真實7天試收 | **尚未啟動，未通過**。`seven_day_status=not_started`、`level2_pass=false`。正式試收累積時間0，沒有用工程分鐘、重複請求或合成時間補成168小時 |
| 外部時間公證 | `external_timestamp_attestation_verified=false`。GitHub留存、Git history、hash及CI不是完整WORM或法律時間公證 |
| 保存 | 27個月政策、不自動刪除；遠端資料Git分支及可攜Git bundle已驗證。沒有宣稱已保存滿24個月；第三個獨立provider／離線媒體尚未部署 |
| 策略與交易 | 新交易假說0、候選0、未來價格labels 0、真實委託0、已驗證正期望策略0。未重算或修改任何歷史財務結果 |

工程系列已明確結束，防止其後空閒時間被誤算為7天試收。尚無指定可常駐168小時的主機、持久volume及每日監督人；本次沒有安裝daemon、timer、付費服務、私人憑證或調整部署權限。

## 固定方法與最小實作

先提交[固定PLAN](P1_08E_PLAN.json)（`3181b02`）與[方法](P1_08E_METHODS.md)，再提交可重現的失敗測試及實作。PLAN的SHA256為`e1d2451e497266da5a6f7a5bf4a9faf47fe9cd6c81b4dde64d0054387555486b`，最終CI逐byte核對首份版本。

全部改動是新增P1-08E檔案；原P1-08C Registry、P1-08D receiver及所有繼承檔案保持逐字不變。沒有第二套候選登記器、回測重寫或新技術指標。

| 程式 | 功能 |
|---|---|
| `p1_08e_ops.py` | 持久狀態監督；每次行情I/O前讀實際遠端head及驗父CP；本地flock與遠端Git fast-forward lease；保存、獨立readback、顯式啟用及每小時排程入口 |
| `p1_08e_monitor.py` | 六個feed每日JSON報告、真正已到期閉棒分母、未知／延遲／缺口、備份／容量／lease／異常告警、168小時狀態 |
| `p1_08e_drill.py` | 真實公開資料接收、獨立程序SIGKILL、保存partial state、從遠端恢復到新目錄並續接 |
| `p1_08e_verify.py` | 另一環境只讀下載、Git ancestry與fsck、原始／normalized／CP語意、真實停機證據、D基準及可攜Git bundle重驗 |

固定營運值：UTC每小時minute02；已收盤資料收集延遲容忍5分鐘；磁碟至少256MiB；封存payload上限512MiB；運作中備份超過2小時未核對告警；保留期剩30天告警。這些是營運限制，未調整ATR、RR、SL、TP、AI或原研究5／15bps滑價。

## 真實公開資料與時間紀錄

獨立工程演練由2026-11-17 **07:12:54.297 UTC**開始；工程系列於**07:30:01.403 UTC**明確結束（台北15:12:54.297～15:30:01.403）。資料來源沿用D的公開Binance Spot端點，無交易所帳戶權限／API Key；原始HTTP body、receipt、system receive time、來源可提供的server time及SHA全部保留。未知的來源publication時間維持未知，不能當成「閉棒立即可取得」。

| E工程系列的已完成資料 | 數量與解釋 |
|---|---|
| 完整接收批次／canonical HTTP receipts | 2／24；24次完整HTTP皆成功，這只是HTTP完成率 |
| BTC／ETH × 1H／4H／1D已確認閉棒 | 12；每feed 2根，均是工程啟動前閉棒的warmup，沒有回填成當時即時收到 |
| 重複觀察 | 12；復原後重新取得相同閉棒，不增加事件 |
| 未完成K線 | 12；排除於已確認證據之外 |
| L1／有限depth觀察 | 8；可觀測資料，並非實際成交、可執行報價保證或實現滑價 |
| 完成批次的HTTP錯誤／來源修訂／無效／零量 | 本次皆0；沒有以此宣稱營運永遠無異常 |
| 遠端inventory檔案 | 78；另有catalog及完整Git history |
| 中斷前有效原檔 | 45份，逐byte與SHA／原時間／原ID不變 |

被中斷程序另留下實際HTTP receipt及raw，放在incident ZIP，不混入上述24份canonical receipts或確認數。所有真實來源與合成fixture分離；1H／4H／1D是巢狀時間，以上數字不是獨立交易樣本。

## 真實停機與復原證據

1. 先完成一批真實接收、遠端保存及全新Git object store獨立讀回。
2. 啟動另一個實際接收程序；確認收到HTTP200的公開klines body並核對hash後，對該程序群組執行受控SIGKILL。
3. 保留PID、host、receipt、實際stop／exit時間及未完成目錄ZIP；作業系統返回碼**−9**。
4. 從實際最新遠端有效版本恢復到全新目錄；核對lease owner已死，保存中斷ZIP，再接收公開行情。未刪改舊有效資料或把partial偽裝為完整批次。
5. 最終CI在新runner只讀重驗遠端祖先、實際lease owner、incident ZIP、raw hash／時間、45份原檔及原D基準。

| 實際時間／內容 | 結果 |
|---|---|
| SIGKILL | `2026-11-17T07:13:26.448+00:00`（台北15:13:26.448） |
| 復原後成功接收 | `2026-11-17T07:13:51.512+00:00`（台北15:13:51.512） |
| 不可用時間 | **25,064ms** |
| 停機期間1H／4H／1D閉棒及minute02排程節點 | 皆0；沒有捏造缺棒、late catch-up或假連續性 |
| 未完成state ZIP SHA256 | `49430f9693c4d8d99a48807122ceee82ce203a1adf9bf753b32cbd188605edf2` |

這是**真實程序停機／遠端復原**，不是整機斷電、真實長時間斷網或跨小時漏收演練。長停機超出D最近3根的查詢範圍仍可能留洞，應明示並停止；本次沒有宣稱具備任意歷史補抓器。自然限流、漏棒、修訂、磁碟耗盡及時鐘故障沒有在真實封包出現，相關保護僅由明確標記的合成／本地Git測試覆蓋。

## 連續性、保存與獨立核對

資料authority是既有儲存庫的獨立[data分支](https://github.com/danielwang0918-design/btc-market-lab/tree/research/p1-08e-data-archive)，不以一次性目錄或固定舊Artifact代替最新狀態。每次接收前要求遠端實際head＝預期head＝本地已完成publication；local CP、完整inventory、root ancestry及PLAN均須吻合。僅使用普通fast-forward，競爭程序不能同時取得lease；失敗者不得呼叫行情API。

資料先在lease下封存；新object store讀回核對成功才完成operation並清lease。保存／readback失敗會停止並保留lease及diagnostic，不自動超時搶鎖或採納未封存本地分叉。異常程序恢復須實際死亡證據；同主機PID仍存活時拒絕清lease。跨主機死亡證據仍需可信營運流程及人工查核，不能只靠自報布林值認定可信。

| 固定證據 | 值 |
|---|---|
| Root pin | `58d98629fd838f3bb7d5ee273e0eb1264e4de96a` |
| 結束工程系列的archive head | `fa1ac3aca246829060634838c054a6969c445db4` |
| D接收batch CP | `89aca53ac5a204d826dcd5bac08d47efcc508db637b627c517711dd72a39c781`（2批） |
| C journal CP | sequence13，record SHA `930d0a4633908c370cddea38e7c9f770662446eafe57eea14b1acbb01212eb0d` |
| 原P1-08D capture SHA256 | `b5178061c7dcdef7261fc31f2b39f33b6640dceb0cb94656df414b022fc8b9c7` |

[發布reference](P1_08E_ARCHIVE_REFERENCE.json)釘選本次工程證據，不能當成未來永遠有效的營運head。原D capture及metadata另存frozen-baseline：Run44023196507、48 receipts、12 normalized，原封包獨立重驗，沒有混入E試收分母。

[保存政策](P1_08E_RETENTION_POLICY.md)設定27個月、不自動purge；遠端Git branch與本地分開，已從保存副本恢復。CI另產出包含整條資料history的`archive-history.bundle`並執行`git bundle verify`，可由操作者離線備份。45天Artifact僅短期驗收／傳輸，不能滿足24個月保證。第三provider／離線實體副本及每月監督維護尚待實際落地，沒有私自開通付費服務。GitHub同帳戶／管理權限仍有共同風險，管理員可刪branch或改寫歷史，未設不可撤回WORM保護。

## 監控、日報與測試

每日按UTC、幣種、週期分列expected、due、confirmed、pending grace、missing、late、duplicates、revisions、成功率、最後有效CP、外部備份及未解異常。以session開始後真正到期的收盤計分母；0分母成功率為null。raw／normalized無法核對時實際計數為未知，不補0或100%。

本次工程窗口沒有新的收盤節點，六個feed各expected=0、confirmed=0、成功率null；12根warmup及12次重複另列。工程已關閉，所以`continue_conditions_met=false`是預期的關閉狀態，沒有偽裝成仍在運作或未來漏棒。保存的工程日報沒有未解告警。

監控／停止項包括HTTP／限流／來源錯誤、逾5分鐘未收、延遲、零量、修訂、raw與normalized不一致、CP／parent落後、lease未清、時鐘倒退、低磁碟、容量、備份失敗／過舊、保留期將到及本地異常。這是每次操作與每日人工檢查的監控，不是已部署的24小時外部paging服務；主機整體消失仍須獨立監督人發現。

**完整Python測試775／775通過，無跳過**（P1-08D原739＋E新增36）。新增測試涵蓋父版本／unbacked local fork、跨client lease競爭、雜湊／時間／backup失敗、未啟動與不足168h、UTC分母、partial day、未知／pending／late、仍存活PID、已結束工程window及scheduler cache更新。兩個本地Git client的競爭測試不冒充真實跨機故障。

發現的新E問題均先新增失敗測試再最小修正：未封存本地CP前進不能自動採納；資料不能核對不能顯示假成功；工程結束後不能繼續擴大分母；post-start late必須保存後阻止自動續收；長程序須讀同目錄最新已完成publication，不能永遠使用舊記憶體head。沒有修改原D或正式策略來使測試通過。

工程程式與操作文件已由[Run44041939993](https://github.com/danielwang0918-design/btc-market-lab/actions/runs/44041939993)在`de78837a031a2320138d61a526b0eef81810c4ae`驗證。加入本驗收報告後的最終Commit／Run／Artifact以本階段Draft PR交付連結及`artifact_manifest.json`為準。專用workflow只有`contents:read`，不從CI修改資料分支或部署排程；所有繼承檔案與事前PLAN的未變更檢查均須成功。

Artifact包含完整source／tests／中文文件、775測試log、`artifact_manifest.json`逐檔SHA、`independent/acceptance.json`、全新環境日報、raw與receipt、normalized／C journal、CP、operation／異常／真實中斷ZIP、D原capture與metadata、Git bundle及獨立verification-files。衍生Git cache不包含在交付，原資料與可攜備份均保留。

## 7天試收的啟動與剩餘限制

[操作手冊](P1_08E_OPERATIONS.md)提供持久volume初始化、status、明示start-trial、run／人工oneshot、每日保存、STOP及新目錄復原流程。GitHub schedule通常需default branch，本次不改main，不用研究分支未生效的cron宣稱已排程。

下一步應先指定能實際常駐的主機、持久儲存、UTC/NTP監控及每日監督人；另建有明確起點的試收資料系列，核對遠端保存後啟用168小時。時間由實際clock產生，不接受回填起點，不重置已開始的試收。每天核對全部六feed、CP／backup與異常；long outage資料不足就保留缺口，不造連續性。

**滿168小時也只進入`awaiting_full_evidence_review`，不自動通過第二級**。仍須覆蓋整段窗口的日報（任意起點可能跨8個UTC日期）、所有CP／原始hash、備份證據、漏收／中斷／late紀錄及人工審核。未來資料試收成功也不是新獨立OOS交易證據，更不代表正期望。

未完成：真實7天、長停機跨閉棒補抓演練、持久主機部署及外部heartbeat、第三獨立備份、真正24個月留存、可信第三方時間公證、平台／管理員毀損後復原。這些限制不以合成測試替代。本次符合工程準備的交付範圍，不提前宣告第二級成功。
