# P1-04 成交量／RVOL 研究中文驗收報告

**結論：通過 P1-04 特徵正確性、完整分層及可重現性研究驗收；不認證可執行交易策略、真正 OOS 或正期望值。**

版本 `p1-volume-rvol-v1`；基礎為 PR #11 `research/p1-01-review-propagation`，commit `78d141a4d5fcb03eb2c56a4da369d1b3194aea63`。開發於獨立 `codex/p1-04-volume-rvol`，沒有改寫既有 PR #6～#11、main、正式策略、P0／Genesis 或凍結歷史。

## 開發順序與驗證

先讀取 PR #6／#7／#8／#9／#10／#11 與整合 Run `43862040301` 的紀錄，確認基準 346 項測試通過。先提交規格、固定門檻及測試（commit `ed26ba0`），再實作研究；首次 P1-04 績效執行前，規格已固定。

新增 40 項測試，完整本地 Python 測試 **386／386 通過**；本地 Python 3.12、GitHub workflow 使用 3.10。測試包括分母排除當期、prefix invariance、缺口重置、零均量、零當期量、極端值滾出時保留微小量、精確門檻、1H+ 限制、來源 hash／截止、完整 horizon、不可評估狀態、分層空格、PF 語義與未四捨五入的跨幣正負計數。

另有獨立驗收器，使用 **Fraction 直接重算前 20 根**，不呼叫研究程式的 rolling RVOL 算法；另寫計數與績效累加器，不使用研究程式的 Stats 來自我驗證。逐格核對四種完整交叉報告、分時段摘要、同層比較與跨幣種穩定性。

PR #11 資料介面在本次實測中相容，**不需修改任何既有介面**。P1-01 價格、成本、結果狀態及 P1-02／03 分類的前後差異全部為 0；沒有用重新計算或重新分類偷換既有基準。

## 固定方法

`RVOL[i] = volume[i] / mean(volume[i−20:i])`，使用型態完成棒的已收盤量；前 20 根均量排除當期。低量 `<0.8`、正常量 `[0.8,1.2)`、高量 `≥1.2`；歷史不足、缺口暖機、零前期均量分別保留。RVOL≥10 只標記極端值、不截尾、不刪除，全部留在 high。

量能是 Binance Spot 基礎資產 volume，只在同幣、同週期內做比率。1H／4H／1D 的 20 根窗口分別是 20 小時／80 小時／20 天。特徵在訊號完成棒收盤才可知；位置及 MA200／ATR 沿用型態開始前已確認的分類。

不新增進出場、停損／止盈或量能濾網。固定 24／72／168 小時結果仍直接複製 P1-01，維持單邊費率 10 bps、單邊滑價 5 bps。

## 來源及 SHA256

| 來源 | 鎖定證據 |
|---|---|
| 凍結 OHLCV | [Run 43820018876](https://github.com/danielwang0918-design/btc-market-lab/actions/runs/43820018876)，24 個 CSV 的 hash／bytes／筆數依 P0 lock |
| 修正版 P1-01 | [Run 43857787070](https://github.com/danielwang0918-design/btc-market-lab/actions/runs/43857787070) 的官方 `after` |
| 已驗證位置與狀態 | [Run 43862040301](https://github.com/danielwang0918-design/btc-market-lab/actions/runs/43862040301) 的 `zone`／`regime`／`audit` |
| P0 lock SHA256 | `42c0d049a8f277934b2908767869e1c6830009c47f346463140c9abac7549dbf` |
| P1-04 事前規格 SHA256 | `1b30c5ac93d8c87258e884287ec44f5850bcf35b8f99970ca649723ea19ba68a` |
| 研究程式 SHA256 | `e1b4521716e552582a0c1bd8f61e0a47ae7f6094f24d4f8fa1bbe8de095e1e60` |
| 獨立驗收程式 SHA256 | `e2efbac322526e04499f4b54c8c252ea46014e9a5bc8b455db807dffc0a7c53a` |

完整輸入、相依程式與輸出 hash 均寫入 `p1_04_manifest.json`；獨立核對 hash 在 `p1_04_acceptance.json`。ZIP 解壓後重新封裝會改變容器 hash，24 檔原始內容 hash 必須一致；不重新抓取更新過的行情。

## 基準對帳與資料完整性

| 項目 | PR #11 基準 | P1-04 | 差異 |
|---|---:|---:|---:|
| 唯一型態事件 | 106,433 | 106,433 | 0 |
| 期限觀察列 | 319,299 | 319,299 | 0 |
| 可評估 | 311,873 | 311,873 | 0 |
| 缺口／邊界／追蹤不足 | 7,402 | 7,402 | 0 |
| 成交不足 | 24 | 24 | 0 |
| 位置／MA200／ATR 分類變動 | — | 0 | 0 |

原始 16 欄 × 319,299 列，共 **5,108,784 個原欄位值**逐值相同。三種期限共用同一個事件 RVOL；沒有把原成交不足變成零報酬或輸贏。

24 檔共 **717,638 根已收盤 K 線**；所有 SHA256、bytes、筆數、來源、symbol、interval、UTC 對齊、close_time、截止、遞增與重複檢查通過。原始 **28 根零量棒、201 段缺口、667 根缺失來源棒**完整揭露，沒有補棒。缺口是跨來源檔計數，不能當作 667 個獨立小時。

## 實測 RVOL 事件數

| 分組 | 唯一事件 |
|---|---:|
| low | 50,166 |
| normal | 27,733 |
| high | 27,994 |
| insufficient_history | 59 |
| gap_in_lookback | 481 |
| zero_prior_mean | 0 |
| 合計 | **106,433** |

共有 **122 件 RVOL≥10** 的事件，完整保留。零前期均量在本批事件中為 0，仍保留於所有報表，不能省略零樣本格。

## 全時段合併描述值

以下混合固定八幣種、五型態、三週期；採事件等權，沒有處理同時資金占用。不是投資組合績效，也不是可以直接執行的交易筆數。

| 小時 | RVOL | 可評估事件 | 平均淨紙上報酬% | 方向有利% | 淨正報酬% | 描述性事件 PF |
|---|---|---:|---:|---:|---:|---:|
| 24 | low | 49,798 | −0.302617 | 48.505964 | 43.827061 | 0.827220 |
| 24 | normal | 27,545 | −0.332336 | 48.473407 | 44.341986 | 0.820845 |
| 24 | high | 27,847 | −0.185322 | 48.870614 | 44.855819 | 0.900733 |
| 72 | low | 49,155 | −0.285265 | 49.555488 | 47.171193 | 0.905868 |
| 72 | normal | 27,224 | −0.197045 | 49.698795 | 47.498531 | 0.934994 |
| 72 | high | 27,446 | −0.108837 | 49.402463 | 47.194491 | 0.964982 |
| 168 | low | 47,998 | −0.164991 | 49.772907 | 48.245760 | 0.965029 |
| 168 | normal | 26,545 | −0.116657 | 49.523451 | 48.080618 | 0.975407 |
| 168 | high | 26,792 | +0.094776 | 50.000000 | 48.622723 | 1.019803 |

歷史不足與缺口暖機也可能有價格結果，完整結果在 `P1_04_RESULT_ZH.md` 的 18 列總表與 54 列時段表中，沒有因 RVOL 未定義而刪除；零樣本／零可評估的比例與報酬均為 null。

高量 168h 在合併資料為正，**24h／72h 仍為負**，且 168h 的高量結果只有 4／8 幣種平均為正。按相同全時段、全型態、全週期聚合，每幣平均為正的幣數如下，八幣全部保留：

| 小時 | low | normal | high |
|---|---:|---:|---:|
| 24 | 0／8 | 0／8 | 0／8 |
| 72 | 0／8 | 1／8 | 2／8 |
| 168 | 2／8 | 3／8 | 4／8 |

本資料顯示描述差異，但不足以支持「量能確認已產生跨幣種穩定且可交易的優勢」。不把 168h 選為新策略，不重新調整 RVOL 門檻。

## 完整交付清單

| 檔案 | 內容 |
|---|---|
| `p1_volume_rvol.py` | 量能來源驗證、固定 RVOL、全部交叉統計 |
| `p1_volume_rvol_audit.py` | 直接 Fraction 參考、原欄位與分類保留、逐格驗收 |
| `test_p1_volume_rvol*.py` | 40 個新增測試；完整 suite 386 項 |
| `P1_04_VOLUME_RVOL_PROTOCOL.json` | 首次結果前已提交的固定規格 |
| `p1_04_features.csv` | 106,433 列唯一事件特徵 |
| `p1_04_observations.csv` | 319,299 列，原 P1-01 欄位完整保留 |
| `p1_04_pattern_rvol.csv` | 6,480 組 |
| `p1_04_pattern_location_rvol.csv` | 38,880 組 |
| `p1_04_pattern_ma200_rvol.csv` | 25,920 組 |
| `p1_04_pattern_location_ma200_atr_rvol.csv` | 622,080 組 |
| `p1_04_matched_rvol_comparison.csv` | 同層全 RVOL 與 normal 的 6,480 組比較 |
| `p1_04_symbol_stability.csv` | 810 組，包含每幣覆蓋、正負均值數及跨幣最小／中位／最大均值 |
| `p1_04_rvol_by_split_horizon.csv` | 全部六種 RVOL 分組、時段、期限的 72 列摘要 |
| `p1_04_source_integrity.json` | 逐檔來源、零量與缺口完整清單 |
| `p1_04_manifest.json`、`p1_04_acceptance.json` | 版本／全部 hash／對帳／獨立驗收紀錄 |
| `P1_04_RESULT_ZH.md` | 由每次實測自動產生的完整中文結果 |

四種完整視圖共 **693,360 組**，每種各自對回 319,299 列與三種評估狀態。不能把四個視圖加總成更多事件。完整保留全部五型態、八幣種、三週期、三時段、三期限及六個 RVOL 分組。

GitHub workflow `.github/workflows/p1-volume-rvol.yml` 執行完整測試、核對事前規格 commit、下載既有來源、全研究與獨立驗收。Artifact `p1-04-volume-rvol-evidence-<run_id>` 含 **inputs、results、測試日誌及規格證據**，保留 45 天，可在不重新抓行情下重現。

## 尚未解決的研究限制

- **交易系統淨期望值／PF／最大回撤尚未驗證。** 本次平均是等權事件淨報酬樣本均值，PF 為等進場名目金額的正負淨報酬加總比；原價值不是獨立交易或實際資金曲線。沒有負值時 PF 為 null 並附狀態。
- 沒有資金配置、重疊部位與可執行交易模型，因此實際交易筆數、投資組合最大回撤不定義；manifest 明列 null 與原因，不製造數字。
- 2025+ Test 已開啟，固定名單來自 2026 選樣，無新的獨立 OOS。跨幣、跨週期、同一事件三期限與相鄰窗口相關，不能當獨立樣本，也未以不適當的獨立假設提供顯著性或信賴區間。
- OHLCV 與固定費率／滑價不能證明次根開盤或期末收盤可成交；Spot 模擬空頭未含借幣、資金費率、保證金及容量。
- 未取得主動成交方向或訂單簿來源；**只有 OHLCV，不能精確重建 CVD 或訂單吸收**。

需要另案事前註冊部位、執行、風控與真正未來資料後，才能評估可交易策略。P1-04 本次只完成已授權的描述性研究與工程驗收。

## 離線重現

從 P1-04 Artifact 解壓後，於此分支執行（`EVIDENCE` 代表 Artifact 根目錄）：

```bash
python -m unittest -v
python p1_volume_rvol.py \
  --frozen-archive EVIDENCE/inputs/frozen-source.zip \
  --p1-dir EVIDENCE/inputs/reviewed-p1 \
  --context-dir EVIDENCE/inputs/reviewed-context \
  --lock PROSPECTIVE_LOCK_V1.json --out-dir research/p1_04_replay
python p1_volume_rvol_audit.py \
  --frozen-archive EVIDENCE/inputs/frozen-source.zip \
  --p1-dir EVIDENCE/inputs/reviewed-p1 \
  --context-dir EVIDENCE/inputs/reviewed-context \
  --lock PROSPECTIVE_LOCK_V1.json --out-dir research/p1_04_replay \
  --tests-log EVIDENCE/python-tests.log
```
