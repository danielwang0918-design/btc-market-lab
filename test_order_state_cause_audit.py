"""Isolated non-trading tests for Test-period pending cancellation reason research."""
import unittest
from datetime import datetime,timezone
from unittest.mock import Mock
from pathlib import Path
import tempfile,json,csv
import order_state_cause_audit as a

BASE={'setup_id':'original-ob','side':'long'}


def event(ev,setup='ob-a',hour=0,split='test'):
    return {'split':split,'event':ev,'setup_id':setup,'at_1h_open_utc':datetime.fromtimestamp(hour*3600,timezone.utc).isoformat()}

class CausalCancelTests(unittest.TestCase):
    def test_timeframes_only(self):
        self.assertEqual(a.INTERVALS,('1d','4h','1h'))
        self.assertNotIn('1m',a.INTERVALS)
        self.assertNotIn('15m',a.INTERVALS)

    def test_extract_original_first_failed_condition(self):
        names=[
            ('日線、4H、1H 最近已確認的結構突破方向不一致。','mtf_bos_direction_mismatch'),
            ('扣除設定費率與滑價後的風險報酬比未達個人最低門檻。','net_rr_below_required'),
            ('同向 1H OB 不存在、價位無效，或已被其後已收盤 K 線回測。','no_fresh_ob_or_retested'),
            ('現價已到達或越過規則設定的目標波段，原候選情境失效。','price_at_or_beyond_target_at_close'),
            ('OB 確認後沒有位於預期方向、且超出進場區的已確認 1H 波段目標。','no_eligible_1h_swing_target')]
        for input_name,expected in names:
            with self.subTest(input_name=input_name):
                self.assertEqual(a._classify({'reasons':[input_name]},BASE,None),expected)

    def test_replacement_new_ob_or_side(self):
        self.assertEqual(a._classify({},BASE,{'setup_id':'new-ob','side':'long'}),'replacement_same_side_new_ob')
        self.assertEqual(a._classify({},BASE,{'setup_id':'new-ob','side':'short'}),'replacement_opposite_side_ob')
        with self.assertRaisesRegex(ValueError,'still matches'):
            a._classify({},BASE,{'setup_id':'original-ob','side':'long'})

    def test_unknown_reason_must_be_explicit(self):
        self.assertEqual(a._classify({'reasons':['new not-understood reason']},BASE,None),'unclassified_original_reason')
        with self.assertRaises(ValueError):a._classify({'reasons':[]},BASE,None)

    def test_pair_wait_duration(self):
        events=[event(a.ACCEPT,hour=4),event(a.CANCEL,hour=17)]
        pairs=a.collect_cancel_pairs(events)
        self.assertEqual(len(pairs),1)
        self.assertEqual(pairs[0]['pending_hours'],13)

    def test_pair_unrelated_split_is_ignored(self):
        events=[event(a.ACCEPT,split='train',hour=1),event(a.CANCEL,split='train',hour=2),event(a.ACCEPT,hour=4),event('pending_target_without_zone_cancel',hour=5)]
        self.assertEqual(a.collect_cancel_pairs(events),[])

    def test_duplicate_accept_fails(self):
        events=[event(a.ACCEPT,hour=1),event(a.ACCEPT,hour=2),event(a.CANCEL,hour=3)]
        with self.assertRaisesRegex(ValueError,'Duplicate'):a.collect_cancel_pairs(events)

    def test_orphan_terminal_fails(self):
        with self.assertRaisesRegex(ValueError,'orphan'):a.collect_cancel_pairs([event(a.CANCEL,hour=5)])

    def test_missing_terminal_fails(self):
        with self.assertRaisesRegex(ValueError,'Missing'):a.collect_cancel_pairs([event(a.ACCEPT,hour=1)])

    def test_duplicate_terminal_fails(self):
        with self.assertRaisesRegex(ValueError,'Duplicate terminal'):a.collect_cancel_pairs([event(a.ACCEPT,hour=1),event(a.CANCEL,hour=2),event('pending_target_without_zone_cancel',hour=3)])

    def test_no_backwards_or_same_hour(self):
        for h in (2,3):
            with self.subTest(h=h):
                with self.assertRaisesRegex(ValueError,'later 1H'):a.collect_cancel_pairs([event(a.ACCEPT,hour=3),event(a.CANCEL,hour=h)])

    def test_frozen_bad_sources_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'multi_asset_coverage.json').write_text('{}')
            r=root/'replay';r.mkdir()
            (r/'BTCUSDT_order_state_audit.json').write_text(json.dumps({'symbol':'BTCUSDT','version':'order-state-1h-ob-observational-v1','decision_timeframes_only':['1h','4h','1d'],'subhour_price_data_used':False,'coverage_source_sha256':'wrong'}))
            (r/'BTCUSDT_order_state_events.csv').write_text('symbol,split,event,setup_id,at_1h_open_utc\n')
            with self.assertRaisesRegex(ValueError,'coverage SHA'):
                a._read_inputs(root,r,'BTCUSDT')

    def test_candle_hash_mismatch_rejected_after_valid_coverage_hash(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); replay=root/'replay'; replay.mkdir()
            coverage={'symbols':{'BTCUSDT':{'intervals':{}}}}
            for iv in a.INTERVALS:
                path=root/f'BTCUSDT_{iv}.csv';path.write_text('original')
                coverage['symbols']['BTCUSDT']['intervals'][iv]={'csv_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
            cp=root/'multi_asset_coverage.json';cp.write_text(json.dumps(coverage))
            audit={'symbol':'BTCUSDT','version':'order-state-1h-ob-observational-v1',
                   'subhour_price_data_used':False,'decision_timeframes_only':['1h','4h','1d'],
                   'coverage_source_sha256':hashlib.sha256(cp.read_bytes()).hexdigest(),
                   'splits':{'test':{'event_counts':{a.CANCEL:0}}}}
            (replay/'BTCUSDT_order_state_audit.json').write_text(json.dumps(audit))
            (replay/'BTCUSDT_order_state_events.csv').write_text('symbol,split,event,setup_id,at_1h_open_utc\n')
            (root/'BTCUSDT_1h.csv').write_text('tampered')
            with self.assertRaisesRegex(ValueError,'frozen candle SHA256'):
                a._read_inputs(root,replay,'BTCUSDT')

    def test_aggregate_refuses_missing_or_inconsistent(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            with self.assertRaises(FileNotFoundError): a.aggregate(root)
            for sym in a.SYMBOLS:
                body={'version':a.VERSION,'symbol':sym,'status':'verified_reconstructed_test_cancellation_snapshots','uses_subhour_data':False,
                      'test_canceled_signals':3,'reason_counts':{'net_rr_below_required':2,'no_eligible_1h_swing_target':1}}
                (root/f'{sym}_cancellation_causes.json').write_text(json.dumps(body))
            out=a.aggregate(root)
            self.assertEqual(out['test_canceled_signals'],9)
            self.assertEqual(out['reason_counts']['net_rr_below_required'],6)
            self.assertTrue((root/'cancel_cause_summary.md').exists())

    def test_non_hour_timestamps_fail(self):
        self.assertEqual(a._utc_millis('2025-01-01T01:00:00+00:00') % a.HOUR,0)
        with self.assertRaises(ValueError):a._utc_millis('2025-01-01T01:00:00')

if __name__=='__main__':unittest.main()