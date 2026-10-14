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

`--limit` 範圍為 1–1000。成功時會在輸出目錄建立 `交易對_UTC時間/` 資料夾和同名 ZIP，包含相同 CSV 與 `metadata.json`。若現貨交易對無效或必要的現貨端點失敗，程式會以非零狀態結束；合約資料為選用端點，失敗原因會寫進 metadata，其他現貨資料仍會保存。

## 輸出檔案與欄位

- `klines.csv`：三個週期合併；`symbol`、`interval`、`open_time`/`close_time`（Unix 毫秒）、`open_time_utc`/`close_time_utc`、`open`、`high`、`low`、`close`、`volume`、`is_closed`、`source`。價格和成交量保留 Binance 回應的十進位文字，不做四捨五入。`is_closed=true` 僅在 K 線 `close_time` 早於 Binance `/api/v3/time` 的伺服器時間時成立；目前進行中的 K 線仍會輸出為 `false`。
- `spot_price.csv`：`symbol`、`price`、`captured_at_utc`、`source`。這是擷取當下的最新成交價回應，不保證與寫檔時刻完全同步。
- `spot_24h_stats.csv`：Binance 24 小時 ticker 欄位（包含價格變化、成交量、統計區間起訖等），另加 `captured_at_utc` 和 `source`。24 小時區間不是固定 UTC 日線。
- `futures_open_interest.csv`：合約未平倉量、端點提供的 `exchange_time`（毫秒及 UTC 格式）、本機 `captured_at_utc` 和 `source`。端點若不提供事件時間，欄位留空；擷取時間不等同交易所事件時間。
- `futures_funding_rate.csv`：最近一筆已回傳資金費率、`funding_time`（毫秒及 UTC 格式）、`captured_at_utc` 和 `source`。這不是未來資金費率預測。
- `metadata.json`：Binance server time、資料擷取時間、週期、每週期根數、來源端點，選用合約端點狀態（available/empty/error），以及端點錯誤細節（若有）。

CSV 使用 UTF-8 BOM，方便常見試算表軟體辨識繁體中文/Unicode。時間均以 UTC 標示。

## 測試

```bash
python3 -m unittest -v
```

測試使用本機 mock 回應驗證解析、完整性檢查及檔案輸出；它不會連上 Binance，也不代表任何即時行情或交易表現。

## 限制

- Binance 的端點、可用商品、資料格式或存取政策可能變更；遇到 HTTP 429/418 或 5xx 時會有限次重試，並遵從可解析的 `Retry-After`。重試耗盡後會回報錯誤，不會持續重打 API。
- 每個週期最多請求 1000 根；資料長度以 API 實際回傳為準，程式不會虛構缺漏資料，也不會把正在形成的 K 線當成已確認訊號。
- 合約資料僅為盡力擷取。現貨交易對可能沒有對應的 USDⓈ-M 合約；相關 CSV 可能只有欄名，失敗原因會記在 metadata。
- 工具只保存擷取當下取得的資料，不提供訊號、績效/勝率估算、投資建議或真實下單功能。
