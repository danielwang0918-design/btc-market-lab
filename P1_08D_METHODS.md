# P1-08D｜真實資料接收與外部留存：固定方法

基準P1-08C `987af42806989b2ceb89f3b07dc0994785e35417`。不改任何繼承檔案；先固定規格及失敗測試，再實作。此研究只驗收公開資料管線，不啟動任何價格／交易假說，不計算新收益。

1. 原始HTTP bytes、請求開始／完整接收UTC、monotonic耗時、HTTP狀態／錯誤與header、URL、SHA256先保留，才正規化。只准公開market-data-only host與明列GET端點；不接受Key、私人帳戶或任意URL。限流不重試；中斷不是零根正常連續棒。
2. 用資料組之前的公開serverTime保守確認收盤：close-exclusive不晚於serverTime−1秒，並核對本地clock可相容、request wall/monotonic無異常。serverTime不是資料發布時間；來源publication未知保留null。REST kline的inclusive close轉exclusive明記，原wire時間單位毫秒，不用收盤時間冒充receipt。
3. 同批保留未閉棒、無效row及錯誤，不寫入確認證據。閉棒正規化使用原P1-08C Registry.ingest；其received_at是registry ingest，另保存wire receipt及closure_confirmed_at。原文內容hash與normalized hash分開，逐row提供raw索引。沒有新的衍生交易特徵；最基本closed close投影需從raw重算語意核對，不因schema通過就宣稱任意公式正確。
4. transport batches是接收與品質紀錄，不是第二套候選登記器。批次鏈綁raw receipts、來源身分、P1-08C checkpoint及候選hash。重複棒不重寫evidence；來源OHLCV修訂留新raw、原evidence不變並停止相關readiness。零量、缺棒、過晚收集、來源／時鐘錯誤明標，不補值。bootstrap／catch-up時刻皆為現在收到，不能回填當年可用。
5. 每次重啟重驗raw、row對照與registry checkpoint。途中失敗留下pending，拒絕繼續，不能默默刪掉或重建好看的鏈。復原從已獨立留存、核對hash及checkpoint的完整副本到新目錄，再重新抓缺漏資料；收集時間不回填。保留壞副本供查核。此版不做不必要的自動交易或大型資料平台。
6. BTC／ETH每次各L1及depth20；不同GET不拼成同一瞬間簿。只描述報價價差與有限可見深度，不當保證成交價格／實際滑價。固定10bps費用與5/15bps模型滑價不變；本輪不下單，沒有實現滑價。
7. 外部留存選低權限GitHub Actions：capture artifact由另一job重新下載、核對平台digest及原raw／registry，另留checkpoint與GitHub run/job/artifact時間。這提供獨立於本地目錄的可核對存在上界，但管理員能刪除、會過期，也未有第三方簽章時間戳；始終`external_timestamp_attestation_verified=false`，不稱WORM／法律公證。候選0也如實列出，不用合成資料當真實留存。
8. 真實公開資料與synthetic fixture完整隔離。第一次取得的較早閉棒標bootstrap，未收到的期間明示；不聲稱首次接收已是長期前瞻。至少驗證正常重啟、獨立外部核對及從已驗證parent留存恢復的後續run；若外部能力／網路不足，報未完成而非假造通過。
9. 持續收集採有界CLI及可接續parent的data-only Actions，無交易假說。非default branch的GitHub自動schedule限制另揭露，不改main。未完成長期連續性、外部保留政策及可信時鐘前，只能建議受監督資料試收，不把一次／幾次成功當正式獨立驗證已就緒。

所有時間內部採UTC；人類報告日期另標Asia/Taipei。`P1_08D_PLAN.json`固定工程品質門檻，不是ATR／RR／交易參數。任何修正先重現失敗，不為取得好看營運結果放寬門檻。
