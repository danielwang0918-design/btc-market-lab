#!/usr/bin/env python3
"""P0-ORDER-STATE-02: offline evidence gate for 1H+ frozen state replays.

Research-only. This module reads ONLY reports made from closed 1H/4H/1D
candles. It never accepts finer K-line inputs, forecasts trade outcomes,
performs fills, optimizes filters, or places orders.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = 'p0-order-state-evidence-gate-v1'
SOURCE_VERSION = 'order-state-1h-ob-observational-v1'
SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
SPLITS = ('train', 'validation', 'test')
TIMEFRAMES = ('1h', '4h', '1d')
ACCEPT = 'signal_accepted_pending_from_closed_1h'
FILL = 'pending_ob_wick_touch_modeled_fill'
UNRESOLVED = 'pending_split_end_unresolved'
SAME_HOUR = 'pending_target_and_zone_same_hour_cancel'
ORDERINGS = (
    'target_already_at_open_before_zone_approach',
    'zone_present_at_open_but_reference_fill_unproven',
    'intrabar_target_vs_zone_order_unknown',
)
TERMINALS = {
    FILL, UNRESOLVED, 'pending_data_gap_cancel',
    'pending_expired_or_integrity_cancel',
    'pending_stop_already_breached_at_open_cancel',
    'pending_target_without_zone_cancel', SAME_HOUR,
    'pending_signal_changed_cancel',
}
CANCEL = TERMINALS - {FILL, UNRESOLVED}


class AuditEvidenceError(ValueError):
    """Missing or inconsistent replay evidence: fail closed."""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _utc(raw: str) -> datetime:
    try:
        result = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except (ValueError, AttributeError) as e:
        raise AuditEvidenceError('Invalid timestamp in event evidence') from e
    if result.tzinfo is None:
        raise AuditEvidenceError('Naive timestamp in event evidence')
    return result.astimezone(timezone.utc)


def _finite_int(v: Any, name: str) -> int:
    if type(v) is not int or v < 0:
        raise AuditEvidenceError(f'Invalid nonnegative integer {name}')
    return v


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AuditEvidenceError(f'Missing required frozen replay: {path.name}')
    try:
        r = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError) as e:
        raise AuditEvidenceError(f'Cannot read frozen replay: {path.name}') from e
    if not isinstance(r, dict):
        raise AuditEvidenceError('Replay must be JSON object')
    return r


def audit_symbol(report_path: Path, events_path: Path, expected_symbol: str) -> dict[str, Any]:
    """Validate accepted -> exactly one pending terminal transition per split/setup.

    The strict one-terminal rule checks the replay accounting, not actual fills.
    Strict schema prohibits posthoc relabeling of ambiguous OB+target candles.
    """
    r = _load(report_path)
    if (r.get('symbol') != expected_symbol or r.get('version') != SOURCE_VERSION
        or r.get('strategy_unchanged') is not True
        or r.get('subhour_price_data_used') is not False
        or r.get('orders_placed') is not False
        or tuple(r.get('decision_timeframes_only', ())) != TIMEFRAMES):
        raise AuditEvidenceError(f'Provenance/1H-only/strategy mismatch: {expected_symbol}')
    if not isinstance(r.get('splits'), dict) or set(r['splits']) != set(SPLITS):
        raise AuditEvidenceError('Missing or unexpected split in replay')
    if not events_path.is_file():
        raise AuditEvidenceError(f'Missing event CSV for {expected_symbol}')
    with events_path.open('r', newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        fields = set(reader.fieldnames or [])
        expected_cols = {'symbol','split','event','setup_id','at_1h_open_utc',
                         'ordering','intra_hour_fill_observed', 'actual_fill_observed'}
        if not expected_cols.issubset(fields):
            raise AuditEvidenceError('Incomplete event CSV schema')
        events = list(reader)
    times: dict[str, datetime] = {}
    accepted: dict[str, dict[str, datetime]] = defaultdict(dict)
    terminal: dict[str, dict[str, str]] = defaultdict(dict)
    count: dict[str, Counter[str]] = defaultdict(Counter)
    overlap_groups: dict[str, Counter[str]] = defaultdict(Counter)
    for n, event in enumerate(events):
        split = event['split']
        if event['symbol'] != expected_symbol or split not in SPLITS:
            raise AuditEvidenceError(f'Wrong symbol/split at event row {n+1}')
        kind = event['event']
        known_exits = {'position_data_gap_exit','position_exit','position_integrity_exit',
                       'position_same_hour_exit','position_split_end_exit'}
        if kind not in ({ACCEPT} | TERMINALS | known_exits):
            raise AuditEvidenceError('Unknown replay event: refuse silent drop')
        setup = event['setup_id']
        if not setup or not kind:
            raise AuditEvidenceError('Missing setup or event name')
        stamp = _utc(event['at_1h_open_utc'])
        if int(stamp.timestamp()) % 3600:
            raise AuditEvidenceError('Event is not aligned to a 1H candle')
        if split in times and stamp < times[split]:
            raise AuditEvidenceError('Events out of time order within split')
        times[split] = stamp
        count[split][kind] += 1
        if kind == ACCEPT:
            if setup in accepted[split]:
                raise AuditEvidenceError('Duplicate accepted signal/setup within split')
            accepted[split][setup] = stamp
        elif kind in TERMINALS:
            if setup not in accepted[split]:
                raise AuditEvidenceError('A terminal event has no preceding accepted signal')
            if setup in terminal[split]:
                raise AuditEvidenceError('Accepted setup has multiple terminal outcomes')
            if stamp < accepted[split][setup]:
                raise AuditEvidenceError('Terminal precedes signal')
            terminal[split][setup] = kind
            if kind == SAME_HOUR:
                ordering = event['ordering']
                if ordering not in ORDERINGS:
                    raise AuditEvidenceError('Unsupported sequence classification')
                if event['intra_hour_fill_observed'].lower() != 'false':
                    raise AuditEvidenceError('1H overlap must never claim a verified fill')
                overlap_groups[split][ordering] += 1
        elif kind.startswith('pending_'):
            raise AuditEvidenceError('Unknown pending transition: refuse silent drop')
        if kind in {FILL, SAME_HOUR}:
            if kind == FILL and event['actual_fill_observed'].lower() != 'false':
                raise AuditEvidenceError('Wick touch is not evidence of an actual fill')
    out: dict[str, Any] = {}
    for split in SPLITS:
        report = r['splits'][split]
        if not isinstance(report, dict):
            raise AuditEvidenceError('Malformed per-split replay report')
        if set(accepted[split]) != set(terminal[split]):
            raise AuditEvidenceError(f'{expected_symbol} {split}: missing terminal(s) or missing acceptance(s)')
        terminal_counts = Counter(terminal[split].values())
        modeled = terminal_counts[FILL]
        canceled = sum(terminal_counts[k] for k in CANCEL)
        unresolved = terminal_counts[UNRESOLVED]
        checks = {
            'observed_signal_count': len(accepted[split]),
            'modeled_fills': modeled,
            'no_fill_pending_cancellations': canceled,
            'pending_at_split_end_unresolved': unresolved,
            'target_zone_same_1h_cancellations': terminal_counts[SAME_HOUR],
        }
        for field, calc in checks.items():
            if _finite_int(report.get(field), field) != calc:
                raise AuditEvidenceError(f'{expected_symbol}/{split} count mismatch: {field}')
        if _finite_int(report.get('baseline_trades_verified'), 'baseline_trades_verified') != modeled:
            raise AuditEvidenceError('Frozen baseline trade count does not match modeled fills')
        recorded_counts = report.get('event_counts', {})
        if not isinstance(recorded_counts, dict) or dict(count[split]) != recorded_counts:
            raise AuditEvidenceError(f'{expected_symbol}/{split}: event taxonomy counts mismatch')
        if report.get('target_zone_sequence_classification', {}) != dict(overlap_groups[split]):
            raise AuditEvidenceError(f'{expected_symbol}/{split}: OHLC ordering taxonomy mismatch')
        out[split] = {
            **checks, 'baseline_trades_verified': modeled,
            'closed_loop_accounting': len(accepted[split]) == modeled + canceled + unresolved,
            'event_counts': dict(count[split]),
            'same_hour_target_zone_classification': dict(overlap_groups[split]),
            'irreducibly_ambiguous_same_hour_orders': terminal_counts[SAME_HOUR],
            'min_claimable_recovered_profitable_trades': 0,
            'validation_basis': '1h/4h/1d replay outputs, not execution fills',
        }
    return {
        'symbol': expected_symbol, 'report_sha256': sha256(report_path),
        'events_sha256': sha256(events_path), 'splits': out,
    }


def build_report(source_dir: Path, output_dir: Path, *, symbols: tuple[str,...] = SYMBOLS) -> dict[str, Any]:
    if symbols != SYMBOLS:
        raise AuditEvidenceError('Full BTC/ETH/SOL evidence required, in frozen provenance order')
    by = {}
    for symbol in symbols:
        by[symbol] = audit_symbol(source_dir / f'{symbol}_order_state_audit.json',
                                  source_dir / f'{symbol}_order_state_events.csv', symbol)
    totals: dict[str, Counter[str]] = defaultdict(Counter)
    ordering: dict[str, Counter[str]] = defaultdict(Counter)
    for x in by.values():
        for split in SPLITS:
            v=x['splits'][split]
            for key in ('observed_signal_count','modeled_fills','no_fill_pending_cancellations',
                        'pending_at_split_end_unresolved','target_zone_same_1h_cancellations'):
                totals[split][key]+=v[key]
            ordering[split].update(v['same_hour_target_zone_classification'])
    for split in SPLITS:
        ts=totals[split]
        if ts['observed_signal_count'] != (ts['modeled_fills']+ts['no_fill_pending_cancellations']+
                                          ts['pending_at_split_end_unresolved']):
            raise AuditEvidenceError('Overall signal terminal accounting does not balance')
    out = {
        'version': VERSION, 'status': 'verified_1h_only_observational',
        'source_replay_version': SOURCE_VERSION,
        'decision_timeframes_only': list(TIMEFRAMES), 'subhour_used': False,
        'orders_placed': False, 'strategy_changed': False,
        'source_symbols': list(symbols), 'by_symbol': by,
        'split_totals': {k: dict(totals[k]) for k in SPLITS},
        'target_zone_ordering': {k: dict(ordering[k]) for k in SPLITS},
        'cannot_infer_recovered_win_rate': True,
        'limitations': [
            'Only closed 1H/4H/1D decisions. No 1m/15m candles or trading API.',
            'Wick-touch modeled entry is not a verifiable executed limit order.',
            'OB+target both touched in one 1H candle do not reveal first-touch order.',
            'No recovered trade, profit or changed win rate is inferred from canceled setups.',
            'The Test period was previously inspected; this is not independent forward validation.',
            'Current-volume-selected symbol universe contains survivorship and selection bias.',
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    js = output_dir / 'order_state_evidence_gate.json'
    js.write_text(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False)+'\n',encoding='utf-8')
    lines = ['# P0-ORDER-STATE-02｜OB 掛單閉環驗收', '',
             '只使用 1H／4H／1D 閉合 K 線決策資料；無實盤下單、無勝率調整。', '',
             '| 幣種 | Test 訊號 | 模擬成交 | 未成交取消 | 未結掛單 | 同根 OB＋目標取消 |',
             '|---|---:|---:|---:|---:|---:|']
    for symbol in symbols:
        x=by[symbol]['splits']['test']
        lines.append(f"| {symbol} | {x['observed_signal_count']} | {x['modeled_fills']} | {x['no_fill_pending_cancellations']} | {x['pending_at_split_end_unresolved']} | {x['target_zone_same_1h_cancellations']} |")
    t=totals['test']
    lines += [f"| **合計** | **{t['observed_signal_count']}** | **{t['modeled_fills']}** | **{t['no_fill_pending_cancellations']}** | **{t['pending_at_split_end_unresolved']}** | **{t['target_zone_same_1h_cancellations']}** |",
              '', '## 同根 OB＋目標價的 1H 時間順序分類', '']
    for k in ORDERINGS: lines.append(f'- {k}: {ordering["test"].get(k,0)}')
    lines += ['', '## 方法限制', '']+['- '+w for w in out['limitations']]
    (output_dir/'order_state_evidence_gate.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return out


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-dir',type=Path,required=True)
    p.add_argument('--output-dir',type=Path,required=True)
    a=p.parse_args()
    r=build_report(a.source_dir,a.output_dir)
    print(json.dumps({'status':r['status'],'test_totals':r['split_totals']['test']},ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())