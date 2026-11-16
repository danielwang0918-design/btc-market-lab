"""All inputs are fake 1H replay events; no subhour bars, network or orders."""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from copy import deepcopy

import order_state_evidence_gate as m

FIELDS=['symbol','split','event','setup_id','at_1h_open_utc', 'ordering','intra_hour_fill_observed','actual_fill_observed']


def fixture(folder, symbol='BTCUSDT', *, kind=m.FILL, order=''):
    p=Path(folder)
    events=[]
    splits={}
    for si,split in enumerate(m.SPLITS):
        at=f'2026-01-0{si+1}T00:00:00+00:00'
        for k in (m.ACCEPT,kind):
            events.append(dict(symbol=symbol,split=split,event=k,setup_id='x1',
                         at_1h_open_utc=at,ordering=(order if k==m.SAME_HOUR else ''),
                         intra_hour_fill_observed='false',actual_fill_observed='false'))
        splits[split]={'baseline_trades_verified':int(kind==m.FILL),
             'observed_signal_count':1,'modeled_fills':int(kind==m.FILL),
             'no_fill_pending_cancellations':int(kind in m.CANCEL),
             'target_zone_same_1h_cancellations':int(kind==m.SAME_HOUR),
             'pending_at_split_end_unresolved':int(kind==m.UNRESOLVED),
             'event_counts':{m.ACCEPT:1,kind:1},
             'target_zone_sequence_classification':{order:1} if kind==m.SAME_HOUR else {}}
    report=dict(symbol=symbol,version=m.SOURCE_VERSION,strategy_unchanged=True,orders_placed=False,
         subhour_price_data_used=False,decision_timeframes_only=list(m.TIMEFRAMES),splits=splits)
    with (p/f'{symbol}_order_state_events.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=FIELDS); writer.writeheader();writer.writerows(events)
    (p/f'{symbol}_order_state_audit.json').write_text(json.dumps(report),encoding='utf-8')
    return report,events


def edit_events(root, symbol, mutate):
    path=Path(root)/f'{symbol}_order_state_events.csv'
    with path.open('r',encoding='utf-8-sig',newline='') as f: rows=list(csv.DictReader(f))
    mutate(rows)
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        wr=csv.DictWriter(f,fieldnames=FIELDS);wr.writeheader();wr.writerows(rows)


def edit_report(root,symbol,mutate):
    path=Path(root)/f'{symbol}_order_state_audit.json'
    data=json.loads(path.read_text());mutate(data)
    path.write_text(json.dumps(data))


class EvidenceGateTests(unittest.TestCase):
    def test_valid_fill_passes_three_splits(self):
        with tempfile.TemporaryDirectory() as root:
            fixture(root)
            a=m.audit_symbol(Path(root)/'BTCUSDT_order_state_audit.json',Path(root)/'BTCUSDT_order_state_events.csv','BTCUSDT')
            self.assertTrue(a['splits']['test']['closed_loop_accounting'])
            self.assertEqual(a['splits']['test']['modeled_fills'],1)

    def test_valid_cancelled_same_hour_is_not_marked_winner(self):
        with tempfile.TemporaryDirectory() as root:
            fixture(root,kind=m.SAME_HOUR,order=m.ORDERINGS[-1])
            a=m.audit_symbol(Path(root)/'BTCUSDT_order_state_audit.json',Path(root)/'BTCUSDT_order_state_events.csv','BTCUSDT')
            self.assertEqual(a['splits']['test']['target_zone_same_1h_cancellations'],1)
            self.assertEqual(a['splits']['test']['min_claimable_recovered_profitable_trades'],0)

    def test_valid_unresolved(self):
        with tempfile.TemporaryDirectory() as root:
            fixture(root,kind=m.UNRESOLVED)
            a=m.audit_symbol(Path(root)/'BTCUSDT_order_state_audit.json',Path(root)/'BTCUSDT_order_state_events.csv','BTCUSDT')
            self.assertEqual(a['splits']['test']['pending_at_split_end_unresolved'],1)

    def test_missing_csv_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            fixture(root)
            with self.assertRaises(m.AuditEvidenceError):
                m.audit_symbol(Path(root)/'BTCUSDT_order_state_audit.json',Path(root)/'missing.csv','BTCUSDT')

    def test_subhour_flag_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            fixture(root)
            edit_report(root,'BTCUSDT',lambda a:a.__setitem__('subhour_price_data_used',True))
            with self.assertRaises(m.AuditEvidenceError):
                m.audit_symbol(Path(root)/'BTCUSDT_order_state_audit.json',Path(root)/'BTCUSDT_order_state_events.csv','BTCUSDT')

    def test_duplicate_signal_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            fixture(root)
            edit_events(root,'BTCUSDT',lambda a:a.insert(1,deepcopy(a[0])))
            with self.assertRaises(m.AuditEvidenceError):
                m.audit_symbol(Path(root)/'BTCUSDT_order_state_audit.json',Path(root)/'BTCUSDT_order_state_events.csv','BTCUSDT')

    def test_missing_terminal_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            fixture(root)
            edit_events(root,'BTCUSDT',lambda a:a.pop())
            with self.assertRaises(m.AuditEvidenceError):
                m.audit_symbol(Path(root)/'BTCUSDT_order_state_audit.json',Path(root)/'BTCUSDT_order_state_events.csv','BTCUSDT')

    def test_unobserved_actual_fill_cannot_be_claimed(self):
        with tempfile.TemporaryDirectory() as root:
            fixture(root)
            edit_events(root,'BTCUSDT',lambda a:a[1].__setitem__('actual_fill_observed','true'))
            with self.assertRaises(m.AuditEvidenceError):
                m.audit_symbol(Path(root)/'BTCUSDT_order_state_audit.json',Path(root)/'BTCUSDT_order_state_events.csv','BTCUSDT')

    def test_intrabar_sequence_cannot_be_claimed(self):
        with tempfile.TemporaryDirectory() as root:
            fixture(root,kind=m.SAME_HOUR,order=m.ORDERINGS[0])
            edit_events(root,'BTCUSDT',lambda a:a[1].__setitem__('intra_hour_fill_observed','true'))
            with self.assertRaises(m.AuditEvidenceError):
                m.audit_symbol(Path(root)/'BTCUSDT_order_state_audit.json',Path(root)/'BTCUSDT_order_state_events.csv','BTCUSDT')

    def test_taxonomy_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            fixture(root)
            edit_report(root,'BTCUSDT',lambda a:a['splits']['test'].__setitem__('modeled_fills',5))
            with self.assertRaises(m.AuditEvidenceError):
                m.audit_symbol(Path(root)/'BTCUSDT_order_state_audit.json',Path(root)/'BTCUSDT_order_state_events.csv','BTCUSDT')

    def test_requires_utc_timestamps_and_hourly_alignment(self):
        with tempfile.TemporaryDirectory() as root:
            fixture(root)
            edit_events(root,'BTCUSDT',lambda a:a[1].__setitem__('at_1h_open_utc','2026-01-01T00:00:05Z'))
            with self.assertRaises(m.AuditEvidenceError):
                m.audit_symbol(Path(root)/'BTCUSDT_order_state_audit.json',Path(root)/'BTCUSDT_order_state_events.csv','BTCUSDT')

    def test_all_three_symbols_required_for_complete_gate(self):
        with tempfile.TemporaryDirectory() as root:
            for s in m.SYMBOLS: fixture(root,s)
            r=m.build_report(Path(root),Path(root)/'out')
            self.assertEqual(r['split_totals']['test']['modeled_fills'],3)
            self.assertEqual(r['status'],'verified_1h_only_observational')
            self.assertFalse(r['subhour_used'])
            self.assertTrue((Path(root)/'out/order_state_evidence_gate.md').is_file())
            with self.assertRaises(m.AuditEvidenceError):
                m.build_report(Path(root),Path(root)/'fail',symbols=('BTCUSDT','ETHUSDT'))

    def test_no_subhour_data_dependencies(self):
        self.assertEqual(m.TIMEFRAMES,('1h','4h','1d'))
        self.assertNotIn('1m',m.TIMEFRAMES)
        self.assertNotIn('15m',m.TIMEFRAMES)

if __name__=='__main__': unittest.main()