# P0-ORDER-STATE-07｜前瞻驗證封存協議

**2026-11-16 建立；正式策略不修改。** 這是前瞻模擬研究，不是實盤、自動交易或報酬承諾。PR #6 維持 Draft，未合併到 `main`。

## 研究鎖定（在未來資料到來之前）

- **唯一前瞻開始時間：2026-11-17 00:00:00 UTC**（台灣時間 11/17 上午 08:00）；結束時間預設 **2028-01-01 00:00 UTC（不含）**。
- 名單固定為 2026-11-16 確認符合歷史覆蓋的 BTCUSDT、ETHUSDT、SOLUSDT、ZECUSDT、XRPUSDT、BNBUSDT、NEARUSDT、DOGEUSDT。沒有因 Test 表現改選；ASTER、NVDAB 持續不列入。固定選幣方式也不代表當年歷史點位真實可投資名單。
- 原始 Formal `mtf-bos-ob-retest-v1`：只有已收盤 1D／4H／1H、OB 回踩、RR≥1.5、OB 最長 168h；費率 10bps/side、滑價 5bps/side；同根 SL/TP 以 stop-first 模擬；只用 Spot K 線作研究，**不是實際可驗證的期貨空單回測**。
- 鎖住核心程式檔案的 Git blob ID、歷史來源 GitHub Actions Run **43820018876**、8×3 原始 CSV 的 **SHA256 與原始檔長度**、原 Train/Validation/Test 總表。更動規則、轉換單位、刪除輸錢幣種或回頭更改曾觀察的 K 線均會讓驗證拒絕發布。
- 前瞻進場訊號與模擬成交時間均不可早於上述開始時間。開始時不承接原來舊 Test 的持倉或掛單；報告只包含**真實模擬出場完結的交易**，每日截點造成的假性 `split_or_segment_end` 不納入。
- 主要研究指標：已完成交易的平均淨 R；次要：各幣勝率、PF、同週重疊、掛單取消分布。至少 **200 筆完成交易及 26 個不同入場 UTC 週** 才顯示完整描述性讀數；這**不是**統計顯著、策略有效或能直接實盤的門檻。2028-01-01 才進行固定日曆終點的評估，期間報告不能拿來選參數或宣告 Edge。

## 可重現實作

- `PROSPECTIVE_LOCK_V1.json`：固定時間、8 幣名單、參數、來源與每個 CSV 的前綴 SHA256／位元長度。檔案整體 SHA256 亦嵌入 `prospective_validation.py`，防止默默改規則。
- `prospective_validation.py preflight`：檢查策略與原檔完全一致、驗證 8×3 凍結 K 線原始位元、確認前瞻日期**尚未開始**後產生 genesis 証書。**不能在前瞻日期開始後補發「事前註冊」**。
- `prospective_candle_append.py`：從 Binance 公開市場 API **只抓剩餘的已收盤 1D/4H/1H K 線**，先驗證完整、不缺根再附加；不呼叫帳號 API／不下單。
- `prospective_validation.py observe`：對照前一個雜湊鏈紀錄檢查歷史前綴與後續紀錄無改寫；使用正式引擎完整重建已收盤 K 線訊號；輸出當次 checkpoint 及實際已完成的模擬交易。每次新 checkpoint 都必須帶上先前檔案，否則拒絕。
- 鏈式記錄只是「提供舊檔可查時」的防竄改措施，**沒有外部不可變存證，就無法防止整條鏈被重建**。請保存每期 GitHub Artifact 或獨立唯讀檔案庫。

## 手動資料更新流程（未來才有新樣本）

```bash
# 1. 先從原 GitHub Run 43820018876 下載並原樣保留 1D/4H/1H CSV；勿重寫來源 CSV。
# 2. 將前次 checkpoint（首次為 prospective_genesis.json）保留在安全位置。
python prospective_candle_append.py --lock PROSPECTIVE_LOCK_V1.json --data-dir data/prospective_07
python prospective_validation.py observe --lock PROSPECTIVE_LOCK_V1.json \
  --data-dir data/prospective_07 --prior-record previous_checkpoint.json \
  --out new_checkpoint.json
```

**重要作業限制**：GitHub Actions 的排程只會在預設分支執行；目前新 workflow 仍在 Draft PR 分支，**本次只用 `push` 觸發一次前瞻事前註冊驗收，未設定每天自動跑**。不能因為前瞻框架已提交，就宣稱未來每日自動觀察已經啟用。後續要開固定排程須另取得正式部署方案授權，絕不為了排程自動合併 PR。

## 風險揭露

1. Spot OHLC 的回踩假設不等於真實 limit fill，且缺少深度、費率變動、資金費率與期貨多空可成交性。
2. 原策略多幣種存在共同市場因素；331 筆舊 Test 的勝率與正負期望**全部是歷史探索**，不能回溯稱為未來驗證。
3. 即使前瞻 R 後來為正，也必須另外做跨幣相關性、風險預算、實際成交可行性與再驗證才有資格討論實盤。