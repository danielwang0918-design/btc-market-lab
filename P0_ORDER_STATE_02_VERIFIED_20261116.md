# P0-ORDER-STATE-02｜OB 掛單閉環驗收

只使用 1H／4H／1D 閉合 K 線決策資料；無實盤下單、無勝率調整。

| 幣種 | Test 訊號 | 模擬成交 | 未成交取消 | 未結掛單 | 同根 OB＋目標取消 |
|---|---:|---:|---:|---:|---:|
| BTCUSDT | 153 | 36 | 117 | 0 | 1 |
| ETHUSDT | 188 | 44 | 144 | 0 | 0 |
| SOLUSDT | 178 | 43 | 135 | 0 | 0 |
| **合計** | **519** | **123** | **396** | **0** | **1** |

## 同根 OB＋目標價的 1H 時間順序分類

- target_already_at_open_before_zone_approach: 0
- zone_present_at_open_but_reference_fill_unproven: 0
- intrabar_target_vs_zone_order_unknown: 1

## 方法限制

- Only closed 1H/4H/1D decisions. No 1m/15m candles or trading API.
- Wick-touch modeled entry is not a verifiable executed limit order.
- OB+target both touched in one 1H candle do not reveal first-touch order.
- No recovered trade, profit or changed win rate is inferred from canceled setups.
- The Test period was previously inspected; this is not independent forward validation.
- Current-volume-selected symbol universe contains survivorship and selection bias.