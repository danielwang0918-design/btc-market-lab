# P1-08C｜公開成本與流動性資料可行性

查核日期2026-11-17（Asia/Taipei）。依官方原始文件與四次無認證GET連線探測；不是宣稱具備完整歷史成本資料。官方文件URL、Git blob版本與原文SHA256列於`P1_08C_SOURCES.json`；本次HTTP原回應、請求／接收時間、hash及衍生量在`P1_08C_PUBLIC_PROBE.json`。沒有API Key、交易帳戶授權或委託。

## 逐項可用性

| 資料 | 公開來源 | 時間與歷史 | 權限／本輪實測 | 缺值、延遲及限制 |
|---|---|---|---|---|
| 最佳Bid/Ask、spread | Spot REST `ticker/bookTicker`；WS `<symbol>@bookTicker` | REST當下快照、WS變動即時；沒有由此端點回補任意過去的能力 | 官方market-data-only列為無認證；REST BTC成功，WS未連線 | 文件中的payload無可靠交易所發布時間；updateId不是時間；receipt不是發布。一次價差不是平均有效spread |
| 可見order book depth | REST `depth`；WS partial/diff depth | REST最多5000價位/側；本輪只取20。WS partial 1000/100ms，diff需序號銜接與snapshot重建 | 無交易帳戶；REST BTC20檔成功，未建立WS簿 | REST無事件時間，兩次GET不同時；有限深度不是完整市場可成交量。未證明Spot八幣整段歷史L2可下載 |
| 成交量／成交額 | 原始klines的base volume、quote asset volume；公開trades/aggTrades | K線開收盤時間與成交時間不同；歷史依各幣上市／檔案供應。既有只有OHLCV時無真quote額 | 不需交易帳戶；本輪不下載或改原K線 | `close*volume`只是代理，不能補成真quote_volume；未收盤棒不可生成訊號 |
| 逐筆成交／聚合成交 | REST recent trades、aggTrades；WS trade/aggTrade；Vision日/月檔 | API/WS預設毫秒，可選微秒；Vision Spot自2025年起為微秒。每筆T是成交時間，不是下載或發布時間 | recent trades BTC5筆成功；aggTrades、WS及各幣歷史清單未逐一實測 | 聚合不等於每個撮合；分頁、重複ID、修訂與斷線須驗證。trade方向／量不能單靠OHLC重建CVD |
| 滑價／價格衝擊代理 | 某次公開簿依預先固定紙上金額計算可見VWAP、價差與深度 | 只能對該時刻可見簿作條件式估算 | 不需帳戶；本輪不開發新的impact模型 | 不含排隊、撤單、隱藏量、網路／策略延遲或簿的恢復，不能當實現滑價；沒有真實委託也沒有實際成交驗證 |

公開market-data-only官方清單及免認證說明：[Binance FAQ](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/market_data_only.md)。REST欄位與頻率／權重：[官方REST文件](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#market-data-endpoints)。WS時間單位及斷線／序號規則：[官方WS文件](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md)。精確所讀版本不依賴master永久不變，已另釘Git blob與SHA256。

## 真實探測與證據邊界

2026-11-17 **11:04:42～11:04:46 Asia/Taipei**，依序請求同一公開host的time、BTC bookTicker、depth20及recent trades limit5，四次HTTP200且通過基本payload檢查。沒有重試、替換市場或虛造失敗回應；另外的451／429等皆只是明標合成單元測試，不是本次網路結果。

本次bookTicker可計算該瞬間報價價差；depth為20+20個可見價位；recent trades只有5筆，**不代表連續成交歷史**。四次回應各有不同receipt，不能拼成同一毫秒的可成交簿。time只是一個伺服器時間樣本，沒有驗證本地時鐘偏移或單向延遲；HTTP Date也不冒充市場資料發布時間。每筆成交的原時間保留在raw與metrics，而整個HTTP回應沒有單一可認證市場事件／發布時點，保持null。

只實測BTC，不推定八幣全年無缺值。下一階段若研究需要，先對八幣／確定期間做可用性清單；某幣缺報價、停牌、無來源時間或序號斷裂時標未知。不以別的交易所或較低頻OHLC替代真實歷史spread。

## 歷史涵蓋與資料發布

[Binance Public Data](https://github.com/binance/binance-public-data)提供日／月檔，官方說明日檔次日、月檔在每月第一個星期一提供；這是排程說明，**不是每檔可靠實際發布時間**。各幣最早／最後可用日與缺檔需逐檔查；本輪沒有聲稱已遍歷、下載或驗證八幣完整歷史。已有trade/aggTrade/K線檔，不足以證明任意期間的Spot order book快照／diff歷史存在。

每次下載應保存原ZIP、CHECKSUM、實際receipt、schema、timestamp單位與原檔hash。供應方可能修訂檔案，不能拿現在下載的歷史檔反推當年已可取得；不覆寫已觀察前綴。2025起的Spot微秒與REST預設毫秒須明示轉換，禁止靠位數猜而不記原單位。既有P0封存及P1-08B凍結資料保持不變。

## 最小頻率與儲存規劃（估算，非量測）

優先在已收盤1H／4H／1D候選登記附近取得少量公開L1／有限L2成本快照，保留請求與receipt，不訂閱全市場高頻流。本輪未部署背景收集或排程；短週期報價只做成本研究，不能產生交易方向。

假設八幣每小時各留一個L1（0.5KB）及一個top20深度（2KB），192組/日約480KB/日、兩年約350MB，未含HTTP標頭、索引／副本、壓縮及額外候選；這是明列假設的容量算式，不是實際資料量保證。若8幣100ms全留且每訊息2KB，理論約691萬訊息／日、13.8GB／日；真diff大小與頻率變動，故不建這類平台。逐筆成交量因幣／時段差異大，先量測再做保存預算，不編造固定筆數。

REST權重、WS訊息／連線限制須依當次官方文件／exchangeInfo確認；429／418立即停止本次探測，不繞限流或地區限制。儲存前保留缺值與延遲，不前填quote，不用HTTP200掩蓋空／錯誤schema。

## 來源授權與下一步

本次讀到的[Vision Dataset Terms](https://github.com/binance/binance-public-data/blob/master/TERMS_AND_CONDITIONS.md)標示2026-08-26版、預設非商業CC BY-NC-SA 4.0並有額外條件；免費讀取不等於可任意商業散布。保留Binance來源署名；正式保存／再散布按取得時適用條款，REST/WS服務條款須各自確認，不能把Vision授權一概套給所有服務。本輪只有非商業離線研究與極少量公開可用性探測，未下載大型新增歷史資料集或商業部署。

**可行的是少量、無交易帳戶的前瞻成本快照；未解決的是可信時鐘、完整八幣歷史L2／quote涵蓋、真實成交滑價與持續營運品質。** 不因此改5/15bps原成本基準，也不把一次極小spread當成策略新優勢。
