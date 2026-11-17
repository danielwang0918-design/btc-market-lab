# P1-08E｜27個月保存與復原政策

目標為至少24個月，設定27個月、不自動purge。這是保存政策及工程方案，**沒有宣稱已經過24個月或有不可撤回的平台保證**。

## 分層與現有部署

| 類別 | 保存與核對 |
|---|---|
| 原始HTTP／實際receipt／source hash | D批次原檔，逐byte不可由client覆寫；來源publication未知不補值 |
| normalized evidence | 原C/D格式，raw row與證據hash一對一重算；源修訂另留raw，不覆寫原件 |
| checkpoint／營運record／anomaly／復原 | Git canonical head、C/D CP、operation generation、UTC、PID／退出碼與incident ZIP |
| 候選／未來labels | 保留分離格式；本階段全部0，不把raw變成獲利訊號 |
| D固定基準 | C3真實capture與平台metadata另存frozen-baseline；48 receipts／12 evidence保留原hash與ID，不混入E系列分母 |

已部署的遠端副本是既有專案獨立data分支`research/p1-08e-data-archive`。普通Git ref沒有本輪設定的45天Artifact自動過期；client只做fast-forward、不force push，原始file不可改寫，新資料與紀錄只追加。先取得lease，資料在lease下publish，**從全新Git object store讀回**驗raw／CP，再清lease。儲存／readback失敗就停，不把未保存狀態當成功。

這與本地執行目錄分離，已實際測試丟棄工作副本後從GitHub新環境恢復。Git平台／repo管理員仍可刪branch或改寫歷史，Git commit時刻不是時間公證，不能稱WORM。源程式與已知root／head pin可查篡改，卻不能完全抵抗同權限管理員。[Git fast-forward規則](https://git-scm.com/docs/git-push)

## 備份與保留

1. 執行環境：指定持久volume，非唯一副本；每次收集前檢查free space與實際最新head。
2. 現有遠端：data Git分支及所有可達祖先，policy27個月、不自動刪。新runner核對root ancestry、file SHA、D語意與CP。現有GitHub授權可用，不新增帳戶、Key、付費服務或部署權限。
3. 可攜副本：CI產出`archive-history.bundle`並執行Git bundle verify，包含全部data history與raw；Artifact45天僅傳輸／短期驗收副本，不算24個月長期保留保證。
4. 規劃中的第三獨立副本：監督人至少每月下載bundle到既有離線媒體或另一已授權provider，保留27個月，抽查恢復。**本輪未部署第三provider或實體離線媒體**；同GitHub帳戶的多副本仍有共同管理／平台風險。

若需要新付費儲存、帳戶、私密憑證、WORM object lock或時間戳服務，先另提用量／方案／費用／權限。本輪沒有自動開通。既有Git與Actions受其配額及條款，不承諾未來容量無限或永久免費。

## 容量、過期及停止條件

固定最低free disk256MiB、archive payload上限512MiB；到上限或無法保存／讀回即停止，不擅自刪資料換空間或啟Git LFS。一般GitHub單檔上限及repo健康建議仍適用，[官方大檔案限制](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)。本次是少量raw／JSON與小型ZIP，不是高頻平台；7天需要的容量應先用實測bytes/day及保守上界估算，不能把幾次小樣本當24個月規模保證。

運作中超過2小時沒有verified backup就報backup_stale；保存policy剩30天內報retention_expiry_warning。已結束工程系列不需冒充每小時運作，但保留policy及定期離線完整性檢查仍有效。backup失敗會保留lease／diagnostic，parent落後、未封存local advance、hash不一致及時鐘錯誤都禁止下一次行情I/O。

每月人工檢查遠端branch存在、root ancestry、bundle可恢復、policy到期與平台配額；到期前決定續存或經批准移交，程式沒有自動刪除功能。若長期權限／帳戶狀態或配額無法維持，先停試收並保存最後有效CP及本地raw，不能宣稱仍有備份保證。

## 復原驗證

日常從最新已核對的遠端ref恢復到新目錄，逐file核對SHA與normalized values、原receipt、原C record hash／ID。root pin及head需由已留存參考提供；固定發布head只做特定驗收，不能永遠拿舊Artifact當最新版本。

本輪有實際程序SIGKILL後的新目錄／遠端復原，也有CI全新runner的只讀重驗；原有效45份檔案保持一致，D frozen-baseline獨立重驗。自然斷網、長停機、provider毀損及真正24個月保存沒有經實際驗證，不能以合成測試替代。

`external_timestamp_attestation_verified=false`；未部署第三方時間戳。GitHub提供可對照留存與執行紀錄，但不是法律公證或保證來源最早發布時間。
