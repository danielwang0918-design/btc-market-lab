# P1-08E｜固定營運驗收方法

固定開發基準P1-08D `c3b27a6aaf33b8291b9b3c7559521433c3f44b99`。原C/D接收器、原始資料與所有繼承檔案保持逐字不變。先提交此規格與測試，再實作；本階段不啟動價格假說、策略或真實交易。

每小時UTC minute02為規劃收集節點，沿用D的5分鐘收集lag及原時鐘閘門，不改市場／交易閾值。狀態須位於指定持久目錄，且每次I/O前必須驗證實際遠端最新封存head及D checkpoint。固定舊Artifact只是bootstrap，不是持續head。

使用既有GitHub專案新增data-only封存分支，不更動main、不合併PR、不調整branch protection或部署權限、不新增帳戶／Key。普通fast-forward push先取得全域lease，競爭失敗者不能呼叫行情API。資料完成後先在lease下封存，再從全新Git物件儲存readback驗證，成功才清lease；保存失敗停止。失效lease需人工／受控演練提供已停止程序證據，不因超時自行搶鎖。repo管理員仍可能改寫分支，不能稱WORM。

原始HTTP／receipt、D normalized evidence、operation／anomaly、checkpoint、候選與labels分層保留；後兩者本輪皆0。保存政策27個月、至少24個月、不自動清除；45天Artifact僅次要傳輸／驗收副本。設容量／磁碟／過期／備份年齡警報，未能保存／核對即停止。跨provider／離線第三副本只列政策與操作，不假稱已部署。

每日分母只算session開始後、截至asof真正已收盤的UTC節點；尚在5分鐘容忍期的pending另列。Bootstrap、duplicate、late catch-up、未收到／未知不得當成功。BTC/ETH三週期分列，不加成獨立交易樣本。7天狀態獨立於工程演練：未啟用正式試收就標not_started，實際不足168小時不得通過第二級。

真實演練以獨立程序接收公開資料後受控SIGKILL，保留PID、返回碼、實際UTC停止／恢復時刻及未完成目錄hash；從已核對遠端有效狀態復原到新目錄，重新接收。不把短停機捏造成1H漏棒；若停機未跨收盤，該窗口漏棒數為0，仍須保留不可用時間。自然缺口／來源修訂若未實際發生，只能說合成測試覆蓋。

CI用既有只讀權限取得封存分支，獨立驗證raw／時間／所有checkpoint、受控停機證據及每日報告。不能以一次性CI目錄為唯一資料副本。獨立data分支與受監督本地持久目錄是營運方案；本環境未能保證7天常駐，不擅自宣布或假造7天成功。`external_timestamp_attestation_verified=false`始終維持。
