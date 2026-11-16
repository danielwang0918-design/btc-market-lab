#!/usr/bin/env python3
"""Per-trade excursion and higher-timeframe context diagnostics for BTC MTF backtests."""
from __future__ import annotations
import argparse, bisect, csv, json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _ema20_by_segment(rows: list[dict[str, str]], interval_ms: int) -> list[float | None]:
    output: list[float | None] = [None] * len(rows)
    start = 0
    while start < len(rows):
        end = start + 1
        while end < len(rows) and int(rows[end]["open_time"]) - int(rows[end - 1]["open_time"]) == interval_ms:
            end += 1
        closes = [float(row["close"]) for row in rows[start:end]]
        if len(closes) >= 20:
            value = sum(closes[:20]) / 20
            output[start + 19] = value
            alpha = 2.0 / 21.0
            for offset in range(20, len(closes)):
                value = alpha * closes[offset] + (1.0 - alpha) * value
                output[start + offset] = value
        start = end
    return output


def _context_at(rows: list[dict[str, str]], ema: list[float | None],
                close_after: list[int], asof_ms: int) -> dict[str, Any]:
    index = bisect.bisect_right(close_after, asof_ms) - 1
    if index < 0:
        return {"close": None, "ema20": None, "position_pct": None, "slope_pct_3bars": None}
    close, value = float(rows[index]["close"]), ema[index]
    old = ema[index - 3] if index >= 3 else None
    return {
        "close": close, "ema20": value,
        "position_pct": round((close / value - 1) * 100, 6) if value else None,
        "slope_pct_3bars": round((value / old - 1) * 100, 6) if value and old else None,
        "candle_close_utc": rows[index]["close_time_utc"],
    }


def _trend_alignment(direction: str, context: dict[str, Any]) -> str:
    position, slope = context.get("position_pct"), context.get("slope_pct_3bars")
    if position is None or slope is None:
        return "unknown"
    if direction == "long":
        if position > 0 and slope > 0: return "aligned"
        if position < 0 and slope < 0: return "opposed"
    else:
        if position < 0 and slope < 0: return "aligned"
        if position > 0 and slope > 0: return "opposed"
    return "mixed"


def excursion_metrics(trade: dict[str, Any], hourly: list[dict[str, str]],
                      open_times: list[int]) -> dict[str, Any]:
    entry_ms, exit_ms = _ms(trade["entry_time_utc"]), _ms(trade["exit_time_utc"])
    # exit_time_utc is an exclusive end boundary (exit candle close + 1 ms).
    # Its equality to the following candle's open must NOT include that candle.
    if exit_ms <= entry_ms:
        raise ValueError("Exit boundary must be later than entry time")
    first, last = bisect.bisect_left(open_times, entry_ms), bisect.bisect_left(open_times, exit_ms)
    window = hourly[first:last]
    if not window:
        raise ValueError(f"No hourly candles for trade {trade.get('setup_id')}")
    direction, entry = trade["direction"], float(trade["raw_entry_price"])
    risk = abs(entry - float(trade["stop_price"]))
    if risk <= 0: raise ValueError(f"Invalid initial risk for trade {trade.get('setup_id')}")
    if direction == "long":
        favorable, adverse = max(float(r["high"]) for r in window)-entry, entry-min(float(r["low"]) for r in window)
        fi = max(range(len(window)), key=lambda i: float(window[i]["high"]))
        ai = min(range(len(window)), key=lambda i: float(window[i]["low"]))
    else:
        favorable, adverse = entry-min(float(r["low"]) for r in window), max(float(r["high"]) for r in window)-entry
        fi = min(range(len(window)), key=lambda i: float(window[i]["low"]))
        ai = max(range(len(window)), key=lambda i: float(window[i]["high"]))
    return {
        "bars_in_window": len(window), "mfe_price": round(favorable, 8),
        "mae_price": round(adverse, 8), "mfe_pct_of_entry": round(favorable/entry*100, 6),
        "mae_pct_of_entry": round(adverse/entry*100, 6), "mfe_r": round(favorable/risk, 6),
        "mae_r": round(adverse/risk, 6), "bars_to_mfe": fi+1, "bars_to_mae": ai+1,
        "mfe_time_utc": window[fi]["open_time_utc"], "mae_time_utc": window[ai]["open_time_utc"],
        "includes_exit_candle": True,
    }


def _summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [t for t in trades if float(t["r_multiple"]) > 0]
    losers = [t for t in trades if float(t["r_multiple"]) <= 0]
    stops = [t for t in trades if t["exit_reason"] in {"stop", "stop_gap"}]
    def mean(key: str) -> float | None:
        return round(sum(float(t[key]) for t in trades)/len(trades), 6) if trades else None
    return {
        "trades": len(trades), "wins": len(wins), "losses_and_breakeven": len(losers),
        "win_rate_percent": round(100*len(wins)/len(trades), 4) if trades else 0.0,
        "mean_mfe_r": mean("mfe_r"), "mean_mae_r": mean("mae_r"),
        "rapid_stops_le_2_bars": sum(int(t["bars_held"]) <= 2 for t in stops),
        "stop_count": len(stops),
        "losers_reached_mfe_0_5r": sum(float(t["mfe_r"]) >= .5 for t in losers),
        "losers_reached_mfe_1r": sum(float(t["mfe_r"]) >= 1 for t in losers),
        "trend_aligned": sum(t["trend_4h_alignment"] == "aligned" for t in trades),
        "trend_opposed": sum(t["trend_4h_alignment"] == "opposed" for t in trades),
        "trend_mixed_or_unknown": sum(t["trend_4h_alignment"] in {"mixed", "unknown"} for t in trades),
    }


def run_diagnostics(data_dir: Path, input_report: Path, output_json: Path,
                    output_csv: Path) -> dict[str, Any]:
    report = json.loads(input_report.read_text(encoding="utf-8"))
    hourly, four, daily = (_read_csv(data_dir / f"BTCUSDT_{i}.csv") for i in ("1h", "4h", "1d"))
    h_open = [int(r["open_time"]) for r in hourly]
    four_ema = _ema20_by_segment(four, 4*60*60*1000)
    day_ema = _ema20_by_segment(daily, 24*60*60*1000)
    four_close = [int(r["close_time"])+1 for r in four]
    day_close = [int(r["close_time"])+1 for r in daily]
    variants, all_rows = {}, []
    for name, variant in report["variants"].items():
        enriched = []
        for trade in variant["trades"]:
            row = dict(trade)
            row.update(excursion_metrics(trade, hourly, h_open))
            signal = _ms(trade["signal_time_utc"])
            c4 = _context_at(four, four_ema, four_close, signal)
            c1d = _context_at(daily, day_ema, day_close, signal)
            row["trend_4h_alignment"] = _trend_alignment(trade["direction"], c4)
            row["trend_1d_alignment"] = _trend_alignment(trade["direction"], c1d)
            row["4h_context"], row["1d_context"] = c4, c1d
            enriched.append(row)
            flat = {k:v for k,v in row.items() if not isinstance(v,dict)}
            flat.update({"4h_ema20_position_pct":c4["position_pct"],
                         "4h_ema20_slope_pct_3bars":c4["slope_pct_3bars"],
                         "1d_ema20_position_pct":c1d["position_pct"],
                         "1d_ema20_slope_pct_3bars":c1d["slope_pct_3bars"]})
            all_rows.append({"variant":name, **flat})
        splits = {}
        for split in ("train","validation","test"):
            subset = [t for t in enriched if t["split"] == split]
            splits[split] = {
                "all":_summary(subset),
                "winners":_summary([t for t in subset if float(t["r_multiple"])>0]),
                "losers":_summary([t for t in subset if float(t["r_multiple"])<=0]),
                "by_4h_trend":{k:_summary([t for t in subset if t["trend_4h_alignment"]==k])
                               for k in ("aligned","opposed","mixed","unknown")},
            }
        variants[name] = {"trade_count":len(enriched),"splits":splits,"trades":enriched}
    output = {
        "report_type":"btc_mtf_trade_diagnostics","report_version":2,
        "strategy":report.get("strategy"),"data":report.get("data"),
        "definitions":{
            "MFE":"Maximum favorable hourly high/low excursion from raw entry between entry and exit.",
            "MAE":"Maximum adverse hourly low/high excursion from raw entry between entry and exit.",
            "MFE_R_and_MAE_R":"Excursion divided by raw entry-to-stop distance.",
            "trend_alignment":"At signal time, last fully closed 4H/daily close vs EMA20 and 3-bar EMA20 slope both agree with trade direction for aligned/opposed; otherwise mixed.",
            "exit_candle_caveat":"The exit-time timestamp is exclusive; only bars opened strictly before it are included. Entry/exit candle high-low order and entry fill time cannot be resolved from 1H OHLC.",
            "scope":"Diagnostics describe existing trades only; no strategy rule is changed.",
        },
        "variants":variants,
    }
    output_json.parent.mkdir(parents=True,exist_ok=True)
    output_json.write_text(json.dumps(output,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    if all_rows:
        with output_csv.open("w",encoding="utf-8-sig",newline="") as stream:
            writer=csv.DictWriter(stream,fieldnames=list(all_rows[0]))
            writer.writeheader(); writer.writerows(all_rows)
    return output


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir",type=Path,default=Path("data/historical"))
    p.add_argument("--input",type=Path,default=Path("data/historical/BTCUSDT_stop_loss_research.json"))
    p.add_argument("--output-json",type=Path,default=Path("data/historical/BTCUSDT_trade_diagnostics.json"))
    p.add_argument("--output-csv",type=Path,default=Path("data/historical/BTCUSDT_trade_diagnostics.csv"))
    a=p.parse_args()
    r=run_diagnostics(a.data_dir,a.input,a.output_json,a.output_csv)
    print(f"逐筆診斷報告：{a.output_json}")
    print(f"逐筆 CSV：{a.output_csv}")
    for name,v in r["variants"].items():
        x=v["splits"]["test"]["all"]
        print(f"{name} test: trades={x['trades']}, mean_MFE={x['mean_mfe_r']}R, mean_MAE={x['mean_mae_r']}R, losers_MFE>=0.5R={x['losers_reached_mfe_0_5r']}, 4H_aligned={x['trend_aligned']}, 4H_opposed={x['trend_opposed']}")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
