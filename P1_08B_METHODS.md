# P1-08B｜價格路徑與有效波動事前規格

2026-11-17（Asia/Taipei）。基準P1-08A `973791ea5be0170a6b41edfeb9f6756426314539`，Run43974884735／Artifact14280058200，ZIP SHA256 `6f7120ca7c3866a2126afab4745cb8440f7562a45dc96ffb142a8fdd030e2380`。本規格先提交，再執行新路徑統計。不重跑策略、不調ATR/RR/SL/TP/72h/AI，不產生新的策略或資金曲線。

## 母體與R的單位

重用160封存帳戶、32,232個含profile重複的交易列，以及13,548列孤立情境。真正機會母體為25,176個policy-event列／8,005個唯一事件ID；未eligible沒有等價交易或路徑label，不補造。不同模型、情境、連續及分段帳戶不合併推論。

`R0=abs(entry−original_SL)`為原始純價格風險；`R_model=原planned_risk/q`為原成本內含的正常停損單位風險，以原5/15bps公式獨立對帳。MFE/MAE同時輸出兩種gross正規化，不能把純價格R與原淨R混稱。原SL與q不變。另列MFE/prebreakATR及價格百分比，避免SL分母本身產生假關聯。

## 三種路徑，絕不混為可實現報酬

1. **完整原持倉**：到原exit；沒有改退出時點。
2. **6/12/24/48/72h持倉前綴**：在原exit截斷，標示early_exit，不是足量存活至h小時。
3. **固定h小時行情帶**：不因原exit截斷，因此可能包含止損後／出場後價格；一律明標counterfactual及post-exit旗標，不進可實現盈虧。不能跨2023／2025／凍結截止界線。

方向sign=+1多、−1空，`MFE=max(0,sign*(price−entry))`、`MAE=max(0,−sign*(price−entry))`。已知全程持有棒的H/L可用；限價入場同棒及棒內出場棒只給寬鬆OHLC envelope，上下界可能不相等，不能當真實可成交極值。下界由已知entry／exit、可確認持有時段的報價給出。缺棒／零量時真上界未知，僅保留已觀察下界及缺口原因。stop_gap與資料缺口的開盤退出按原紀錄時點，不誤取下一小時全棒。

`giveback=MFE_gross_R−endpoint_gross_R`，成本另列；MFE−netR含成本，不可全部叫價格回吐。MFE處按原成本公式算出的net_quote_potential只是事後上限算式，沒有假設能在該高低點成交。實現淨R只讀原檔，不二次扣費。

## 時間與順序

固定診斷振幅為0.5/1/2R0，以及成本損益兩平價、原TP、原SL，全部輸出，不挑最好幅度或期限。OHLC觸及時間為[棒open,棒close]；若開盤報價已越線可給quote時間。入場／出場不確定棒只可能觸及，除非已知報價確認；同棒TP/SL、同棒MFE/MAE先後一律未知。不同完整棒的先後可知。所有時間、MFE/MAE及未來到達均為**posthoc label**。

逐小時close效率為`abs(endpoint−entry)/sum(abs(consecutive closes))`；近進場比例為已觀察close落在±0.5R0的比例。這不是盤中停留時間，無法由OHLC知道價格在區間內待了幾分鐘。到期交易的MFE首現時段固定為0–6、6–12、12–24、24–48、48–72h，並列各期限累積MFE，不把峰值時刻當最佳可執行退出。

72h退出的描述分類依序：資料／順序未知；連成本都不足；MFE<0.5R0；MFE≥1R0且終點有利位移≤MFE一半的回吐；close效率≤.2；其他／部分進展。類別是路徑表型，不是已識別的經濟根因。所有連續指標及其他類別仍保留，不隱藏難分類事件。

## 決策時已知的特徵

20D兩臂原registration為首次突破棒收盤。prebreak cutoff=該棒open；壓縮、ER與ATR只看更早已收盤棒；實體／影線、突破幅度與該棒RVOL到registration才知道。前20日level以**突破棒所屬UTC日**計，不因registration跨午夜而偷換成下一日價位。

壓縮比=`ATR14_pre / mean(嚴格更早20個ATR14)`，用P1-08A精確14TR和，分母排除當期preATR，<1為壓縮、≥1非壓縮，缺史／缺棒未知。實體比abs(C−O)/(H−L)，上／下影線分別(H−max(O,C))/(H−L)、(min(O,C)−L)/(H−L)，0 range未知。突破close／extreme對原20D level的有方向距離除以preATR；位置為(C−20Dlow)/(20Dhigh−20Dlow)，不截斷範圍外值。

重用P1-08A的三週期趨勢、ER、ATR、RVOL及正常風險成本占比。非20D訊號家族的「突破棒」未有等價欄位，本輪不以最近棒假造，明列不可用。拒絕事件保留registration快照，沒有假想entry。進場棒最終實體／RVOL須t+1H才可用；未確認pivot亦不能提前用，列於deferred catalog而不填特徵值。特徵表與path／PnL標籤分檔，來源狀態、exit、PnL、MFE、MAE不入feature whitelist。

## 探索關聯與最多兩個假說

全部固定分組輸出：壓縮1、實體.5、突破距離ATR .5/1、ER .2/.4、RVOL .8/1.2、位置區間、三週期MA、成本占比.25。這些是先固定的診斷分組，不修改策略。所有組含不足／未知及零樣本，不依Test挑最佳。

僅預登記H_COMPRESSION與H_BODY，以T20_CONT全部eligible long孤立事件為主；A分別是壓縮<1、突破實體≥.5，B為另一組。主要label是**固定24h行情帶**的MFE_R0，明列可能含原exit後價格；同列MAE_R0、MFE/preATR與價格百分比，不能藉放大波動或改風險分母認證優勢。

假說形成的最低證據：Train及Validation每臂≥30，兩段ΔMFE_R0及ΔMFE/preATR皆正、ΔMAE_R0不正，全期95%共同28日區塊MFE差下界>0；BTC／其他七幣／既有Test各臂≥10且方向不反轉，否則不足／不推薦。這不是正期望驗收。現有Test只作探索一致性／否證；不得宣稱獨立OOS。若0就報0，不改門檻或補第三個假說。

重用28日共同日曆區塊1000次、seed808，跨幣同時抽；不同scenario不當新樣本。首尾部分及空區塊保留，區間依賴區塊假設，不能保證長期獨立。新前瞻若ΔMFE不正或MAE增加，則否證預測；需保留同規格、正常化、完整候選及品質旗標，不可藉事後退出結果重新接納。即使關聯成立，仍需另行前瞻及可執行成本驗證，不能直接變成進場規則。

## 成本與未知

沿用10bps/side、5/15bps滑價、1000紙上本金及2%正常風險。對同一原事件只做成本算式與原帳戶對帳，不生成新曲線。成本／MFE於MFE=0時未定義，不硬填0。OHLCV不能重建spread、depth、CVD或精確成交；hypothetical short另表，不冒稱現貨可放空。

完整輸出應包含來源版本／SHA、原帳戶與機會對帳、唯一path及profile映射、held與counterfactual各期label、觸價時間區間、timeout分類、feature/deferred catalog、全分組、共同區塊不確定性、圖表及繁中P1_08B_ACCEPTANCE.md。main、P0／Genesis、正式策略及既有PR只讀，無實盤／授權／槓桿執行，不自動合併。
