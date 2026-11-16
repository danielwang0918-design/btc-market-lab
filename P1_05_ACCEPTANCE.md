# P1-05｜離線訊號及資金回測驗收索引

本專案永久定位為訊號分析與量化研究，不是自動交易系統；見 `AGENTS.md`。本階段只做歷史回測及模擬，不建立實盤 API、帳戶交易授權或自動開平倉。

## 來源與事前規格

- 以 PR #14 `515590e122880207e23936ae9c65905ad13b988e` 為基準，獨立 `codex/p1-05-portfolio-backtest`。
- 事前規格 commit `23a0b5f`，`P1_05_PROTOCOL.json` SHA256：`56265d7e7aa8c4fb5aeeb2b49442db96df0291b8df8bf667c37bda7c6c0b0690`。規格先於績效，沒有搜尋參數。
- 原始資料／交易 [Run 43820018876](https://github.com/danielwang0918-design/btc-market-lab/actions/runs/43820018876)，交易 Artifact `14206217921`。原交易 CSV SHA256：`cc9ae34de758569306e24d7e8cdec79006ef502effeeadc85425e09547a0d0c0`。
- P1-04 [Run 43867291228](https://github.com/danielwang0918-design/btc-market-lab/actions/runs/43867291228)，manifest SHA256：`e740acccae1b35a115a56b8d2c26167a3fc61efaa791e208cec7daad1e22a0b9`。
- P1-QUALITY-01 [Run 43864680608](https://github.com/danielwang0918-design/btc-market-lab/actions/runs/43864680608)，manifest SHA256：`d8d67cf15b6a8611dfbbdb2d21566bde65fa7b4a43de54f3d6d7f300c9b6c5b6`。
- 八幣 24 個凍結來源依原 P0 lock 逐檔驗證，不改資料。原始引擎 Git blob `fa5aa27cbb70b008514144087a8387b58a82b41c` 保持不變。

## 獨立重現及資金限制

從原始交易檔已獨立重現最新 R 重播基準：1,000 筆、210 勝、平均 −0.24972920793R、最終 **4.226477219422668… USDT**。原 R 已含成本，沒有再扣費。舊版用未來出場排序所得 3.24 USDT 不作最新基準。

完整 canonical 訊號由未修改原 P0 引擎對八幣全歷史重新產生，並以原引擎逐筆對帳封存 1,340 筆交易；新資金模型不限於已成交子集。所有原始候選與各方案的觀察、等待、拒絕、成交模擬均有清單。

共同日曆為 2020-10-15～2026-11-16 UTC，包含連續及 Train／Validation／已開啟 Test 的獨立 1,000 USDT 帳戶。每筆風險按停損距離、雙邊費用及正常滑價預算 2%，受全額現金限制、全球單倉、同小時不重新進場、每次最多 1,000 次建倉，不循環補造交易。

固定政策 A/B/C/D/E_ob/E_2b，見 `P1_05_RESEARCH.md`。B 用 P1-02／03 同公式的實際入場 as-of 適配器，C 用前一根已收盤 1H 同向型態及 RVOL≥1.2，D 僅為組合對照，兩種 E 是獨立確認政策。不能把不相關型態事件的既有標籤硬套到 BOS 入場。

## 必須分開解讀的模型

歷史 LOT_SIZE／MIN_NOTIONAL、借幣及深度資料缺少，嚴格 Spot 模型拒絕未知規則，不把 0 筆、保留本金解讀為優勢。另報有現金限制的連續數量純長倉 **conditional OHLC** 模型，及 stop-first／target-first、15 bps 滑價壓力與獨立假想 1x 多空；全部明列未驗證交易所可成交性。它們是歷史模擬，不是真實委託或槓桿操作。

2% 是正常停損模型預算，不保證實際虧損不超額。跳空及壓力結果逐筆列出。缺口不得回填過去價格成交，無量終點不得假裝成功平倉。最大回撤是含退出成本的 1H 收盤估價，不假稱 tick 級最大回撤。

## 完整交付與判讀

每次實測的完整中文結果在 Artifact `results/P1_05_RESULT_ZH.md`，以實測 CSV 為準，包含：

- `strategy_comparison.csv`：120 個固定帳戶對照的筆數、勝負、勝率、平均 R、USDT／R 淨期望、USDT PF、初終本金、最大回撤／連虧、費用、滑價、在市比例。
- `by_year.csv`、`by_symbol.csv`：全部日曆年度、固定八幣與零交易格。年度同列收盤估價損益與已實現交易損益，避免誤認兩者相同。
- `runs/*__trades.csv`、`*__equity.csv`、`*__signals.csv`：逐筆數量、成本、風險、現金及所有未篩選與拒絕訊號。訊號含時間戳、區間、SL、TP1、RR、依據與失效；原策略無 TP2，明列不足，不補造。
- `equity_*.png`：同一日曆起訖的全部模型／時段曲線；未持倉每日保留相同餘額，持倉時逐時估價與異動均記錄。
- `canonical_reference.json`、`legacy_reproduction.json`：原引擎 1,340 筆及使用者最新 1,000 筆基準對帳。
- `independent_account_audit.json`、`p1_05_manifest.json`：逐筆現金／費用／風險／單倉／日曆核對、全部來源與程式 SHA256。

比較同時看勝率和淨期望；勝率提高但期望惡化不算改善，少交易保住本金也不算正優勢。120 個帳戶是固定比較情境，不能加總成獨立交易。2025–2026 Test 已反覆查看，僅為回顧比較；沒有新 OOS、因果獲利認證或足夠真實交易可執行性資料。

舊 H5 的實際程式與附件未提供，無法逐筆認證其 81 筆／431.523101 USDT；本次新政策也不冒稱與 H5 相同。下一步應先補歷史規則／可用成交證據，事前鎖定獨立訊號規則，等待全新前瞻資料，不以本批 Test 繼續尋優。
