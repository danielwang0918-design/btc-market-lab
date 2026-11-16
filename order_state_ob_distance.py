#!/usr/bin/env python3
"""P0-ORDER-STATE-05: descriptive OB distance before BOS-driven cancellation.

Uses only previously archived closed Binance Spot 1H candles, accepted OB identifiers,
and verified stage-03/04 cancellation ledgers. 4H/1D alignment is established by the
stage-04 original BOS replay. NEVER places orders or alters trading rules.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "p0-ob-distance-before-bos-v1"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
INTERVALS = ("1d", "4h", "1h")
HOUR_MS = 3_600_000
EXPECTED_BOS = {"BTCUSDT": 23, "ETHUSDT": 36, "SOLUSDT": 35}
EXPECTED_CHANGED = {"BTCUSDT": 32, "ETHUSDT": 53, "SOLUSDT": 51}
CANCEL_TYPE = "pending_signal_changed_cancel"
REASON_BOS = "mtf_bos_direction_mismatch"


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def utc_ms(raw: str) -> int:
    d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("Timestamp must include UTC timezone")
    t = int(d.astimezone(timezone.utc).timestamp() * 1000)
    if t % HOUR_MS:
        raise ValueError("Unaligned 1h event time")
    return t


def utc(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, tz=timezone.utc).isoformat()


def rows_csv(path: Path) -> list[dict[str,str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        r = csv.DictReader(stream)
        if not r.fieldnames:
            raise ValueError(f"Empty CSV header: {path}")
        return list(r)


def write_json(path: Path, content: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(content, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def valid_price(raw: Any) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError, OverflowError) as e:
        raise ValueError("Invalid candle price") from e
    if not math.isfinite(v) or v <= 0:
        raise ValueError("Nonpositive/nonfinite candle price")
    return v


def ob_from_setup_id(setup_id: str, symbol: str, direction: str) -> int:
    # Original event identifier uses symbol:1h:order_block:direction:formed_ms:confirmed_ms:reference
    items = setup_id.split(":")
    if len(items) != 7 or items[:3] != [symbol, "1h", "order_block"]:
        raise ValueError("Unexpected OB event ID format")
    expected_dir = "bullish" if direction == "long" else "bearish"
    if items[3] != expected_dir:
        raise ValueError("OB identifier conflicts with accepted signal direction")
    formed = int(items[4]); confirmed = int(items[5])
    if formed < 0 or formed % HOUR_MS or confirmed % HOUR_MS != HOUR_MS - 1 or confirmed <= formed:
        raise ValueError("Invalid OB event confirmation time")
    valid_price(items[6])
    return formed


def nearest_zone_gap(price: float, zone_low: float, zone_high: float) -> float:
    if not 0 < zone_low <= zone_high:
        raise ValueError("OB bounds invalid")
    return max(zone_low - price, 0.0, price - zone_high)


def directional_gap(price: float, zone_low: float, zone_high: float, side: str) -> float:
    """Signed raw price gap: positive on original approach side, negative past far edge."""
    if side == "long":
        return price - zone_high
    if side == "short":
        return zone_low - price
    raise ValueError("Side invalid")


def atr14_at(bars: list[dict[str, str]], idx: int) -> float:
    """Simple TR(14), as of a fully closed 1H bar; requires 15 contiguous candles."""
    if idx < 14:
        raise ValueError("Not enough 1H history for ATR14")
    candle = bars[idx-14:idx+1]
    opens = [int(b["open_time"]) for b in candle]
    if any(b-a != HOUR_MS for a,b in zip(opens, opens[1:])):
        raise ValueError("Discontinuous hourly ATR input")
    tr = []
    for i in range(1,15):
        b = candle[i]; h = valid_price(b["high"]); l = valid_price(b["low"])
        prev = valid_price(candle[i-1]["close"])
        if h < l:
            raise ValueError("Invalid price high-low")
        tr.append(max(h-l, abs(h-prev), abs(l-prev)))
    result = sum(tr)/14
    if not math.isfinite(result) or result <= 0:
        raise ValueError("Zero/non-finite ATR14")
    return result


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {"n":0}
    def stat(k: str):
        values = [float(r[k]) for r in records]
        return round(statistics.median(values), 4)
    def qs(k: str):
        values = sorted(float(r[k]) for r in records)
        return {"p25":round(values[(len(values)-1)//4],4),
                "median":round(statistics.median(values),4),
                "p75":round(values[(3*(len(values)-1))//4],4)}
    return {
        "n":len(records),
        "wait_hours":qs("waiting_hours"),
        "ob_width_atr14_acceptance":qs("ob_width_atr14_acceptance"),
        "gap_at_signal_atr14":qs("gap_at_signal_atr14"),
        "gap_before_cancel_atr14":qs("gap_before_cancel_atr14"),
        "gap_before_cancel_bps":qs("gap_before_cancel_bps"),
        "gap_at_cancel_atr14":qs("gap_at_cancel_atr14"),
        "gap_shrank_before_cancel":sum(float(r["gap_before_cancel_atr14"]) < float(r["gap_at_signal_atr14"]) for r in records),
        "close_before_cancel_inside_ob":sum(float(r["gap_before_cancel_bps"]) == 0 for r in records),
        "close_at_cancel_inside_ob":sum(float(r["gap_at_cancel_bps"]) == 0 for r in records),
        "side_distribution":dict(sorted(Counter(r["original_side"] for r in records).items())),
        "cancel_months":len({r["canceled_open_utc"][:7] for r in records}),
    }


def extract_symbol(data_dir: Path, stage03: Path, stage04: Path, replay_dir: Path,
                   symbol: str, output_dir: Path) -> dict[str, Any]:
    if symbol not in SYMBOLS:
        raise ValueError("Not one of the frozen 3 symbols")
    prev = json.loads((stage04 / f"{symbol}_bos_timeline.json").read_text(encoding="utf-8"))
    cause = json.loads((stage03 / f"{symbol}_cancellation_causes.json").read_text(encoding="utf-8"))
    valid = (prev.get("version") == "p0-ob-bos-first-divergence-v1" and
             prev.get("status") == "verified_from_closed_formal_bos_events" and
             prev.get("strategy_changed") is False and prev.get("uses_subhour_candles") is False and
             tuple(prev.get("decision_timeframes", [])) == INTERVALS and
             cause.get("status") == "verified_reconstructed_test_cancellation_snapshots" and
             cause.get("uses_subhour_data") is False and cause.get("strategy_changed") is False and
             prev.get("test_cancellations_stage03_reason_mtf") == EXPECTED_BOS[symbol] and
             cause.get("test_canceled_signals") == EXPECTED_CHANGED[symbol])
    if not valid:
        raise ValueError("Upstream 03/04 verification status mismatch")
    for iv in INTERVALS:
        name = f"{symbol}_{iv}.csv"
        if sha(data_dir / name) != prev["source_sha256"].get(name):
            raise ValueError(f"Frozen {name} SHA256 mismatch")
    if sha(data_dir / "multi_asset_coverage.json") != prev["source_sha256"].get("coverage"):
        raise ValueError("Frozen coverage SHA256 mismatch")
    source_names = {
        "stage03_causes_csv":stage03/f"{symbol}_cancellation_causes.csv",
        "stage03_causes_json":stage03/f"{symbol}_cancellation_causes.json",
        "order_state_events":replay_dir/f"{symbol}_order_state_events.csv",
    }
    for key, path in source_names.items():
        if sha(path) != prev["source_sha256"].get(key):
            raise ValueError(f"Frozen {key} SHA256 mismatch")
    bars = rows_csv(data_dir / f"{symbol}_1h.csv")
    expected = list(range(len(bars)))
    opens = [int(b["open_time"]) for b in bars]
    if len(set(opens)) != len(opens) or any(b<=a for a,b in zip(opens,opens[1:])):
        raise ValueError("Invalid/duplicate hourly candle timeline")
    # Verify stage04 cohort against stage03, and against ORIGINAL accepted -> canceled ledger.
    causes = rows_csv(source_names["stage03_causes_csv"])
    bosque = rows_csv(stage04/f"{symbol}_bos_timeline.csv")
    bos_path = rows_csv(stage04/f"{symbol}_bos_path.csv")
    events = [r for r in rows_csv(source_names["order_state_events"]) if r["split"]=="test"]
    accepted = {r["setup_id"]:r for r in events if r["event"]=="signal_accepted_pending_from_closed_1h"}
    canceled = {r["setup_id"]:r for r in events if r["event"]==CANCEL_TYPE}
    if len(accepted) != sum(r["event"]=="signal_accepted_pending_from_closed_1h" for r in events):
        raise ValueError("Repeated original accepted signal")
    if len(canceled) != sum(r["event"]==CANCEL_TYPE for r in events):
        raise ValueError("Repeated original canceled signal")
    causes_by_id = {r["setup_id"]:r for r in causes}
    bos_by_id = {r["setup_id"]:r for r in bosque}
    if len(causes_by_id)!=EXPECTED_CHANGED[symbol] or len(bos_by_id)!=EXPECTED_BOS[symbol]:
        raise ValueError("Lost or duplicate 03/04 cause records")
    if {k for k,v in causes_by_id.items() if v["category"]==REASON_BOS} != set(bos_by_id):
        raise ValueError("Different BOS cohort from frozen 03 causal diagnosis")
    if set(causes_by_id)!=set(canceled):
        raise ValueError("Stage-03 changed-signal set differs from original lifecycle")
    all_out=[]
    for row in causes:
        setup=row["setup_id"]
        old=accepted.get(setup); ended=canceled.get(setup)
        if not old or not ended or row["split"]!="test":
            raise ValueError("Missing original accepted/canceled event")
        acc=utc_ms(row["accepted_open_utc"]);cancel=utc_ms(row["canceled_open_utc"])
        if utc_ms(old["at_1h_open_utc"])!=acc or utc_ms(ended["at_1h_open_utc"])!=cancel:
            raise ValueError("Original ledger timestamps differ")
        if int(row["pending_hours"])!=(cancel-acc)//HOUR_MS or cancel<=acc:
            raise ValueError("Waiting time mismatch")
        formation = ob_from_setup_id(setup,symbol,row["original_side"])
        bi=bisect.bisect_left(opens,formation)
        ai=bisect.bisect_left(opens,acc)
        ci=bisect.bisect_left(opens,cancel)
        if (bi>=len(bars) or ai>=len(bars) or ci>=len(bars) or
            opens[bi]!=formation or opens[ai]!=acc or opens[ci]!=cancel or ci<=ai):
            raise ValueError("Required OB/accept/cancel candle absent")
        ob=bars[bi];lo=valid_price(ob["low"]);hi=valid_price(ob["high"])
        accept_close=valid_price(bars[ai]["close"])
        prior_close=valid_price(bars[ci-1]["close"])
        cancel_close=valid_price(bars[ci]["close"])
        atr_accept=atr14_at(bars,ai)
        atr_before=atr14_at(bars,ci-1)
        atr_cancel=atr14_at(bars,ci)
        if any(opens[i+1]-opens[i]!=HOUR_MS for i in range(ai,ci)):
            raise ValueError("Gap during canceled pending lifecycle")
        gap_accept=nearest_zone_gap(accept_close,lo,hi)
        gap_pre=nearest_zone_gap(prior_close,lo,hi)
        gap_c=nearest_zone_gap(cancel_close,lo,hi)
        rowout={
            "symbol":symbol,"setup_id":setup,"original_side":row["original_side"],
            "cancel_category":row["category"],"accepted_open_utc":row["accepted_open_utc"],
            "canceled_open_utc":row["canceled_open_utc"],"waiting_hours":int(row["pending_hours"]),
            "ob_formed_open_utc":utc(formation),
            "ob_low":round(lo,10),"ob_high":round(hi,10),
            "ob_width_atr14_acceptance":round((hi-lo)/atr_accept,8),
            "accept_close":accept_close,"close_before_cancel":prior_close,"close_at_cancel":cancel_close,
            "atr14_at_accept_close":round(atr_accept,10),
            "atr14_before_cancel":round(atr_before,10),
            "atr14_at_cancel_close":round(atr_cancel,10),
            "gap_at_signal_atr14":round(gap_accept/atr_accept,8),
            "gap_before_cancel_atr14":round(gap_pre/atr_before,8),
            "gap_at_cancel_atr14":round(gap_c/atr_cancel,8),
            "gap_at_signal_bps":round(1e4*gap_accept/accept_close,6),
            "gap_before_cancel_bps":round(1e4*gap_pre/prior_close,6),
            "gap_at_cancel_bps":round(1e4*gap_c/cancel_close,6),
            "signed_directional_gap_before_cancel_atr14":round(directional_gap(prior_close,lo,hi,row["original_side"])/atr_before,8),
            "price_snapshot_closed_hourly_only":True,"model_strategy_unchanged":True,
        }
        if setup in bos_by_id:
            b=bos_by_id[setup]
            detail=json.loads(b["first_deviation_detail"])
            if (b["first_deviating_intervals"]!='["1h"]' or
                len(detail)!=1 or detail[0].get("interval")!="1h" or
                detail[0].get("type")!="new_opposite_confirmed_bos" or
                b["first_bos_mismatch_asof_utc"]!=row["snapshot_close_utc"] or
                b["original_side"]!=row["original_side"] or
                int(b["waiting_hours"])!=rowout["waiting_hours"]):
                raise ValueError("Stage-04 confirmed 1H reversal chronology mismatch")
            rowout["first_deviating_timeframe"]="1h"
            rowout["first_bos_mismatch_asof_utc"]=b["first_bos_mismatch_asof_utc"]
        else:
            rowout["first_deviating_timeframe"]=""
            rowout["first_bos_mismatch_asof_utc"]=""
        all_out.append(rowout)
    if len(all_out) != EXPECTED_CHANGED[symbol]:
        raise ValueError("Not all changed-signal cancellations reconstructed")
    # Ensure the stage-04 BOS path is exactly one row per waiting 1H boundary,
    # and the first mismatch only arrives on the original cancellation hour.
    path_count=Counter(r["setup_id"] for r in bos_path)
    for setup,b in bos_by_id.items():
        if path_count[setup] != int(b["waiting_hours"]):
            raise ValueError("Stage-04 BOS as-of path has missing hours")
    bos_cases=[r for r in all_out if r["cancel_category"]==REASON_BOS]
    others=[r for r in all_out if r["cancel_category"]!=REASON_BOS]
    report={
        "version":VERSION,"symbol":symbol,"status":"verified_closed_1h_distance_observation",
        "decision_timeframes":["1h","4h","1d"],"subhour_candles_used":False,
        "model_strategy_unchanged":True,"real_orders":False,
        "test_changed_signal_cancellations":len(all_out),
        "test_1h_new_opposite_bos_cancellations":len(bos_cases),
        "test_other_changed_signal_cancellations":len(others),
        "bos_cancellations":summarize(bos_cases),
        "other_changed_cancellations":summarize(others),
        "source_sha256":{
            "prior_stage04_json":sha(stage04/f"{symbol}_bos_timeline.json"),
            "prior_stage04_csv":sha(stage04/f"{symbol}_bos_timeline.csv"),
            "prior_stage04_path":sha(stage04/f"{symbol}_bos_path.csv"),
            **{str(p.name):sha(p) for p in source_names.values()},
            "coverage":sha(data_dir/"multi_asset_coverage.json"),
            **{f"{symbol}_{iv}.csv":sha(data_dir/f"{symbol}_{iv}.csv") for iv in INTERVALS},
        },
        "definitions":{
            "prior_close":"Close of the last completed 1H bar BEFORE the 1H bar that confirms reversal; no future information.",
            "cancel_close":"Close of the 1H bar confirming the new opposite BOS; known only once that bar closes.",
            "distance":"Absolute price difference from close to closest edge of frozen original OB candle; zero if inside OB.",
            "distance_bps":"10000 * absolute distance / observed 1H close price.",
            "distance_atr14":"absolute distance divided by simple 14-period 1H true range average of fully closed bars, including previous close and excluding future bars.",
            "not_a_trade_rule":"ATR14 is a descriptive historical normalization ONLY and is NOT added to order entry, cancel, exit or position sizing.",
        },
        "limitations":[
            "Only cancellations in a previously inspected Test set. Not a prospective estimate or new holdout.",
            "The cohort is selected conditional on a future cancel event; comparison with other cancels is descriptive, not causal.",
            "BOS mismatch was the original first failed strategy condition; no conclusion that holding orders improves results.",
            "No 1m/5m/15m trading signals, no alternative trades, no actual limit order execution evidence.",
        ],
    }
    output_dir.mkdir(parents=True,exist_ok=True)
    csv_path=output_dir/f"{symbol}_ob_distance_records.csv"
    with csv_path.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(all_out[0]),extrasaction="ignore")
        w.writeheader();w.writerows(all_out)
    write_json(output_dir/f"{symbol}_ob_distance_summary.json",report)
    return report


def aggregate(results: Path, output: Path) -> dict[str, Any]:
    reports=[]
    for sym in SYMBOLS:
        p=results/f"{sym}_ob_distance_summary.json"
        report=json.loads(p.read_text(encoding="utf-8"))
        if (report.get("version")!=VERSION or report.get("symbol")!=sym or
            report.get("status")!="verified_closed_1h_distance_observation" or
            report.get("subhour_candles_used") is not False or
            report.get("model_strategy_unchanged") is not True or
            report.get("test_changed_signal_cancellations")!=EXPECTED_CHANGED[sym] or
            report.get("test_1h_new_opposite_bos_cancellations")!=EXPECTED_BOS[sym]):
            raise ValueError("Missing or unverifiable per-symbol evidence")
        records=rows_csv(results/f"{sym}_ob_distance_records.csv")
        if len(records)!=EXPECTED_CHANGED[sym] or sum(r["cancel_category"]==REASON_BOS for r in records)!=EXPECTED_BOS[sym]:
            raise ValueError("Per-symbol CSV evidence differs from verified summary")
        if len({r["setup_id"] for r in records}) != len(records):
            raise ValueError("Repeated cancellation setup ID")
        if any(r["symbol"] != sym or r["price_snapshot_closed_hourly_only"] != "True" or
               r["model_strategy_unchanged"] != "True" for r in records):
            raise ValueError("Invalid trade decision provenance in CSV")
        bos_records=[x for x in records if x["cancel_category"]==REASON_BOS]
        other_records=[x for x in records if x["cancel_category"]!=REASON_BOS]
        if summarize(bos_records)!=report["bos_cancellations"] or summarize(other_records)!=report["other_changed_cancellations"]:
            raise ValueError("CSV distance statistics differ from verified summary")
        reports.append(report)
    combined=[]
    for sym in SYMBOLS:
        combined.extend(rows_csv(results/f"{sym}_ob_distance_records.csv"))
    bos=[r for r in combined if r["cancel_category"]==REASON_BOS]
    others=[r for r in combined if r["cancel_category"]!=REASON_BOS]
    output.mkdir(parents=True,exist_ok=True)
    report={
        "version":VERSION,"status":"all_three_symbols_verified","real_orders":False,
        "decision_timeframes_only":["1h","4h","1d"],"uses_subhour_data":False,
        "strategy_changed":False,"test_accepted_changed_cancelled":len(combined),
        "test_bos_cancellations":len(bos),"test_other_changed_cancellations":len(others),
        "by_symbol":{r["symbol"]:r for r in reports},
        "all_bos_cancellations":summarize(bos),
        "all_other_changed_cancellations":summarize(others),
        "warning":"Old Test data, no independent holdout, correlation not adjusted. Do not change order rules from this report.",
    }
    write_json(output/"ob_distance_05_aggregate.json",report)
    md=["# P0-ORDER-STATE-05｜OB 距離與 1H 反向 BOS 前的等待時間", "",
        "既有 Test 事後描述性研究；只使用 1H/4H/1D 已收盤資料，不調整進出場規則。", "",
        "| 幣種 | 1H反向BOS取消 | 其他訊號改變取消 | BOS等待時間中位數(h) | BOS前一根1H收盤距OB中位(ATR14) |", 
        "|---|---:|---:|---:|---:|"]
    for sym in SYMBOLS:
        r=next(t for t in reports if t["symbol"]==sym)
        b=r["bos_cancellations"]
        md.append(f'| {sym} | {r["test_1h_new_opposite_bos_cancellations"]} | {r["test_other_changed_signal_cancellations"]} | {b["wait_hours"]["median"]} | {b["gap_before_cancel_atr14"]["median"]} |')
    b=report["all_bos_cancellations"];o=report["all_other_changed_cancellations"]
    md+= [f'| **合計** | **{len(bos)}** | **{len(others)}** | **{b["wait_hours"]["median"]}** | **{b["gap_before_cancel_atr14"]["median"]}** |', "",
        f'- BOS取消時，前一根收盤距 OB 中位：{b["gap_before_cancel_bps"]["median"]} bps。',
        f'- 接受訊號時距OB中位：{b["gap_at_signal_atr14"]["median"]} ATR14。',
        f'- BOS取消前一根距OB中位：{b["gap_before_cancel_atr14"]["median"]} ATR14。',
        f'- 其他42筆不同取消類別，取消前距OB中位：{o["gap_before_cancel_atr14"]["median"]} ATR14。',
        "", "**重要**：這是已知取消結果條件下的描述統計，不能據此創建可宣稱有效的 ATR 距離閾值，也不能把被取消掛單視為本應成交。",
    ]
    (output/"ob_distance_05_aggregate.md").write_text("\n".join(md)+"\n",encoding="utf-8")
    return report


def main(argv: list[str]|None=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="command",required=True)
    s=sub.add_parser("symbol")
    s.add_argument("symbol",choices=SYMBOLS)
    s.add_argument("--data-dir",type=Path,required=True)
    s.add_argument("--stage03",type=Path,required=True)
    s.add_argument("--stage04",type=Path,required=True)
    s.add_argument("--replay-dir",type=Path,required=True)
    s.add_argument("--output-dir",type=Path,required=True)
    a=sub.add_parser("aggregate")
    a.add_argument("--input-dir",type=Path,required=True)
    a.add_argument("--output-dir",type=Path,required=True)
    opt=parser.parse_args(argv)
    if opt.command=="symbol":
        result=extract_symbol(opt.data_dir,opt.stage03,opt.stage04,opt.replay_dir,opt.symbol,opt.output_dir)
    else:
        result=aggregate(opt.input_dir,opt.output_dir)
    print(json.dumps({"status":result["status"],"symbol":result.get("symbol"),
                      "test_bos_cancellations":result.get("test_bos_cancellations",result.get("test_1h_new_opposite_bos_cancellations"))},ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())