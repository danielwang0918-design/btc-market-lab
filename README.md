# BTC Market Lab：Binance 市場資料擷取工具

第一版命令列工具，使用 Binance 公開 REST API 擷取現貨 K 線、最新報價和 24 小時統計，並嘗試取得 USDⓈ-M 合約未平倉量與最近一筆資金費率。只讀公開市場資料，無需 API Key，不會登入帳戶，也沒有交易或下單功能。

## 需求與安裝

- Python 3.10 或更新版本
- 不需安裝第三方套件；僅使用 Python 標準函式庫
- 執行環境需能連線到 `api.binance.com` 和 `fapi.binance.com`

## 執行

```bash
python3 binance_market_data.py BTCUSDT
```

預設每個週期擷取最近 200 根 K 線，週期為日線 `1d`、4 小時 `4h` 和 1 小時 `1h`。可指定其他 Binance 現貨交易對，例如 `ETHUSDT`。自訂 K 線根數和輸出目錄：

```bash
python3 binance_market_data.py ETHUSDT --limit 200 --output-dir data
```

`--limit` 範圍為 1–1000。成功時會在輸出目錄建立 `交易對_UTC時間/` 資料夾和同名 ZIP，包含 CSV 與 `metadata.json`。若現貨交易對無效或必要的現貨端點失敗，程式會以非零狀態結束；合約資料為選用端點，失敗原因會寫進 metadata，其他現貨資料仍會保存。

## 輸出檔案與欄位

- `klines.csv`：三個週期合併；`symbol`、`interval`、`open_time`/`close_time`（Unix 毫秒）、`open_time_utc`/`close_time_utc`、`open`、`high`、`low`、`close`、`volume`、`is_closed`、`source`。價格和成交量保留 Binance 回應的十進位文字，不做四捨五入。`is_closed=true` 僅在 K 線 `close_time` 早於 Binance `/api/v3/time` 的伺服器時間時成立；目前進行中的 K 線仍會輸出為 `false`。
- `spot_price.csv`：`symbol`、`price`、`captured_at_utc`、`source`。這是擷取當下的最新成交價回應，不保證與寫檔時刻完全同步。
- `spot_24h_stats.csv`：Binance 24 小時 ticker 欄位（包含價格變化、成交量、統計區間起訖等），另加 `captured_at_utc` 和 `source`。24 小時區間不是固定 UTC 日線。
- `futures_open_interest.csv`：合約未平倉量、端點提供的 `exchange_time`（毫秒及 UTC 格式）、本機 `captured_at_utc` 和 `source`。端點若不提供事件時間，欄位留空；擷取時間不等同交易所事件時間。
- `futures_funding_rate.csv`：最近一筆已回傳資金費率、`funding_time`（毫秒及 UTC 格式）、`captured_at_utc` 和 `source`。這不是未來資金費率預測。
- `market_structure.csv`：依已收盤 K 線建立的結構標記，欄位含事件類型、方向、形成/確認時間、價位區間、detector 版本、參數及 `evidence_json`。證據 JSON 逐根列出來源 K 線的 OHLCV、時間、週期、來源與角色，便於回查。
- `metadata.json`：Binance server time、資料擷取時間、週期、每週期根數、來源端點、選用合約端點狀態（available/empty/error）、錯誤細節（若有）、K 線完整性報告（含缺口位置與分析適用性），以及結構偵測方法與事件數。

CSV 使用 UTF-8 BOM，方便常見試算表軟體辨識繁體中文/Unicode。時間均以 UTC 標示。

## 市場結構標記（v1）

所有判定只使用 `is_closed=true` 的 K 線；尚未收盤的最新 K 線會被排除。每個事件都帶有原始證據 K 線和參數，因此結果可回查、可重算。

- **波段高低點**：左右各 2 根的嚴格局部高點/低點；需等右側 2 根 K 線收盤後才標記確認，端點未具備右側資料時不確認。
- **結構突破**：收盤價首次越過最近已確認的波段高點/低點。這個標記也作為 OB 偵測依據。
- **OB（Order Block）**：結構突破前，最近 10 根內、位於該波段確認之後的最後一根反向 K 線；價位區間採該根 K 線的完整最低至最高價。
- **FVG（Fair Value Gap）**：三根 K 線的影線缺口；第一根最高價低於第三根最低價為多方缺口，反向條件為空方缺口。
- **2B 假突破**：已確認波段首次被影線穿越，當根或接下來最多 2 根已收盤 K 線收回波段內才標記；沒有及時收回就不回頭標成 2B。

這些名稱在不同交易方法中有多種定義；以上是本專案明確採用的可重現規則，不代表市場共識或預測。目前只輸出歷史結構標記，沒有把標記轉成多空訊號、進出場建議或下單功能。

## K 線完整性與缺口處理

每根 K 線都會檢查時間戳是否為非負整數毫秒、開盤時間是否符合 UTC 週期邊界、收盤時間是否符合週期長度，以及開盤時間是否晚於 Binance server time。重複、錯序、未對齊、收盤時間不符或未來開盤時間會使該次擷取失敗，不會把可疑資料輸出成可分析資料。

對回傳範圍內的時間缺口，工具會在 `metadata.json` 的 `data_integrity.intervals` 中列出前一根、第一個與最後一個缺漏開盤時間、下一根及缺漏數量。整體 `analysis_suitability` 為 `complete_within_returned_window` 表示回傳範圍內未發現缺口；若有缺口則為 `partial_gap_segmented`。遇到缺口時不會補造或插值 K 線，市場結構偵測會在缺口處切開資料，各段獨立分析，任何事件都不會跨越缺口取證。

完整性報告只涵蓋 Binance 這次回傳的時間範圍；`--limit` 是最多請求數量，不代表此前的全部歷史資料都已核對。未發現缺口也不保證資料來源之外或回傳範圍以外沒有缺漏。日線依 Binance 預設 UTC 邊界檢查。

## 測試

```bash
python3 -m unittest -v
```

測試使用本機 mock 回應和明確標記的合成 K 線驗證解析、時間戳與連續性、缺口位置、缺口分段、只分析已收盤 K 線、未來資料不改寫已確認事件、結構規則、證據鏈與檔案輸出；測試資料不代表真實行情。

## 限制

- Binance 的端點、可用商品、資料格式或存取政策可能變更；遇到 HTTP 429/418 或 5xx 時會有限次重試，並遵從可解析的 `Retry-After`。重試耗盡後會回報錯誤，不會持續重打 API。
- 每個週期最多請求 1000 根；資料長度以 API 實際回傳為準。缺口會被標記並分段分析；時間戳或排序不合法時，擷取會中止。程式不會虛構缺漏資料，也不會把正在形成的 K 線納入結構判定。
- 合約資料僅為盡力擷取。現貨交易對可能沒有對應的 USDⓈ-M 合約；相關 CSV 可能只有欄名，失敗原因會記在 metadata。
- 結構標籤尚未經統計/績效驗證，不提供勝率、投資建議或真實下單功能。
