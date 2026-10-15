# BTC Market Lab：市場研究與交易復盤工具

使用 Binance 公開 REST API 的研究工具：輸入 `BTC` 可擷取 `BTCUSDT`，依明示策略規則輸出「候選做多」、「候選做空」或「不適合開單」，並將每份分析和人工交易復盤留存。只讀公開市場資料，無需 API Key，不會登入帳戶，也沒有下單或連接交易帳戶的功能。

## 需求與安裝

- Python 3.10 或更新版本
- 不需安裝第三方套件；僅使用 Python 標準函式庫
- 執行分析需能連線到 `api.binance.com`；合約補充資料來自 `fapi.binance.com`，屬選用端點

## 首次設定與分析

```bash
python3 market_research.py --setup
```

設定時會先完整顯示 `mtf-bos-ob-retest-v1` 規則，再要求你確認並填入最低淨風險報酬比、報價最長秒數、單邊費率、單邊滑價及 OB 情境最長有效小時數。程式不會替你填入這些個人門檻。設定保存在 `.market-lab/strategy.json`，可供檢視；數值門檻可在檔案中調整。策略規則版本不符時，程式會要求重新設定。

設定完成後只輸入幣種即可分析：

```bash
python3 market_research.py BTC
```

也接受 `BTCUSDT` 或其他 Binance 現貨交易對。第一次直接執行 `python3 market_research.py BTC` 且尚未設定時，互動終端會先要求設定，再接著分析。非互動環境請先執行 `--setup`。

分析會使用日線 `1d`、4 小時 `4h` 和 1 小時 `1h` 各最多 200 根 K 線、現貨最新報價與現有結構事件。原始 ZIP 保存在 `data/`；每次分析 JSON 存在 `research/analyses/`。JSON 包含分析時間、ZIP SHA-256、策略版本、設定快照、每個週期最近已確認的結構突破、候選所用 OB/波段目標、K 線證據、完整性結果、報價時間與拒絕理由。

若只想擷取資料，不執行策略分析，仍可使用：

```bash
python3 binance_market_data.py BTCUSDT --limit 200 --output-dir data
```

`--limit` 範圍為 1–1000。擷取器保留原始 K 線與 CSV/ZIP 欄位。

## P0：歷史資料下載與回測

長區間資料由 Binance 公開現貨 K 線端點分頁抓取；每次最多 1000 根，資料存在 `data/historical/`，按交易對與週期各存一個 CSV。第一次不指定起始日會從 BTC 現貨資料起始日 `2017-08-17` 開始；後續不指定 `--start` 時，會接在現有最後一根 K 線之後更新。指定 `--start` 可下載或補回指定區段，重複時間戳會合併為單一根，輸出按時間排序。日期按 UTC 解讀；`--end YYYY-MM-DD` 會包含該日。

```bash
python3 historical_backtest.py download BTCUSDT --interval 1h --start 2020-01-01 --end 2025-12-31
python3 historical_backtest.py download BTCUSDT --interval 4h --start 2020-01-01
python3 historical_backtest.py download BTCUSDT --interval 1d --start 2017-08-17
python3 historical_backtest.py backtest BTCUSDT --fee-bps-per-side 10 --slippage-bps-per-side 5 --target-r 2
```

下載器沿用本專案 Binance client 的逾時、有限次重試與退避，並檢查 OHLCV、時間對齊與收盤狀態。Binance 歷史回應有時以時段內最後成交時間作為 `close_time`；CSV 同時保留 `exchange_close_time` 原值，並以週期邊界作為 `close_time` 供連續性檢查和回測。回傳範圍內若有缺口，會保留 CSV 並在下載摘要列出；回測會在缺口切段，結構判定和持倉都不跨越缺口。資料只納入已收盤 K 線。

P0 回測策略版本為 `structure_break_with_order_block_v1`：只在 1H 已收盤結構突破與 OB 同時確認後產生訊號，下一根 1H 開盤進場，以 OB 外緣作停損，目標採固定 R。回測只串流計算 BOS/OB 所需事件，避免完整結構標籤器在大樣本上計算不必要的事件。費率和滑價均按進出場每一側計入。若同一根 K 線同時觸及停損和目標，按停損先到處理；跨越缺口或資料切分邊界的持倉不會延伸到下一段，而在該段最後收盤平倉。資料依時間順序切成 train/validation/test（預設 60%/20%/20%），輸出各段交易數、勝率、平均盈虧、期望值 R、profit factor、最大回撤 R、連敗數，以及依方向和 setup 的統計。完整交易明細和切分界線寫入 JSON。

這是供驗證資料和程式流程的基準回測，不表示策略有效；目前沒有參數搜尋、資金曲線倉位模型、點差/流動性模型或策略優化。預設每筆一單位作比較，績效仍可能受市場微結構及 OHLC 模擬限制影響。此功能只讀公開行情，不登入帳戶、不送出訂單。

## P1：比較 OB、FVG、2B 假設

先用固定定義比較各版本，不會因結果自動改動 `market_research.py` 的候選判斷。研究程式會輸出每個版本的 setup 數、被哪條規則排除、train/validation/test 指標與相對 P0 的差異：

```bash
python3 p1_research.py BTCUSDT
```

研究採以下預先固定的假設：

1. `p0_ob`：P0 對照組；1H 收盤確認結構突破，且同一收盤確認 OB，下一根 1H 開盤進場。
2. `ob_fvg_overlap`：OB 形成至突破訊號收盤之間，需有同方向、已收盤確認的 1H FVG，且 FVG 與 OB 價格區間有正面積交集；只在邊界相接不算重疊。若同期間有反方向 FVG 與 OB 重疊，該 setup 觀望。
3. `ob_2b_confirmed`：需有同方向 2B，且回到已確認 swing 內的收盤確認發生在訊號收盤或前兩根已收盤 1H K 線內。影線刺穿但未收盤回到 swing 內不算確認；窗口內出現反方向 2B 則觀望。
4. `ob_fvg_and_2b`：上述 FVG 和 2B 條件都成立，且沒有反向衝突，才保留 setup。

四個版本使用相同的費率、滑價、停損優先、時間順序切分與缺口分段。版本定義和兩根 K 線窗口不在 test 結果出爐後調整；test 是一次性的留出檢查，不用來挑參數。若某版本交易筆數很少，應視為證據不足。這個研究比較只對照 `historical_backtest.py` 的 P0 1H 基準，不會自動改變即時/手動研究候選規則；只有穩定的留出資料表現與足夠樣本才適合考慮升級策略版本。

### P1：合約市場歷史資料與清算事件

新工具 `derivatives_research.py` 專門處理 Binance USDⓈ-M 永續合約公開資料。先下載指定日期區間，再產生時間對齊的價格/OI 比較與資金費率極端值標記：

```bash
python3 derivatives_research.py download BTCUSDT --start 2026-09-15 --period 1h --interval 1h
python3 derivatives_research.py analyze BTCUSDT --period 1h --interval 1h
```

下載器會分頁並增量合併 CSV，收集 OI 歷史、資金費率歷史、全市場帳戶多空比、主動買/賣量、taker 買賣比及合約 K 線（含主動買入量）。分析檔會按時間戳對齊已收盤合約 K 線，分別列出價格變化、OI 名目價值變化、同期多空比和主動買賣量；價量/OI 的四種組合只是同步狀態標籤，不會被解釋為「新增多單」或「新增空單」。

資金費率使用嚴格早於該筆資料的前 30 日絕對費率第 95 百分位作極端值門檻，至少需要 30 筆先前紀錄；樣本不足時標示 `insufficient_history`。這個門檻是透明、可重算的統計標籤，不代表交易訊號。費率正負只說明費率方向，不足以推斷市場淨持倉。

Binance 公開 OI 統計、多空比及 taker 歷史統計目前只提供最近 30 天，且每次最多 500 筆；資金費率歷史另由其端點分頁下載。若要求區間早於保留期，metadata 會記錄實際可請求的起點，而不會補造資料。不同資料序列的 coverage 會各自寫入 `BTCUSDT_derivatives_metadata.json`。

### P2：總經與政策事件風險提醒

先同步 BLS 官方發布行事曆（含 CPI、Employment Situation 非農就業報告）、Fed 官方 FOMC 會議日期，以及 Fed/SEC 官方 RSS 標題：

```bash
python3 event_risk.py sync
python3 event_risk.py show
```

排程與已人工核對的事件存在 `.market-lab/events/calendar.json`。官方 RSS 標題只進入 `review_required` 清單，不會由程式推測 BTC 影響範圍或漲跌方向；確認重大政策或 BTC 事件後，可用 `add` 明確登錄時間、官方來源、影響範圍和嚴重程度：

```bash
python3 event_risk.py add --title "政策公告" --category policy --time 2026-10-15T12:30:00Z --source-name "Official agency" --source-url https://agency.gov/news --scope BTCUSDT global_markets --severity critical
```

`market_research.py` 分析時自動讀取此行事曆，亦可用 `--event-calendar` 指定其他檔案。透明的預設規則是：CPI、非農和 FOMC 利率決議列為重大事件；事件前 60 分鐘至後 30 分鐘暫停候選情境，事件前 24 小時內則要求重新評估。行事曆超過 48 小時未同步或不存在時，報告會明確標示未知並要求人工確認；不存在/過期本身不會假裝成「沒有事件」。這些時間窗是保守的操作護欄，尚未以歷史績效最佳化。FOMC 預定決議時間依 Fed 月曆標準化為美東時間 14:00，執行前仍須回看最新官方月曆。

官方來源：[BLS 發布行事曆](https://www.bls.gov/schedule/news_release/bls.ics)、[Fed FOMC 會議行事曆](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm)、[Fed RSS 清單](https://www.federalreserve.gov/feeds/feeds.htm)、[SEC RSS 清單](https://www.sec.gov/files/about/secrss.shtml)。BLS 行事曆涵蓋多數近期新聞發布且會更新；FOMC 日期來自 Fed 會議行事曆。RSS 不是完整的突發事件偵測器，重大加密產業事件仍需人工從可信來源登錄。本功能只降低信心或暫停情境，不做新聞情緒分析、價格方向預測或自動下單。

清算事件可從開始記錄時起保存：

```bash
python3 derivatives_research.py record-liquidations BTCUSDT
# 按 Ctrl-C 停止記錄
python3 derivatives_research.py liquidation-heatmap BTCUSDT --price-bin-usd 100
```

熱區 CSV 將已觀測的強平快照按實際平均成交價分箱，並分開彙總多方被清算（SELL）與空方被清算（BUY）的數量和名目值。Binance 每個交易對每秒最多推送一筆強平快照，這不是完整逐筆強平資料；REST API 也沒有可回補的全市場歷史清算清單。因此熱區只代表本機開始記錄後觀察到的事件聚集，**不是**未觸發倉位的預估清算價或未來必達區域。推估潛在清算價需要帳戶槓桿、入場價、保證金模式和維持保證金等未公開的持倉資料，本工具不會假裝能從 OI 或資金費率推算。

## v1 分析規則與狀態

策略規則完整寫在設定檔及程式版本 `mtf-bos-ob-retest-v1` 中：

1. 日線、4H、1H 最近的已收盤結構突破必須都有資料且同方向；缺少或不同向時輸出「不適合開單」。
2. 以最新同向且其後尚未被已收盤 K 線回測的 1H OB 全部價格區間作回踩區；OB 外緣作止損參考。
3. 目標使用 OB 確認之後才被確認、超出進場區最近且未被其後已收盤 K 線觸及的反向已確認 1H 波段。
4. 任一週期有缺口、無效資料、報價過舊、OB 超過設定有效時間、目標已被現價越過、成本後淨風險報酬比未達你的門檻時，不提供候選價位。
5. 若所有條件通過，狀態為「候選做多」或「候選做空」。現價高於多方回踩區／低於空方回踩區時標示「等待觸發」；現價進入區間時標示「目前已觸發」。這代表現貨報價觸及價格區間，不代表成交、反轉確認或任何委託。

淨風險報酬比使用較保守的進場區邊界，以每 1 單位基礎資產計算；進場及假設止損／目標出場各計一次設定費率與滑價。模型不含點差、資金費率、借幣成本、稅費、流動性、成交機率或合約乘數。分析以現貨 K 線和報價為基礎；「候選做空」只描述價格方向條件，不代表現貨可直接做空或指定任何合約。OB/FVG/2B/波段等名稱依本專案可重現的程式慣例，並非市場共識或經驗證的預測規則；目前候選規則用 OB 作進場區、波段作目標，FVG 與 2B 會列入事件上下文，不作獨立入場訊號。

### 狀態判讀

- **候選做多／候選做空**：通過設定的資料與規則檢查，仍是條件式研究情境，不代表現在應下單。
- **等待觸發**：報價尚未回到 OB 區間。
- **目前已觸發**：最新現貨報價在 OB 區間內；不是成交紀錄或下單許可。
- **不適合開單**：資料或任一必要規則不足。JSON `reasons` 會列出原因；不會硬湊進場點位。

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

這些名稱在不同交易方法中有多種定義；以上是本專案明確採用的可重現規則，不代表市場共識或預測。研究分析器只依 README 的 `mtf-bos-ob-retest-v1` 設定組合候選或不交易狀態。

## K 線完整性與缺口處理

每根 K 線都會檢查時間戳是否為非負整數毫秒、開盤時間是否符合 UTC 週期邊界、收盤時間是否符合週期長度，以及開盤時間是否晚於 Binance server time。重複、錯序、未對齊、收盤時間不符或未來開盤時間會使該次擷取失敗，不會把可疑資料輸出成可分析資料。

對回傳範圍內的時間缺口，工具會在 `metadata.json` 的 `data_integrity.intervals` 中列出前一根、第一個與最後一個缺漏開盤時間、下一根及缺漏數量。整體 `analysis_suitability` 為 `complete_within_returned_window` 表示回傳範圍內未發現缺口；若有缺口則為 `partial_gap_segmented`。遇到缺口時不會補造或插值 K 線，市場結構偵測會在缺口處切開資料，各段獨立分析，任何事件都不會跨越缺口取證。

完整性報告只涵蓋 Binance 這次回傳的時間範圍；`--limit` 是最多請求數量，不代表此前的全部歷史資料都已核對。未發現缺口也不保證資料來源之外或回傳範圍以外沒有缺漏。日線依 Binance 預設 UTC 邊界檢查。

## 既有人工情境風險檢核

`market_risk_review.py` 仍可讀取 ZIP，檢查一組由使用者明確提供的價格情境：

```bash
python3 market_risk_review.py data/BTCUSDT_資料包.zip \
  --side long --entry ENTRY_PRICE --stop STOP_PRICE --target TARGET_PRICE \
  --fee-bps-per-side FEE_BPS --slippage-bps-per-side SLIPPAGE_BPS \
  --max-quote-age-seconds MAX_QUOTE_AGE_SECONDS --invalidation-interval 4h
```

請將大寫欄位名稱換成自己的情境與假設。`--minimum-net-rr` 可選，用來比較使用者自行設定的最低淨風險報酬比；程式不內建通用門檻。成功後會在 ZIP 旁建立 `*_risk_review.json`，記錄輸入資料包 SHA-256、報價時間與年齡、三個週期的缺口狀態、最近已確認結構突破方向、使用者方向與結構標記的逐週期比較，以及止損價相對指定週期最近已確認反向波段的位置。

淨風險報酬比按每 1 單位基礎資產估算，費率和滑價均由使用者指定，並假設進場及出場各套用一次。計算不含點差、資金費率、借幣成本、稅費、流動性、成交機率或合約乘數。缺口、舊報價、缺少結構事件、結構方向不一致和價格順序錯誤會各自列出；不會合併成「可交易」判斷。波段參考的相對位置只是可追溯的機械比較，不代表普遍適用的止損規則。

## 人工交易記錄與復盤

分析結果會顯示 `analysis_id`。完成交易後，將實際平均進場、平倉價、數量、實際總費用、含時區的時間和復盤筆記連結到該分析；原分析沒有候選方向或實際方向不同時，另加 `--side long` 或 `--side short`：

```bash
python3 market_research.py journal ANALYSIS_ID \
  --entry 101000 --exit 103000 --quantity 0.01 --fees 2.06 \
  --opened-at 2025-01-10T13:00:00+00:00 --closed-at 2025-01-10T17:00:00+00:00 \
  --notes "記錄進場原因、執行差異與下次要檢查的事項"
```

復盤 JSONL 保存在 `research/trade_reviews/reviews.jsonl`，會比較計畫與實際進出場、實際費用、費用假設及損益。只有原分析含有效方向與止損時才計算實際 R；分母使用實際平均進場至原計畫止損的價格風險乘數量，實際費用扣在損益分子。復盤筆記只供日後查看，不會自動調整策略，也不統計或宣稱勝率、績效。所有 `research/` 和個人設定都留在本機並列入忽略清單。

## 測試

```bash
python3 -m unittest -v
```

測試使用本機 mock、fixture 和明確標記的合成資料，涵蓋擷取、K 線完整性及缺口分段、已收盤 K 線、未來資料隔離、事件證據、策略設定、候選與不交易狀態、多空成本後風險報酬、報價新鮮度、缺口阻擋、人工紀錄與復盤。測試資料不代表真實行情，也不呼叫即時 Binance API。

## 限制

- Binance 的端點、可用商品、資料格式或存取政策可能變更；遇到 HTTP 429/418 或 5xx 時會有限次重試，並遵從可解析的 `Retry-After`。重試耗盡後會回報錯誤，不會持續重打 API。
- 每個週期最多請求 1000 根；資料長度以 API 實際回傳為準。缺口會被標記並分段分析；時間戳或排序不合法時，擷取會中止。程式不會虛構缺漏資料，也不會把正在形成的 K 線納入結構判定。
- 合約資料僅為盡力擷取。現貨交易對可能沒有對應的 USDⓈ-M 合約；相關 CSV 可能只有欄名，失敗原因會記在 metadata。
- 結構標籤與候選規則尚未經統計/績效驗證，不提供勝率或績效宣稱、投資建議、帳戶登入或真實下單功能。

