# P1-08C｜候選資料格式與欄位規格 v1

所有 JSON 由標準庫驗證器 `p1_08c_registry.py` 驗證；不是只靠欄位命名或一份不執行的 schema。未知欄位、重複 JSON key、NaN／Infinity 拒絕。時間為顯式 UTC ISO8601（Z 或 +00:00），精度至毫秒；等價時間表示不會產生第二個事件。1H／4H／1D 採 UTC 對齊及 **close-exclusive**。

## 共用不可覆寫 envelope

| 欄位 | 意義 |
|---|---|
| version／registry_id | 格式版本與本研究 journal UUID |
| sequence／kind | 全域連續序號；protocols、evidence、candidates、labels 之一 |
| recorded_at | 寫入器取樣的系統 UTC 時間；不是交易所事件時間或外部公證時間 |
| previous_sha256 | 前一紀錄 hash；首筆 null |
| payload | 下列對應類別，不混放候選與結果 |
| record_sha256 | 除本欄外 canonical JSON 的 SHA256；UTF8、sorted keys、無多餘空白 |

檔名 `<kind>/<sequence:010d>.json`。先寫暫存、fsync，再排他 hard-link 提交及 fsync 目錄；無 replace。候選提交前再檢查觀察尚未開始。檔案設唯讀，但管理員仍能改，因此仍須外部 checkpoint、備份與可信時鐘；不是硬體 WORM。checkpoint 用 registry_id／sequence／record_sha256／recorded_at 核對鏈中既有位置，必須從可信外部副本取得，不能從待驗證目錄自己產生當信任根。

## protocols（唯一首筆）

payload 保存 protocol、mode、registry_id、frozen_at、writer_code_sha256、固定八幣及三週期。mode 為 synthetic、prospective 或 historical_import，之後不能切換；注入時鐘只允許 synthetic。所有輸出永不自行宣稱 independent_forward_evidence。

protocol 必填：study_key；hypothesis 的 id／statement／falsification；rule 的 id／version／code_sha256；study_start_at（UTC，晚於凍結，歷史匯入另標，不必等月初）；review_dates（固定日曆，至少涵蓋24個月）；arms；features；label_names；label_calculator_sha256；horizons_hours（固定、唯一1～168整小時）；observation_anchor；costs。

features 是事前白名單，每欄必有 definition、code_sha256、dtype（number/category/boolean）、lookback_bars（例如 `{"1h":21}`）。禁止明確的事後欄位名稱；這不能代替對外部計算程式的語意審查。costs 固定10bps單邊費用、5/15bps滑價、1000USDT及0.02風險預算。P1-08C 的已提交範例只用於合成測試，不是正式假說。

## evidence（已接收的正規化 K 線）

| 輸入欄位 | 型別及規則 |
|---|---|
| symbol／timeframe | 固定八種USDT；1h／4h／1d |
| open_at／close_at | UTC開棒／結束排他時間；close ≤ 系統接收時間 |
| ohlcv | open/high/low/close/volume/quote_volume；價格正、有合理幾何，量非負；quote_volume 可 null，不能用 close×volume 假裝真成交額 |
| source | id、uri、market_event_at、published_at、unknown_reason；未知發布時間維持 null 並附原因 |

系統另寫 received_at、normalized_input_sha256 及 quality_flags（零量、未知發布／事件時間／成交額）。normalized hash 證明**所輸入正規化內容**未改，並不認證來源真實，也不表示持有交易所原始 wire bytes。source.raw HTTP 收集／正規化對照及可信 receipt 整合仍須另審；本輪的公開探測有另外保存原 response 與 hash。

同source id的同symbol/frame/open拒絕重複；更正必須另具source revision身分，原值保留。登記使用到的多根特徵證據須連續且非零量；不足改未知，不補造。已知 source publication 必須在收盤後且不晚於receipt；market event若提供不得是未來。無發布時間時，本系統receipt僅是確知可用的上界，不是延遲的精確估計。

## candidates（可觀測機會與版本）

| 輸入欄位 | 規則 |
|---|---|
| market_event_key／symbol／timeframe | 外部事件鍵、幣種、決策週期；規則版本由protocol固定 |
| formed_at／confirmed_at | 形成必須引用已接收的該週期收盤；確認需另有已收盤證據；未確認為null |
| state／stance | pending/confirmed/invalid；long/short/watch。pending及invalid必須watch、不得附確認時間 |
| arm | protocol預先登記研究臂 |
| observation_start_at | 固定為登記後的下一個1H邊界，嚴格晚於寫入時鐘；是研究行情窗，**不是假設入場成交** |
| evidence_ids | 已存在 evidence 紀錄SHA256；同幣種，不能引用未來或label |
| features | 白名單所有欄位；各含 value、known_at、evidence_ids、unknown_reason；缺值null有原因；非缺值依dtype、連續lookback、receipt≤known_at≤登記驗證 |
| quality | data_gaps／delays／unknown_fields字串清單；不偷偷補齊缺值 |
| previous_candidate_sha256 | 首版null；更新必須引用同event_id最新版本，不接受過期分叉 |

系統新增 event_id、revision、registered_at、data_available_at、registration_delay_ms、source_rule、label_names／horizons、research_only、mode、forward_status及 feature_calculation_verified=false。event_id由研究鍵、來源規則、market event key、幣種、週期與正規化形成時間計算；revision不改event_id。source key本身仍需上游避免重複命名；不是相互獨立市場事件的證明。

新增確認不能改舊pending；新版本重新凍結自己的未來研究窗。已登記舊窗口及觀望狀態保留。沒有策略證據時保持watch，不補造entry/SL/TP。候選並非可交易訊號，本層不新增訊號生成器。

## labels（只能事後追加）

輸入 candidate_sha256、horizon_hours、values（只能含凍結label_names）、status、evidence_ids、intrabar_order、limitations。引用確切候選版本，不接在模糊的「最新訊號」上。窗口結束前禁止寫入；complete需全窗口同幣1H連續證據、無零量且values全非null。incomplete須全null並附原因；未來不得把它們當成輸贏。即使標籤寫入時已滿72h，也不能回填原候選。

系統另寫 observation_start_at／end_at、label_completed_at、calculation_code_sha256、calculation_verified=false、ohlc_intrabar_path_unknown=true。只接受unknown／not_assessed順序；本層不認證外部標籤算式或實盤成交。唯一鍵為candidate_sha256+horizon；已封存label不得覆寫。本v1沒有標籤更正API，發現錯誤須停止評分、保留原件並另凍結修正版流程，不能刪掉重寫。

## 本地操作

`python p1_08c_demo.py --out-dir NEW_EMPTY_DIR` 僅跑合成端到端驗收。真實研究必須另提供經審查的新protocol，沒有預設正式假說。

```sh
python p1_08c_registry.py init --root NEW_REGISTRY --mode prospective --input reviewed_protocol.json
python p1_08c_registry.py ingest --root NEW_REGISTRY --input closed_evidence.json
python p1_08c_registry.py register --root NEW_REGISTRY --input candidate.json
python p1_08c_registry.py checkpoint --root NEW_REGISTRY > checkpoint_to_external_store.json
python p1_08c_registry.py verify --root NEW_REGISTRY --anchor trusted_external_checkpoint.json
# 觀察窗口結束後，另行稽核計算程式與完整新證據，才執行：
python p1_08c_registry.py label --root NEW_REGISTRY --input reviewed_label.json
python p1_08c_registry.py audit --root NEW_REGISTRY
```

指令不連交易所、不接受API Key、不會下單。單寫入鎖殘留時直接拒絕；需人工先驗證checkpoint與備份，再另案復原，不自動清鎖或刪掉疑似壞紀錄。此v1每次重驗全鏈，適合小量前瞻研究，不是大量行情平台。
