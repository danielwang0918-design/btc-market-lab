#!/usr/bin/env python3
"""Predefined, chronological P1 studies for OB/FVG/2B confluence hypotheses."""
from __future__ import annotations

import argparse
import heapq
import json
import math
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import historical_backtest as p0

VERSION = "p1-confluence-study-v1"
VARIANTS = ("p0_ob", "ob_fvg_overlap", "ob_2b_confirmed", "ob_fvg_and_2b")
DEFINITIONS = {
    "p0_ob": "P0 comparator: closed 1H structure break + same-close OB; enter next 1H open.",
    "ob_fvg_overlap": "Require a same-direction 1H FVG confirmed from OB formation through signal close, with a non-zero price overlap with the OB zone. An opposite-direction FVG overlapping the zone is a conflict and blocks the trade.",
    "ob_2b_confirmed": "Require a same-direction 1H 2B event whose return-inside candle has closed within the signal candle or previous 2 closed 1H candles. Any opposite-direction 2B confirmed in that window blocks the trade. The detector only labels 2B after the return close.",
    "ob_fvg_and_2b": "Require both the FVG-overlap and confirmed-2B conditions above; either conflicting condition blocks the trade.",
}


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def p1_events(rows: list[dict[str, Any]], *, left_bars: int = 2, right_bars: int = 2,
              ob_lookback: int = 10, two_b_return_bars: int = 2) -> list[dict[str, Any]]:
    """Produce P0, FVG, and 2B events in one forward-only pass over a contiguous segment."""
    if not rows:
        return []
    if any(value < 1 for value in (left_bars, right_bars, ob_lookback, two_b_return_bars)):
        raise ValueError("結構偵測參數必須是正整數。")
    events = p0._p0_events(rows, left_bars=left_bars, right_bars=right_bars, ob_lookback=ob_lookback)
    pivots_by_confirmation: dict[int, list[tuple[str, int, Decimal]]] = defaultdict(list)
    for pivot in range(left_bars, len(rows) - right_bars):
        window = rows[pivot - left_bars:pivot + right_bars + 1]
        high, low = _dec(rows[pivot]["high"]), _dec(rows[pivot]["low"])
        if all(high > _dec(candle["high"]) for j, candle in enumerate(window) if j != left_bars):
            pivots_by_confirmation[pivot + right_bars].append(("high", pivot, high))
        if all(low < _dec(candle["low"]) for j, candle in enumerate(window) if j != left_bars):
            pivots_by_confirmation[pivot + right_bars].append(("low", pivot, low))

    # FVG is known only at the close of the third candle.
    for index in range(2, len(rows)):
        first, third = rows[index - 2], rows[index]
        first_high, first_low = _dec(first["high"]), _dec(first["low"])
        third_high, third_low = _dec(third["high"]), _dec(third["low"])
        if first_high < third_low:
            events.append({"event_type": "fair_value_gap", "direction": "bullish",
                           "formed_at_open_time": int(third["open_time"]),
                           "confirmed_at_close_time": int(third["close_time"]),
                           "price_low": str(first_high), "price_high": str(third_low)})
        if third_high < first_low:
            events.append({"event_type": "fair_value_gap", "direction": "bearish",
                           "formed_at_open_time": int(third["open_time"]),
                           "confirmed_at_close_time": int(third["close_time"]),
                           "price_low": str(third_high), "price_high": str(first_low)})

    # Watch each confirmed swing until its first wick breach. A pending 2B can only
    # confirm on a closed candle at the breach or within the following configured bars.
    swing_highs: list[tuple[Decimal, int, int]] = []
    swing_lows: list[tuple[Decimal, int, int]] = []  # store -level so the heap exposes the highest level
    pending_high: list[tuple[Decimal, int, int, int]] = []
    pending_low: list[tuple[Decimal, int, int, int]] = []
    activated_confirmation = -1
    for index, candle in enumerate(rows):
        while activated_confirmation + 1 < index:
            activated_confirmation += 1
            for kind, pivot, level in pivots_by_confirmation.get(activated_confirmation, []):
                if kind == "high":
                    heapq.heappush(swing_highs, (level, pivot, activated_confirmation))
                else:
                    heapq.heappush(swing_lows, (-level, pivot, activated_confirmation))
        close, high, low = (_dec(candle[field]) for field in ("close", "high", "low"))
        close_time = int(candle["close_time"])
        next_pending_high: list[tuple[Decimal, int, int, int]] = []
        for level, pivot, breach, deadline in pending_high:
            if index <= deadline and close <= level:
                events.append({"event_type": "two_b_fakeout", "direction": "bearish",
                               "formed_at_open_time": int(rows[breach]["open_time"]),
                               "confirmed_at_close_time": close_time, "reference_price": str(level)})
            elif index <= deadline:
                next_pending_high.append((level, pivot, breach, deadline))
        pending_high = next_pending_high
        next_pending_low: list[tuple[Decimal, int, int, int]] = []
        for level, pivot, breach, deadline in pending_low:
            if index <= deadline and close >= level:
                events.append({"event_type": "two_b_fakeout", "direction": "bullish",
                               "formed_at_open_time": int(rows[breach]["open_time"]),
                               "confirmed_at_close_time": close_time, "reference_price": str(level)})
            elif index <= deadline:
                next_pending_low.append((level, pivot, breach, deadline))
        pending_low = next_pending_low
        while swing_highs and swing_highs[0][0] < high:
            level, pivot, confirmed = heapq.heappop(swing_highs)
            if close <= level:
                events.append({"event_type": "two_b_fakeout", "direction": "bearish",
                               "formed_at_open_time": int(candle["open_time"]),
                               "confirmed_at_close_time": close_time, "reference_price": str(level)})
            else:
                pending_high.append((level, pivot, index, index + two_b_return_bars))
        while swing_lows and -swing_lows[0][0] > low:
            neg_level, pivot, confirmed = heapq.heappop(swing_lows)
            level = -neg_level
            if close >= level:
                events.append({"event_type": "two_b_fakeout", "direction": "bullish",
                               "formed_at_open_time": int(candle["open_time"]),
                               "confirmed_at_close_time": close_time, "reference_price": str(level)})
            else:
                pending_low.append((level, pivot, index, index + two_b_return_bars))
    events.sort(key=lambda event: (int(event["confirmed_at_close_time"]), event["event_type"],
                                   event.get("direction", ""), event.get("event_id", "")))
    return events


def filter_setups(rows: list[dict[str, Any]], events: list[dict[str, Any]], variant: str,
                  *, two_b_window_bars: int = 2) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if variant not in VARIANTS:
        raise ValueError(f"不支援的 P1 假設版本：{variant}。")
    if two_b_window_bars != 2:
        raise ValueError("P1 v1 固定採 2 根已收盤 K 線確認窗口，不在同一研究中調參。")
    if variant == "p0_ob":
        break_pairs = {(int(event["confirmed_at_close_time"]), event["direction"])
                       for event in events if event["event_type"] == "structure_break"}
        ob_pairs = {(int(event["confirmed_at_close_time"]), event["direction"])
                    for event in events if event["event_type"] == "order_block"}
        count = len(break_pairs & ob_pairs)
        return [event for event in events if event["event_type"] in {"structure_break", "order_block"}], {
            "eligible": count, "accepted": count, "no_fvg_confluence": 0, "fvg_conflict": 0,
            "no_2b_confirmation": 0, "two_b_conflict": 0}
    step = p0.INTERVAL_MILLISECONDS["1h"]
    obs = {(int(event["confirmed_at_close_time"]), event["direction"]): event
           for event in events if event["event_type"] == "order_block"}
    gaps_by_direction: dict[str, list[dict[str, Any]]] = defaultdict(list)
    twobs_by_direction: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if event["event_type"] == "fair_value_gap":
            gaps_by_direction[event["direction"]].append(event)
        elif event["event_type"] == "two_b_fakeout":
            twobs_by_direction[event["direction"]].append(event)
    selected: list[dict[str, Any]] = []
    counts = {"eligible": 0, "accepted": 0, "no_fvg_confluence": 0, "fvg_conflict": 0,
              "no_2b_confirmation": 0, "two_b_conflict": 0}
    for event in events:
        if event["event_type"] != "structure_break":
            continue
        setup_time, direction = int(event["confirmed_at_close_time"]), event["direction"]
        ob = obs.get((setup_time, direction))
        if ob is None:
            continue
        counts["eligible"] += 1
        ob_low, ob_high = _dec(ob["price_low"]), _dec(ob["price_high"])
        ob_formed = int(ob.get("formed_at_open_time", setup_time))

        def overlaps(fvg: dict[str, Any]) -> bool:
            return max(ob_low, _dec(fvg["price_low"])) < min(ob_high, _dec(fvg["price_high"]))

        fvg_start = ob_formed
        fvg_ok = any(fvg_start <= int(fvg["confirmed_at_close_time"]) <= setup_time and overlaps(fvg)
                     for fvg in gaps_by_direction[direction])
        fvg_conflict = any(fvg_start <= int(fvg["confirmed_at_close_time"]) <= setup_time and overlaps(fvg)
                           for fvg in gaps_by_direction["bearish" if direction == "bullish" else "bullish"])
        window_start = setup_time - two_b_window_bars * step
        twob_ok = any(window_start <= int(item["confirmed_at_close_time"]) <= setup_time
                      for item in twobs_by_direction[direction])
        twob_conflict = any(window_start <= int(item["confirmed_at_close_time"]) <= setup_time
                            for item in twobs_by_direction["bearish" if direction == "bullish" else "bullish"])
        needs_fvg = variant in {"ob_fvg_overlap", "ob_fvg_and_2b"}
        needs_2b = variant in {"ob_2b_confirmed", "ob_fvg_and_2b"}
        if needs_fvg and fvg_conflict:
            counts["fvg_conflict"] += 1
            continue
        if needs_2b and twob_conflict:
            counts["two_b_conflict"] += 1
            continue
        if needs_fvg and not fvg_ok:
            counts["no_fvg_confluence"] += 1
            continue
        if needs_2b and not twob_ok:
            counts["no_2b_confirmation"] += 1
            continue
        selected.extend((event, ob))
        counts["accepted"] += 1
    return selected, counts


def run_study(path: Path, symbol: str, *, fee_bps: float = 10.0, slippage_bps: float = 5.0,
              target_r: float = 2.0, train_fraction: float = .6,
              validation_fraction: float = .2) -> dict[str, Any]:
    values = (fee_bps, slippage_bps, target_r, train_fraction, validation_fraction)
    if not all(math.isfinite(value) for value in values) or fee_bps < 0 or slippage_bps < 0 or target_r <= 0 or not 0 < train_fraction < 1 or not 0 < validation_fraction < 1 or train_fraction + validation_fraction >= 1:
        raise ValueError("成本、目標 R 與資料切分比例設定無效。")
    rows = p0._closed_rows(path, p0.validate_symbol(symbol), "1h")
    integrity = p0.inspect_candle_integrity(rows, "1h")
    n = len(rows)
    cut1, cut2 = int(n * train_fraction), int(n * (train_fraction + validation_fraction))
    bounds = {"train": (0, cut1), "validation": (cut1, cut2), "test": (cut2, n)}
    variants: dict[str, Any] = {}
    for variant in VARIANTS:
        variant_trades: list[dict[str, Any]] = []
        split_results: dict[str, Any] = {}
        filters: dict[str, dict[str, int]] = {}
        for split, (start, end) in bounds.items():
            segment = rows[start:end]
            trades: list[dict[str, Any]] = []
            aggregate = {"eligible": 0, "accepted": 0, "no_fvg_confluence": 0, "fvg_conflict": 0,
                         "no_2b_confirmation": 0, "two_b_conflict": 0}
            for contiguous in p0._contiguous_segments(segment):
                events = p1_events(contiguous)
                selected, counts = filter_setups(contiguous, events, variant)
                for key, value in counts.items():
                    aggregate[key] += value
                trades.extend(p0._simulate_segment(contiguous, selected, fee_bps, slippage_bps, target_r))
            start_ms = int(segment[0]["open_time"]) if segment else None
            end_ms = int(segment[-1]["close_time"]) if segment else None
            split_results[split] = {"period_start_utc": p0._utc(start_ms) if start_ms is not None else None,
                                    "period_end_utc": p0._utc(end_ms) if end_ms is not None else None,
                                    "metrics": p0._metrics(trades), "filter_counts": aggregate}
            filters[split] = aggregate
            variant_trades.extend({**trade, "split": split} for trade in trades)
        by_direction: dict[str, Any] = {}
        for direction in ("long", "short"):
            by_direction[direction] = p0._metrics([trade for trade in variant_trades if trade["direction"] == direction])
        variants[variant] = {"definition": DEFINITIONS[variant], "periods": split_results,
                             "by_direction": by_direction}
    baseline = variants["p0_ob"]["periods"]
    deltas: dict[str, Any] = {}
    for variant in VARIANTS[1:]:
        deltas[variant] = {}
        for split in ("validation", "test"):
            base = baseline[split]["metrics"]
            current = variants[variant]["periods"][split]["metrics"]
            deltas[variant][split] = {"trade_count_delta": current["trades"] - base["trades"],
                                      "win_rate_delta": current["win_rate"] - base["win_rate"],
                                      "expectancy_r_delta": current["expectancy_r"] - base["expectancy_r"],
                                      "profit_factor_delta": (current["profit_factor"] - base["profit_factor"])
                                      if current["profit_factor"] is not None and base["profit_factor"] is not None else None}
    return {"study_version": VERSION, "symbol": symbol, "interval": "1h",
            "data_summary": {"closed_candles": n, "start_utc": rows[0].get("open_time_utc") or p0._utc(int(rows[0]["open_time"])),
                             "end_utc": rows[-1].get("close_time_utc") or p0._utc(int(rows[-1]["close_time"])),
                             "normalized_exchange_close_time_count": sum(
                                 int(row.get("exchange_close_time") or row["close_time"]) != int(row["close_time"]) for row in rows),
                             "gap_count": len(integrity["gaps"]), "missing_candles": integrity["missing_candle_count"]},
            "method": {"split": {"type": "chronological", "train_fraction": train_fraction,
                                  "validation_fraction": validation_fraction,
                                  "test_fraction": 1 - train_fraction - validation_fraction},
                       "signal_timing": "只在訊號/確認 K 線收盤後決策，下一根 1H 開盤進場。",
                       "same_candle_stop_and_target": "停損先到。",
                       "gap_handling": "每個資料切分內按連續 K 線分段；結構與持倉不跨缺口。",
                       "fees_and_slippage_bps_per_side": {"fee": fee_bps, "slippage": slippage_bps},
                       "target_r": target_r,
                       "fvg_conflict_rule": "若與 OB 有重疊的反向 FVG 在 OB 形成至訊號收盤間確認，該候選觀望。",
                       "two_b_conflict_rule": "若反向 2B 在訊號收盤或前兩根已收盤 1H K 線內確認，該候選觀望。",
                       "selection_rule": "四種假設先行固定；test 僅作一次性報告，不根據 test 結果調整門檻。"},
            "variants": variants, "delta_vs_p0": deltas,
            "limitations": ["本研究是規則假設比較，不是顯著性檢定或投資建議。",
                            "平均盈虧為每 1 BTC 單位的 USDT 金額；最大回撤以累積 R 計。",
                            "FVG 重疊定義採正面積交集；僅邊界相接不算重疊。",
                            "2B 必須等回到已確認 swing 內的 K 線收盤後才算確認。"],
            "data_integrity": integrity}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="比較 P0 與預先定義的 OB/FVG/2B 歷史研究假設。")
    parser.add_argument("symbol", nargs="?", default="BTCUSDT")
    parser.add_argument("--data-dir", type=Path, default=Path("data/historical"))
    parser.add_argument("--fee-bps-per-side", type=float, default=10.0)
    parser.add_argument("--slippage-bps-per-side", type=float, default=5.0)
    parser.add_argument("--target-r", type=float, default=2.0)
    parser.add_argument("--train-fraction", type=float, default=.6)
    parser.add_argument("--validation-fraction", type=float, default=.2)
    parser.add_argument("--output", type=Path, help="JSON 報告路徑")
    args = parser.parse_args(argv)
    try:
        result = run_study(args.data_dir / f"{p0.validate_symbol(args.symbol)}_1h.csv", args.symbol,
                           fee_bps=args.fee_bps_per_side, slippage_bps=args.slippage_bps_per_side,
                           target_r=args.target_r, train_fraction=args.train_fraction,
                           validation_fraction=args.validation_fraction)
        output = args.output or args.data_dir / f"{p0.validate_symbol(args.symbol)}_p1_study.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False,
                                     default=p0._json_default) + "\n", encoding="utf-8")
        compact = {"study_version": result["study_version"], "data_summary": result["data_summary"],
                   "periods": {name: {split: item["metrics"] for split, item in variant["periods"].items()}
                               for name, variant in result["variants"].items()},
                   "delta_vs_p0": result["delta_vs_p0"], "output": str(output)}
        print(json.dumps(compact, ensure_ascii=False, indent=2, allow_nan=False, default=p0._json_default))
    except (OSError, ValueError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
