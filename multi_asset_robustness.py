#!/usr/bin/env python3
"""Calendar-clustered descriptive robustness report for existing multi-asset MTF trades.

Research only. NEVER tunes strategy parameters, creates orders or claims independent
out-of-sample validation from a previously opened Test window.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

VERSION = "multi-asset-robustness-v1"
SPLITS = ("train", "validation", "test")
FIELDS = ("symbol", "split", "setup_id", "direction", "signal_time_utc", "entry_time_utc",
          "exit_time_utc", "exit_reason", "r_multiple", "bars_held")
SEED = 20261116
BOOTSTRAP_REPLICATES = 2000


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _dt(value: str, field: str) -> datetime:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid {field}") from exc
    if dt.tzinfo is None:
        raise ValueError(f"Timezone missing in {field}")
    return dt.astimezone(timezone.utc)


def _number(value: Any, field: str) -> float:
    try:
        n = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"Invalid {field}") from exc
    if not math.isfinite(n):
        raise ValueError(f"Non-finite {field}")
    return n


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        raise ValueError("Empty percentile series")
    arr = sorted(vals)
    offset = (len(arr) - 1) * p
    lo = math.floor(offset)
    hi = math.ceil(offset)
    return arr[lo] * (hi - offset) + arr[hi] * (offset - lo) if hi != lo else arr[lo]


def _metric(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    values = [float(x["r"]) for x in rows]
    winners = sum(v > 0 for v in values)
    profit = sum(v for v in values if v > 0)
    loss = -sum(v for v in values if v < 0)
    return {
        "trades": n,
        "wins": winners,
        "win_rate_pct": round(100 * winners / n, 4) if n else None,
        "expectancy_r": round(sum(values) / n, 6) if n else None,
        "profit_factor": round(profit / loss, 6) if loss else ("infinite" if profit else None),
    }


def _weekly_cluster(rows: list[dict[str, Any]], start: datetime, end: datetime,
                    *, replicas: int = BOOTSTRAP_REPLICATES, seed: int = SEED) -> dict[str, Any]:
    """Sample *all* UTC weeks including weeks without trades, jointly across symbols.

    It preserves within-week co-movement, but not serial dependence across weeks.
    Not a bootstrap confidence interval for a point-in-time selected investable universe.
    """
    if end <= start:
        raise ValueError("Invalid weekly resampling window")
    if type(replicas) is not int or not 100 <= replicas <= 10000:
        raise ValueError("replicas must be 100..10000")
    week0 = (start - timedelta(days=start.weekday())).date()
    last = (end - timedelta(microseconds=1) - timedelta(days=(end - timedelta(microseconds=1)).weekday())).date()
    size = (last - week0).days // 7 + 1
    weeks = [[0, 0, 0.0] for _ in range(size)]
    for t in rows:
        opened = t["entry"]
        if not start <= opened < end:
            raise ValueError("Trade outside its calendar split")
        idx = (opened.date() - timedelta(days=opened.weekday()) - week0).days // 7
        if idx < 0 or idx >= size:
            raise ValueError("Week grouping outside split")
        weeks[idx][0] += 1
        weeks[idx][1] += int(t["r"] > 0)
        weeks[idx][2] += t["r"]
    trading_weeks = sum(w[0] > 0 for w in weeks)
    answer: dict[str, Any] = {
        "calendar_weeks": size, "weeks_with_trades": trading_weeks,
        "sampled_calendar_weeks_per_replica": size,
        "replicates": replicas, "seed": seed,
        "cluster_key": "UTC_calendar_week_all_symbols_jointly",
        "method": "Calendar-week resampling with replacement, includes zero-trade weeks",
        "win_rate_pct_95pct_descriptive": None,
        "expectancy_r_95pct_descriptive": None,
        "usable_replicates": 0,
    }
    if not rows:
        return answer
    rng = random.Random(seed)
    rates: list[float] = []
    expectancies: list[float] = []
    for _ in range(replicas):
        n = w = 0
        total = 0.0
        for _ in range(size):
            ct, wins, r_sum = weeks[rng.randrange(size)]
            n += ct
            w += wins
            total += r_sum
        if n > 0:
            rates.append(100 * w / n)
            expectancies.append(total / n)
    answer["usable_replicates"] = len(rates)
    if len(rates) >= 100:
        answer["win_rate_pct_95pct_descriptive"] = [round(_pct(rates, .025), 4), round(_pct(rates, .975), 4)]
        answer["expectancy_r_95pct_descriptive"] = [round(_pct(expectancies, .025), 6), round(_pct(expectancies, .975), 6)]
    return answer


def build_report(aggregate_path: Path, trades_path: Path, output_dir: Path,
                 *, replicas: int = BOOTSTRAP_REPLICATES, seed: int = SEED) -> dict[str, Any]:
    agg = json.loads(aggregate_path.read_text(encoding="utf-8"))
    if agg.get("version") != "multi-asset-calendar-baseline-v1" or agg.get("report_type") != "multi_asset_fixed_baseline_aggregate":
        raise ValueError("Unexpected aggregate baseline version")
    if agg.get("status") != "complete" or agg.get("missing_or_failed_symbols"):
        raise ValueError("Baseline incomplete: no robustness conclusions allowed")
    symbols = agg.get("complete_symbols")
    if not isinstance(symbols, list) or len(set(symbols)) != len(symbols) or not symbols:
        raise ValueError("Missing or duplicated completed symbols")
    universe = agg.get("symbols_in_universe", [])
    if not isinstance(universe, list) or not set(symbols).issubset(set(universe)):
        raise ValueError("Complete symbols not in frozen universe")
    bounds = agg.get("fixed_utc_bounds")
    if not isinstance(bounds, dict):
        raise ValueError("Missing frozen calendar boundaries")
    split_times = {
        "train": (None, _dt(bounds["train_end_exclusive"], "train end")),
        "validation": (_dt(bounds["train_end_exclusive"], "train end"), _dt(bounds["validation_end_exclusive"], "validation end")),
        "test": (_dt(bounds["validation_end_exclusive"], "validation end"), _dt(bounds["test_end_exclusive"], "test end")),
    }
    if not split_times["train"][1] < split_times["validation"][1] < split_times["test"][1]:
        raise ValueError("Non-increasing fixed calendar boundaries")
    with trades_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not set(FIELDS).issubset(reader.fieldnames or []):
            raise ValueError("Incomplete trade CSV schema")
        original = list(reader)
    seen: set[tuple[str, str, str]] = set()
    groups: dict[str, list[dict[str, Any]]] = {s: [] for s in SPLITS}
    for row in original:
        symbol, split = row["symbol"], row["split"]
        if symbol not in symbols or split not in SPLITS or not row["setup_id"]:
            raise ValueError("Unknown symbol/split/setup in trades")
        key = (symbol, split, row["setup_id"])
        if key in seen:
            raise ValueError("Duplicate setup within symbol/split")
        seen.add(key)
        entry = _dt(row["entry_time_utc"], "entry")
        signal = _dt(row["signal_time_utc"], "signal")
        exit_time = _dt(row["exit_time_utc"], "exit")
        start, end = split_times[split]
        if not (signal <= entry < exit_time <= end) or (start is not None and entry < start):
            raise ValueError("Trade timing outside split or causality")
        if row["direction"] not in ("long", "short"):
            raise ValueError("Invalid trade direction")
        if int(row["bars_held"]) <= 0:
            raise ValueError("Invalid bars_held")
        groups[split].append({"symbol": symbol, "split": split, "entry": entry,
                              "signal": signal, "exit": exit_time, "r": _number(row["r_multiple"], "r_multiple")})
    for split in SPLITS:
        reported = agg.get("pooled_observational", {}).get(split, {})
        if reported.get("trade_count") != len(groups[split]):
            raise ValueError(f"{split}: pooled count mismatches raw evidence")
        wins = sum(t["r"] > 0 for t in groups[split])
        if reported.get("wins") != wins:
            raise ValueError(f"{split}: pooled wins mismatches raw evidence")
        for symbol in symbols:
            subset = [x for x in groups[split] if x["symbol"] == symbol]
            symbol_report = agg.get("by_symbol", {}).get(symbol, {}).get("splits", {}).get(split, {})
            if (symbol_report.get("trade_count") != len(subset)
                    or symbol_report.get("wins") != sum(x["r"] > 0 for x in subset)):
                raise ValueError(f"{symbol} {split}: per-symbol count/wins mismatch")
    split_reports = {}
    for split, trades in groups.items():
        per_symbol = {sym: _metric([t for t in trades if t["symbol"] == sym]) for sym in symbols}
        active = [v for v in per_symbol.values() if v["trades"]]
        avg_symbol_r = (sum(v["expectancy_r"] for v in active) / len(active)) if active else None
        sizes = [v["trades"] for v in per_symbol.values()]
        n = sum(sizes)
        share_max = max(sizes) / n if n else None
        # 1/sum(share^2) is an exposure concentration proxy, NOT independent sample size.
        eff_symbols = n * n / sum(v * v for v in sizes) if n else None
        per_year = {}
        for year in sorted({x["entry"].year for x in trades}):
            per_year[str(year)] = _metric([x for x in trades if x["entry"].year == year])
        week_coverage = {}
        per_week = defaultdict(set)
        for t in trades:
            monday = t["entry"].date() - timedelta(days=t["entry"].weekday())
            per_week[monday].add(t["symbol"])
        week_coverage = {
            "unique_entry_weeks": len(per_week),
            "weeks_with_multi_asset_entries": sum(len(v) > 1 for v in per_week.values()),
            "max_symbols_same_week": max((len(v) for v in per_week.values()), default=0),
        }
        start, end = split_times[split]
        boot = (_weekly_cluster(trades, start, end, replicas=replicas, seed=seed)
                if start else None)  # train start is undefined across different listings.
        loso = {}
        for omitted in symbols:
            rest = [t for t in trades if t["symbol"] != omitted]
            loso[omitted] = _metric(rest)
        split_reports[split] = {
            "pooled": _metric(trades),
            "per_symbol": per_symbol,
            "symbols_with_trades": len(active),
            "equal_active_symbol_mean_expectancy_r": round(avg_symbol_r, 6) if avg_symbol_r is not None else None,
            "maximum_single_symbol_trade_share_pct": round(100 * share_max, 3) if share_max is not None else None,
            "symbol_concentration_proxy_1_over_HHI": round(eff_symbols, 4) if eff_symbols is not None else None,
            "per_entry_year": per_year,
            "same_week_overlap": week_coverage,
            "leave_one_symbol_out": loso,
            "calendar_week_cluster_bootstrap": boot,
        }
    test = split_reports["test"]
    warnings = [
        "Only repeated analysis of previously viewed Test: NOT new holdout evidence or a strategy improvement.",
        "Present-day volume universe creates survivorship and hindsight selection bias.",
        "UTC-week joint resampling preserves within-week cross-asset correlation, but not between-week dependence or historical selection bias.",
        "No portfolio capital, overlapping-position risk, funding, spread or actual order fills; pooled R is NOT an equity curve.",
        "No parameter selection, entry/exit logic changes, causal return forecast or live order placement.",
        "Concentration proxy is NOT the effective independent sample size.",
    ]
    result = {
        "report_type": "multi_asset_calendar_robustness_observational",
        "version": VERSION,
        "source_baseline_version": agg["version"],
        "status": "descriptive_only_not_independent_validation",
        "selection_mode": agg.get("selection_mode"),
        "asof_utc": agg.get("universe_selection_asof_utc"),
        "input_sha256": {aggregate_path.name: _sha(aggregate_path), trades_path.name: _sha(trades_path)},
        "fixed_utc_bounds": bounds,
        "eligible_symbols": symbols,
        "excluded_history_symbols": agg.get("excluded_short_history_symbols", []),
        "split_summaries": split_reports,
        "test_trade_count": test["pooled"]["trades"],
        "test_has_at_least_100_trades": test["pooled"]["trades"] >= 100,
        "claim_of_positive_strategy_edge": False,
        "warnings": warnings,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "multi_asset_robustness.json"
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)
    lines = ["# MULTI-ASSET-03｜跨幣種穩健性觀察報告", "",
             "本報告不是新的樣本外驗證，也不代表投資組合績效。原策略及交易成本完全未改。", "",
             "| 時段 | 交易數 | 勝率 | Expectancy (R) | 交易最多單一幣種占比 | 交易重疊週數 |",
             "|---|---:|---:|---:|---:|---:|"]
    for split in SPLITS:
        x = split_reports[split]
        m = x["pooled"]
        lines.append(f"| {split} | {m['trades']} | {m['win_rate_pct']}% | {m['expectancy_r']} | {x['maximum_single_symbol_trade_share_pct']}% | {x['same_week_overlap']['weeks_with_multi_asset_entries']} |")
    lines.extend(["", "## 逐幣種 Test 觀察", "", "| 幣種 | 交易 | 勝率 | Expectancy (R) |", "|---|---:|---:|---:|"])
    for sym, m in test["per_symbol"].items():
        lines.append(f"| {sym} | {m['trades']} | {m['win_rate_pct']}% | {m['expectancy_r']} |")
    lines += ["", "## Test：UTC 週聚類重抽樣（描述性）", "",
              "此區間是依已觀察 Test 的同週跨幣種交易一起重抽樣；不是獨立的未來績效信賴區間。", "",
              "- 勝率區間：" + str(test["calendar_week_cluster_bootstrap"]["win_rate_pct_95pct_descriptive"]),
              "- 單筆 R 期望值區間：" + str(test["calendar_week_cluster_bootstrap"]["expectancy_r_95pct_descriptive"]),
              "- 交易最多幣種占比：" + str(test["maximum_single_symbol_trade_share_pct"]) + "%",
              "- 留一幣種結果可在 JSON 的 leave_one_symbol_out 查詢。", "",
              "## 必要限制", ""] + ["- " + w for w in warnings]
    outmd = output_dir / "multi_asset_robustness.md"
    tmpmd = outmd.with_suffix(".md.tmp")
    tmpmd.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmpmd.replace(outmd)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Descriptive multi-asset robustness, no tuning or trading")
    parser.add_argument("--baseline", type=Path, default=Path("research/multi_asset/summary/multi_asset_fixed_calendar_baseline.json"))
    parser.add_argument("--trades", type=Path, default=Path("research/multi_asset/summary/multi_asset_fixed_calendar_trades.csv"))
    parser.add_argument("--output-dir", type=Path, default=Path("research/multi_asset/robustness"))
    parser.add_argument("--replicas", type=int, default=BOOTSTRAP_REPLICATES)
    args = parser.parse_args(argv)
    report = build_report(args.baseline, args.trades, args.output_dir, replicas=args.replicas)
    print(json.dumps({"status": report["status"], "test_trade_count": report["test_trade_count"],
                      "test_expectancy_r": report["split_summaries"]["test"]["pooled"]["expectancy_r"],
                      "output_dir": str(args.output_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())