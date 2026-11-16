# P0-ORDER-STATE-08｜八幣種前瞻資料每日增量封存（研究用途）

**專案：** `danielwang0918-design/btc-market-lab`。與 P0-07 已註冊前瞻協議相容，原策略 `mtf-bos-ob-retest-v1` 完全不變。所有交易決策仍僅用 **已收盤 1D／4H／1H**，不採用 1m、5m、15m；本階段甚至不產生任何進出場訊號，只保存行情證據。

## 範圍與日期

- 凍結觀察起點：`2026-11-17T00:00:00Z`；固定截止：`2028-01-01T00:00:00Z`（不含）。
- 固定八幣種：BTC、ETH、SOL、ZEC、XRP、BNB、NEAR、DOGE，全部 Binance Spot USDT。
- 原始來源：GitHub Run `43820018876` 之 `multi-asset-input-43820018876`（24 個 CSV），每檔比對 P0-07 固定 SHA256；Genesis 來自 Run `43845519180`，紀錄 SHA256 `f96c42b9e1d3dc1224cdf01d0f05ec54be90a4e6cc60e9d535770be9321249f0`。
- 首次初始化只保存 24 個凍結歷史 CSV 的 ZIP；之後依 UTC 日期只新增當日封存的 **24 份 CSV delta**，不每日覆寫／重新壓縮整份原始歷史。
- 每次結束時保留 `days/YYYY-MM-DD/checkpoint.json`（逐檔 SHA256、完整 K 線時間戳），`latest.json` 僅是與最後一份 checkpoint 相同的快速指標。

## 安全與驗收

1. `bootstrap` 必須驗證 24 個歷史檔的位元 SHA256、Genesis 原始雜湊及原始策略程式 Git blob SHA1；任何差異直接停止。
2. 每次 `daily` 先重建全部歷史+先前 delta、從 Genesis 逐日核對紀錄雜湊鏈，再抓取所有幣種已完整收盤的新 K 線，確保每個週期時間戳連續、來源一致、無重複／缺棒。任一幣種失敗則拒絕封存整批資料。
3. 重跑相同日期只驗證原封存，不向交易所重新抓取，也不重寫任何舊日紀錄。延後執行時會從最後保存的 K 線時間接續補抓；不把未關閉日線提前當成已收盤。
4. GitHub 工作流程的寫入目標應限定**獨立封存分支** `research/prospective-archive-v1`；不自動合併 Draft PR #6，也不向 `main` 寫入交易結果或憑證。
5. **每日資料封存不是每日績效計算。** `daily_data_archived_not_scored`、`forward_trade_count: null` 明確表示尚未用正式策略結算前瞻交易；未來應另開獨立批次回測，絕不宣稱 0 筆交易。
6. 本機與 GitHub 的雜湊鏈可檢查偽造／改寫，但**若攻擊者能同時重寫整條 Git 歷史，仍須額外保存 Git commit SHA 與外部獨立備份**；GitHub branch 非絕對不可變儲存。

## 使用方法

```bash
python -m unittest -v
python prospective_daily_archive.py bootstrap --lock PROSPECTIVE_LOCK_V1.json --repo-dir . --seed-source /path/frozen-input --genesis /path/prospective_genesis.json --archive-dir /path/forward_archive
python prospective_daily_archive.py audit --lock PROSPECTIVE_LOCK_V1.json --repo-dir . --archive-dir /path/forward_archive
# 必須在指定 UTC 日期 00:00 之後才可執行，且提供實際已收盤 1H／4H／1D：
python prospective_daily_archive.py daily --lock PROSPECTIVE_LOCK_V1.json --repo-dir . --archive-dir /path/forward_archive --asof 2026-11-17T00:00:00+00:00
```

## GitHub 啟用說明

- 此階段先於研究分支使用 `.github/workflows/prospective-daily-archive-preflight.yml` 重建 Genesis，驗證全 24 份凍結檔與所需程式。
- 如要透過 GitHub Actions `schedule` 定期運作，**排程定義必須存在於 GitHub 預設分支 `main`**；在研究分支建立 `schedule` 不會每天執行。
- 任何 `main` 排程必須指向已通過驗收的固定 research commit SHA（不可無條件跟隨移動分支）、在首次啟動時立即保存 frozen seed + Genesis 到獨立封存分支，並使用 `contents: write` 推送該封存分支。倘若 GitHub 權限阻擋推送，應報錯而非宣稱每天自動完成。
- 不存儲 API key；只使用 Binance 公開 K 線 API；不進行真實下單。GitHub Scheduled Actions 有延遲、失敗或被停用的可能，因此後續必須監控成功記錄與連續性。