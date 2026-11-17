# P1-08D｜資料接收、重啟與留存操作

所有內部時點採UTC。僅資料基礎，沒有訊號生成、交易假說、實盤／帳戶Key或自動委託。以P1-08C固定Commit為基準，只新增檔案；`p1_08c_registry.py`仍是唯一正規化證據／候選格式。

## 架構與資料格式

`contract.json`釘來源Binance／Spot、BTC/ETH（可延伸既有八幣）、1h/4h/1d、接收器程式與PLAN hash、實際啟動時刻及P1-08C genesis checkpoint。`registry/`是原登記器；`batches/`只是transport／品質／checkpoint封套，不是第二套候選登記。`candidate_registration_enabled=false`，C registry若出現候選或價格label就停止，防止本階段偷啟新假說。

每批有原HTTP `*.body`、`*.receipt.json`、逐row處理結果`processed.json`、品質`summary.json`及含所有file SHA的`manifest.json`。batch hash鏈串前批，綁registry前後checkpoint與候選hash清單（本輪皆空）；同一來源／symbol/frame/open重複只記新receipt，不重登evidence。source OHLCV以外欄位如成交筆數更動也算修訂，不只比較close。原evidence保留，修訂持續阻止相關data readiness，須另行審查，不自動接受最新版。

原K線12欄都保留，REST毫秒時間明示：open time、inclusive close；正規化轉`close-exclusive = inclusive close + 1ms`，UTC對齊1H/4H/1D。先驗證OHLC幾何、有限價格／非負量與成交筆數。原wire SHA與normalized input SHA不同，raw row index及row SHA把兩者對照。來源發布時間缺失維持null，不用HTTP Date或close代替。

| 時點 | 意義 |
|---|---|
| 原open／inclusive close／normalized exclusive close | 市場K線範圍，不是發布或接收 |
| 前置serverTime | 公開來源clock樣本；不作市場資料publication |
| request_started_at／system_received_at | 本機牆鐘的請求與完整body讀完時刻 |
| monotonic_elapsed_ms／clock_session_id | 同程序請求耗時；跨程序不直接比較monotonic值 |
| closure_confirmed_at | 此次receipt時，依較早serverTime及已完成時間界確認；不是交易所另給的最早確認時點 |
| registry_ingested_at／C received_at | wire保存後，正式寫入正規化證據的時間，不能回填成wire receipt |
| GitHub artifact created／updated／獨立job observed_at | 遠端留存／核對紀錄，不是交易所時間或法律公證 |

前後serverTime須都可與本地時鐘相容、且後值不倒退。close-exclusive ≤ 前置serverTime−1000ms，並已早於當次K線請求開始／接收，才算閉棒。牆鐘倒退、與monotonic差超2秒、單請求逾10秒或server offset interval不相容±2秒會停止該批確認證據；原錯誤仍保存。這些是已固定工程閘門，不是市場參數，也不能取代可信時鐘／NTP監控。

## 啟動與有限收集

```sh
python p1_08d_receiver.py init --root NEW_REAL_ROOT
python p1_08d_receiver.py collect --root NEW_REAL_ROOT --out first-summary.json
python p1_08d_receiver.py collect --root NEW_REAL_ROOT --out restarted-summary.json
python p1_08d_receiver.py verify --root NEW_REAL_ROOT
python p1_08d_receiver.py projections --root NEW_REAL_ROOT
python p1_08d_receiver.py snapshot --root NEW_REAL_ROOT --out NEW_SNAPSHOT.zip
```

每次collect有界：前後time、六個K線請求（每個limit3）、BTC/ETH各L1與depth20，共最多12個GET，遇429／418立即停止，不重試。未閉棒只在raw／處理清單，不進確認證據。每次collect是新的程序也能從同root接續；先全鏈／raw／語意核對再發網路，不能讓壞raw混入下一批。

第一次收到先前已閉的棒明標`bootstrap_backfill`。啟動後才閉但晚超5分鐘收到為`late_catchup`，其他為`post_start_observation`；這些分類不證明真實首次發布時間，也不是交易候選。超時、缺棒、未知或來源修訂不產生假的連續資料。只有limit3，長中斷可能留無法自動補齊的缺口；此版保留缺口，不假裝具有完整歷史補抓器。日後若另開補抓工具，receipt仍須是補抓當下。

`projections`只提供已核對raw close與對應evidence／時間，不新增ATR、MA、RVOL或交易規則。最新poll失敗、來源修訂、缺棒、零量、啟動補抓、收集延遲及當下缺最新已收盤棒會影響data_quality_ready；舊資料不會因先前通過一次而永久就緒。無論資料旗標如何，本階段candidate_registration_enabled始終false。

## 留存與中斷復原

每個snapshot必須有完整receiver checkpoint及SHA，且存到本地以外。讀取中的receiver先取得排他鎖再snapshot；資料／batch／registry不一致、未完成`.pending-*`或stale lock會fail closed。沒有默默清鎖、刪掉pending或回滾已寫入evidence的功能。

恢復時保留損壞目錄，先從已驗證外部副本復原到**全新目錄**：

```python
from pathlib import Path
from p1_08d_receiver import Receiver,read
restored = Receiver.restore(Path('VERIFIED_SNAPSHOT.zip'), Path('NEW_RESTORED_ROOT'),
    expected_sha256='EXTERNALLY_PINNED_SNAPSHOT_SHA256',
    anchor=read('EXTERNALLY_RETAINED_RECEIVER_CHECKPOINT.json'), expected_mode='real_public')
restored.collect()
```

digest／checkpoint錯、symlink／zip路徑穿越、非新目錄或模式混用一律拒絕。新抓資料記新的receipt，歷史缺口不回填為當時可用。接收器或原C writer code hash改版不能在舊版本上追加，須另凍結研究版本並保存原件；同code不同doc Commit可延續。

## GitHub獨立留存與持續收集限制

專用Actions有三job：完整測試→真實capture（另程序重啟）→另一runner重新下載capture artifact、核對GitHub API digest／repo／Commit／Run、重驗snapshot raw與C checkpoint，再上傳external_retention_receipt。`P1_08D_PARENT.json`若存在，先下載釘選的前一run artifact與平台metadata，在新runner復原再接收，保留原前綴。

這是不同於本地目錄、可公開對帳的留存；沒有付費服務、私人API Key或新帳戶。GitHub內建只讀repo/actions權限用於本專案Artifact，不是交易帳戶授權。一般Git Commit、Actions時間與Artifact digest都不自動成為第三方時間公證；同repo權限仍可刪Artifact、改未來workflow，Artifact45天過期，metadata不是簽章憑證。

因此始終`external_timestamp_attestation_verified=false`、非WORM／法律等級時間戳。它只能證明在GitHub所記留存／重驗時已有這份內容，不能證明來源最早發布、抵抗repo管理員、證明當年首次接收或支持24個月永久保存。來源真實性依固定公開HTTPS接收程式與獨立runner紀錄；原資料沒有交易所簽章，local raw亦不能單靠自報hash證明源頭。

此workflow可在研究分支push後及支援的workflow_dispatch接續。GitHub一般排程依default branch；本專案禁止改main，所以**未啟用無人值守每小時收集**。CLI可由受監督排程器呼叫，但本輪不新增外部排程服務或假裝已部署。下一步只能先做資料試收；完整正式前瞻還需營運排程、缺漏警報、長期不可覆寫留存與時鐘／復原演練，不能拿幾次成功宣稱長期穩定。

## 成本觀測

L1為各次receipt的best bid/ask與spread；depth20為有限可見價位／notional。不同請求時間不同，不拼同瞬間簿；缺值留缺，沒取得資料不填0。真實執行滑價／排隊／撤單／impact無法驗證。模型費用10bps與滑價5/15bps不變；無交易、無保證成交價格。
