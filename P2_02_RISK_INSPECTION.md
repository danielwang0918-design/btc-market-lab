# P2-02｜BTC 事件風險巡檢（Draft 研究層）

本 PR 以既有 P2 宏觀行事曆風險模組為底，新增離線、只讀、可重播的 BTC/ETH 市場事件巡檢。**不合入 main、不修改正式 mtf-bos-ob-retest-v1 策略、不更動 P0/Genesis/原始資料、P1 研究、也不下單。**

## 四級判定（與交易訊號獨立）

1. **風險警戒**：新鮮、有來源連結的重大風險事項，或現有宏觀行事曆在有效狀態進入重大事件禁入時段。
2. **等待確認**：任何必要分類缺漏/過期、或仍有注意中的風險；缺資料不等於安全。
3. **候選交易機會**：必要資料完整且沒有更高級風險，另有 4H 已收盤結構、2B、OB、FVG、熱力圖、成交量、OI、資金費率、成本後淨風險報酬比九項來源證據。**不是交易指令**。
4. **未發現重大訊號**：所有分類皆有新鮮資料，且無已標註重大事件或完整候選；僅限本次輸入的有限覆蓋範圍，不能保證市場安全。

優先順序 1 > 2 > 3 > 4。沒有新聞方向推斷、沒有清算區預測成交、沒有事件勝率宣稱。機器只檢核提供的資料格式、來源網址、截止時間與分類；**有來源並不等於已由機器獨立核實來源所述內容**。

## 來源與資料治理

使用既有 Binance 資料 ZIP：BTC/ETH 現貨價與 24h 統計、現貨 24h 成交量、合約 OI、最後已公布的資金費率。OI 缺少交易所事件時間時不得視為新鮮；歷史 funding 不等於下一期 funding 預測。每份 ZIP 保留 SHA256、交易對與口徑。不合併不同交易所、週期、單位的清算/OI/funding 數字。

外部觀測由 evidence JSON 提供，用於宏觀實際值、官方新聞、ETF、清算、多空比、深度、熱力圖以及人工 4H 技術交叉確認。**本版不會自動下載這些資料**；缺少數據會明確列入缺漏，不填預估假數。宏觀事件行事曆直接使用現有 P2 event_risk.py 的輸出，維持既有禁入時段設定不變。

固定 13 項覆蓋分類：btc、eth、volume、oi、funding、long_short、liquidation、orderbook、etf、macro、news、technical、heatmap。每類有明確資料時效上限。分類有一筆資料不代表所有細項皆完整，v1 仍需人工品質覆核。

九項候選門檻：4h_structure、2b、ob、fvg、heatmap、volume、oi、funding、net_rr。各自需 confirmed=true 及新鮮 evidence_ids。4H 結構/型態另外要求 timeframe=4h、is_closed=true。這是**來源紀錄契約**，非自動獨立型態驗證；net_rr 仍需既有策略設定的手續費/滑價與使用者門檻驗證。候選不會傳送給交易引擎。

## 使用方式

Python 3.10+，只用標準函式庫。由原有 binance_market_data.py 先收集 BTCUSDT 與 ETHUSDT ZIP。新增 evidence JSON（以下只有格式範例，**沒有真實市場數據**）：

    {"schema_version":1,"as_of_utc":"2026-11-16T11:00:00Z","observations":[],"risks":[],"candidate_gates":{}}

執行示意：

    python3 p2_risk_inspection.py --evidence inspection_input.json --bundle data/BTCUSDT_來源包.zip --bundle data/ETHUSDT_來源包.zip --calendar data/events/calendar.json --out-dir research/p2_risk_inspections/demo

--bundle 可多次使用，--calendar 可省略。空資料不能得到「市場安全」判定，將列為第 2 級等待確認。輸出 inspection.json 與 inspection.md（嚴格 11 節），記錄來源、URL、各自時間、單位/口徑、風險判定、缺漏與 SHA256。

### evidence JSON 契約

observations 每筆需唯一 id、category、metric、value、unit、observed_at_utc（帶時區）、source_name、source_url（HTTPS）、scope 與 status=sourced。資料缺漏時標記 status=unavailable 與 reason。晚於巡檢時間、超過分類允許時效、NaN/Inf、沒有有效 HTTPS 或空數值均不算新鮮有效值。

risks 每項需 severity=critical/watch、summary、evidence_ids（引用實際 observation ID）；僅所有引用資料均有效時才會啟動。候選門檻以 candidate_gates 的 confirmed 與 evidence_ids 明示，不能只憑 OB/FVG 重疊。

## 下一階段的嚴格隔離

- 本版尚非自動實時巡檢。需分開研究官方宏觀實際值、ETF/監管、清算與 order-book 的可靠供應商及可回溯資料時間。
- 必須記錄資料的 **首次可用時間**，才可用於未來歷史回測；不得把事後發布的新聞回填到過去。
- 不直接把四級風險標籤改為正式策略進/出場條件。任何策略影響要另開獨立實驗 PR，通過樣本外驗證、資料溯源及費用測試後才討論。
- 不把模型推估的清算熱力圖當作確定清算價，也不把事件描述性有利比例當交易勝率。

單元測試使用合成 fixture，不呼叫市場 API。CI 綠燈只代表程式契約通過，**不代表 2026-11-16 的市場巡檢已獨立完成**。
