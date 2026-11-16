# MULTI-ASSET-04 — 避免重跑與整合穩健性稽核

日期：2026-11-16。延續 MULTI-ASSET-03。此提交**不修改** `mtf-bos-ob-retest-v1`、交易規則、成本或實盤下單能力；PR #6 繼續保持 Draft。

## 確認的工程故障
原本重型 GitHub Actions 使用 `pull_request.paths`。同一 PR 的每次 push，path filter 可能因累積變更而繼續匹配，因此反覆啟動整段十幣種下載／回測。舊執行常被 `cancel-in-progress: true` 取消。上一個被取消的聚合 Artifact 全部是 `missing_result`，不能當成 0 交易或有效績效。

## 本次處理
- 四個長時間資料／回測 workflow 改用特定開發分支與 main 的 `push.paths`，僅在**本次提交**包含核心研究程式修改時自動啟動；保留 workflow_dispatch 入口。
- 不把 workflow 自身、文件或測試檔列入重型 push 路徑，避免只修改工作流程、說明文件或測試就重新下載所有 K 線。
- `cancel-in-progress: false` 避免新計算取消仍在執行的舊回測；同時注意 GitHub concurrency 最多容許一個 running 與一個 pending，非無限平行隊列。
- 單元測試 workflow 仍在所有 push 與 PR 自動執行。
- 多幣種固定曆期回測 Aggregate 後接著執行 `multi_asset_robustness.py`，產生 `multi_asset_robustness.json` 與 .md，與基準績效一起上傳 Artifact。
- Aggregate 基準若為 partial_failure，既有 CLI 會回傳非零碼，穩健性檢查不應在缺漏幣種時被宣稱為有效。

## 下一個結果驗收
以 `Binance multi-asset fixed-rule baseline` 完整執行且各幣種結果一致為前提，才讀取 Test 勝率、Expectancy、PF、每年分組、留一幣種、同週群聚 bootstrap 結果。被取消與 partial_failure 的工作不得混入樣本。

**注意**：GitHub 上新增或修改的 workflow_dispatch 入口，實際手動觸發的可用性仍受 GitHub 對預設分支 workflow 的限制。維持 PR 未合併，因此不宣稱已可從 main 手動觸發本次研究。此提交不會重跑已執行中的舊 workflow 定義；後續符合 push.paths 的新 run 才會自動包含穩健性步驟。
