# P1-08E｜受監督試收操作手冊

本文件是資料營運流程，沒有交易策略、交易所私鑰、帳戶授權、下單或槓桿。研究時間採UTC。當前驗收只有工程演練，7天試收為**尚未啟動**。

## 1. 指定持久環境與監督人

真正試收前，操作者必須確認：持久掛載的狀態目錄、主機可持續運作至少168小時、現有GitHub儲存庫Git寫入權限、UTC/NTP監控、每日人工檢查者、磁碟／容量預算及復原人。不要把一次性CI workspace或本對話執行器當唯一持久來源。本輪沒有保證此環境7天常駐，也沒有安裝daemon／timer。

GitHub憑證只用既有專案的Git讀寫，不是交易所Key；本輪未建立任何私人憑證／新帳戶、未擴張repo／部署／branch-protection權限。若新主機尚無合適授權，先由使用者處理，不能為了方便自行開Key。

## 2. 另建明示的7天資料系列

工程封存分支`research/p1-08e-data-archive`已在演練後結束，不能把其空閒日子算成試收。正式試收應建立新的、明示起點的data分支，例如`research/p1-08e-pilot-YYYYMMDD`；不把D的短驗收、E的工程分鐘或不同系列混成連續7天。不要修改main或合併任何Draft PR來啟排程。

在使用者選定的持久volume，用對應固定程式版本初始化（以下路徑是待替換範例，未在本輪部署）：

```python
from pathlib import Path
from p1_08d_receiver import Receiver
from p1_08e_ops import Supervisor
volume = Path('/MOUNTED_PERSISTENT_VOLUME')
seed = Receiver.create(volume/'seed-receiver', mode='real_public')
snapshot = volume/'seed.zip'
seed.snapshot(snapshot)
operator = Supervisor.create(volume/'pilot-state',
    remote='https://github.com/danielwang0918-design/btc-market-lab.git',
    branch='research/p1-08e-pilot-YYYYMMDD', snapshot=snapshot,
    snapshot_sha256=__import__('p1_08c_registry').sha_file(snapshot),
    checkpoint=seed.checkpoint(), mode='real_public')
print(operator.head())
```

所有目錄及分支須全新；存在時不能重新初始化覆蓋。此初始化只建資料系列，尚未啟動7天clock。已有系列應用已核對的實際最新遠端head復原到新目錄，不能從舊Artifact重置。

## 3. 明確啟用後才開始計時

先驗證來源、磁碟、遠端備份與監督安排，再由操作者執行：

```sh
python p1_08e_ops.py status --root /MOUNTED_PERSISTENT_VOLUME/pilot-state
python p1_08e_ops.py start-trial --root /MOUNTED_PERSISTENT_VOLUME/pilot-state --expected-head ACTUAL_LATEST_HEAD --operator-id OPERATOR_ID --durable-host HOST_AND_VOLUME_DESCRIPTION --confirm-persistence
python p1_08e_ops.py run --root /MOUNTED_PERSISTENT_VOLUME/pilot-state
```

start-trial時間由實際系統clock產生，不接受歷史開始時間，不能重置已開始的clock；持久性說明是操作者聲明，不是主機SLA認證。run在UTC每小時minute02執行，等待時定期檢查STOP檔，最多至168小時目標，資料品質／備份／lease失敗立即停止。每次都重驗實際遠端head、同一持久目錄的最新已完成publication、D checkpoint與raw inventory；人工日報操作完成後，長程序會重新讀已完成的本地cache，不會持續拿舊head。

也可使用人工每小時oneshot：`python p1_08e_ops.py cycle --root ... --expected-head ACTUAL_LATEST_HEAD`。明示舊head或本地資料落後／超前都拒絕，不能以忽略錯誤繼續。ACTUAL_LATEST_HEAD須由當下status與遠端ref取得；`P1_08E_ARCHIVE_REFERENCE.json`是**發布驗收的固定證據pin，不是永遠有效的營運head**。

長期主機可由操作者設定user-level systemd服務呼叫run，使用持久WorkingDirectory、同一root、Restart=no及UTC/NTP。此處不提供自動安裝器，也未啟用服務。若要改用cron/timer，不可同時運行第二個run；本地flock及遠端lease仍會阻止雙寫。GitHub schedule通常需default branch，本輪不改main，因此不以研究分支的未生效cron假稱已排程。[官方排程文件](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

## 4. 每日人工檢查

```sh
python p1_08e_monitor.py --root /MOUNTED_PERSISTENT_VOLUME/pilot-state --day YYYY-MM-DD --out NEW_DAILY_REPORT.json
python p1_08e_ops.py daily --root /MOUNTED_PERSISTENT_VOLUME/pilot-state --day YYYY-MM-DD --expected-head ACTUAL_LATEST_HEAD
```

第一項只讀輸出，第二項把報告以新的不可覆寫operation紀錄封存並讀回核對，不呼叫行情API。檢查六個feed的expected／due／confirmed／pending grace／missing／late／duplicate／revision、HTTP完成率、最新CP、backup時間、容量／磁碟及未解異常。HTTP200不是閉棒品質成功，0分母顯示null，資料不能核對時實際計數為未知，不填0。

日期可為已過或當日，不可為未來。部分UTC日只算session開始後到asof已閉棒；5分鐘容忍內未收到先列pending，過期才列missing。1H／4H／1D巢狀時間不當獨立交易。7天從任意UTC時刻開始可能跨8個UTC日期，報告覆蓋整個168小時窗口，不能硬湊7張完整日表。

尚未啟動或已結束的工程時段會明示engineering_only／engineering_closed；不延伸成未來試收分母。現有工程報告包含12根warmup及重複觀察的來源紀錄，但期內沒有新的到期收盤節點，所以期內expected=0、成功率null；不能稱100%試收成功。

## 5. 異常與復原

| 異常 | 必要動作 |
|---|---|
| 預期閉棒逾5分鐘未收、post-start late／零量／修訂／無效 | 保存raw與當時CP；監控列警報，quality阻止後續自動接收；先人工查因 |
| HTTP錯誤／429／來源不可用 | 保留receipt及錯誤，遵守D跨程序Retry-After；不換Key或繞限流 |
| stale parent／lease競爭／本地有未封存資料 | 在行情I/O前停止，不自動採納或覆寫；查實際遠端最新版本 |
| raw／normalized／CP不一致、pending／stale D lock | 保留壞目錄，從已驗證遠端有效狀態復原到新目錄 |
| 低於256MiB磁碟、payload超512MiB | 停止；不可自動刪除歷史或開付費空間 |
| 遠端publish／獨立readback失敗 | lease不清除，保留本地diagnostic及原CP；不把接收當已備份完成 |
| 程序異常退出且留lease | 先查host／PID與實際退出證據；不只因lease過期就搶鎖 |

受控SIGKILL演練使用獨立工程程序，原始未完成目錄另存incident ZIP，從遠端有效state恢復到新目錄。`recover_stopped`要求停止證據、lease-owner PID吻合，同主機PID仍存活時拒絕。跨主機須人工／可信營運紀錄確認，不是自報布林值就具有法律證明。

恢復API範例：`Supervisor.restore(NEW_ROOT, remote=..., branch=..., expected_head=ACTUAL_REMOTE_HEAD, root_pin=KNOWN_ROOT_PIN, mode='real_public')`。完整核對後才處理已停止lease；不可自動刪pending、reset舊root或重新命名舊receipt為當時即時收到。D只查最近3根，長停機超覆蓋範圍可能留洞；目前會明示並停止，沒有假稱已具任意歷史補抓器。

## 6. 每日與7天驗收

每日由監督人核對已保存報告／異常及復原動作。真正滿168小時後，狀態先是awaiting_full_evidence_review，**時間到了不自動通過第二級**。需整段UTC窗口報告、所有CP與remote ancestry、備份／過期紀錄、缺口／late／修訂、異常處理及人工審核；缺資料不能用重複請求或合成clock補足。

這是營運驗證，不是研究獨立OOS或正期望證明。候選、正式交易假說、交易委託維持0；`external_timestamp_attestation_verified=false`。
