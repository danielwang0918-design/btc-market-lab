"""Network-free tests of hard preregistration and source-prefix/chain gates."""
from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import prospective_validation as m
import prospective_candle_append as cap

HOUR=m.MS_PER['1h']

def candle(i, *, interval='1h', symbol='BTCUSDT', close='105', source=m.SOURCE):
    step=m.MS_PER[interval]
    op=i*step
    return {'symbol':symbol,'interval':interval,'open_time':str(op),'open_time_utc':m.utc(op),
            'open':'100','high':'110','low':'90','close':close,'volume':'10',
            'close_time':str(op+step-1),'close_time_utc':m.utc(op+step-1),
            'exchange_close_time':str(op+step-1),'exchange_close_time_utc':m.utc(op+step-1),
            'is_closed':'true','source':source}

FIELDS=['symbol','interval','open_time','open_time_utc','open','high','low','close','volume','close_time','close_time_utc','exchange_close_time','exchange_close_time_utc','is_closed','source']


def write_data(folder):
    path=folder/'BTCUSDT_1h.csv'
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerow(candle(0))
    original=path.read_bytes()
    meta={'bytes':len(original),'sha256':m.sha_bytes(original),'frozen_bars':1,
          'frozen_last_close_exclusive_ms':HOUR,'symbol':'BTCUSDT','interval':'1h'}
    return path,{'frozen_data_prefixes':{path.name:meta}},meta


def append(path,bar):
    with path.open('a',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS);w.writerow(bar)


class ForwardTests(unittest.TestCase):
    def test_real_protocol_registration_future_and_1h_only(self):
        path=Path(__file__).with_name('PROSPECTIVE_LOCK_V1.json')
        data=json.loads(path.read_text())
        self.assertEqual(m.sha_file(path),m.LOCK_SHA256)
        self.assertEqual(data['forward_start_utc'],m.START)
        self.assertEqual(data['forward_end_exclusive_utc'],m.END)
        self.assertEqual(data['formal_engine']['allowed_decision_timeframes'],list(m.ALLOWED_FRAMES))
        self.assertEqual(len(data['symbols']),8)
        self.assertNotIn('1m',data['formal_engine']['allowed_decision_timeframes'])
        self.assertFalse(data['formal_engine']['real_order_placement'])

    def test_lock_drift_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'lock.json';p.write_text('{}')
            with self.assertRaisesRegex(ValueError,'HASH DRIFT'):
                m.lock_check(p,Path(td))

    def test_locked_code_sha_rejects_changes(self):
        with tempfile.TemporaryDirectory() as td:
            repo=Path(td)
            (repo/'f.py').write_text('v1')
            lock=json.loads(Path(__file__).with_name('PROSPECTIVE_LOCK_V1.json').read_text())
            lock['original_source_git_blob_sha1']={'f.py':m.git_blob_sha(repo/'f.py')}
            lock['symbols']=['BTCUSDT','ETHUSDT','SOLUSDT','ZECUSDT','XRPUSDT','BNBUSDT','NEARUSDT','DOGEUSDT']
            # File-level hash replacement is only allowed in isolated offline test fixtures.
            lockpath=repo/'fake.json';lockpath.write_text(json.dumps(lock))
            with patch.object(m,'LOCK_SHA256',m.sha_file(lockpath)):
                self.assertEqual(m.lock_check(lockpath,repo)['symbols'],lock['symbols'])
                (repo/'f.py').write_text('v2')
                with self.assertRaisesRegex(ValueError,'CODE DRIFT'):
                    m.lock_check(lockpath,repo)

    def test_identical_frozen_prefix_and_no_future(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);path,lock,meta=write_data(root)
            files=m.validate_sources(lock,root,asof_ms=None)
            self.assertEqual(files[path.name]['appended_closed_candles'],0)
            self.assertEqual(files[path.name]['full_bytes'],meta['bytes'])
            prev=m.make_record(lock,files,HOUR)
            append(path,candle(1))
            later=m.validate_sources(lock,root,asof_ms=2*HOUR,previous=prev,require_current=True)
            self.assertEqual(later[path.name]['appended_closed_candles'],1)
            self.assertEqual(later[path.name]['last_closed_end_exclusive_utc'],m.utc(2*HOUR))

    def test_modified_frozen_prefix_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);path,lock,_=write_data(root)
            raw=path.read_bytes();path.write_bytes(raw.replace(b'105',b'104',1))
            with self.assertRaisesRegex(ValueError,'prefix CHANGED'):
                m.validate_sources(lock,root,asof_ms=None)

    def test_missing_or_unclosed_future_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);path,lock,_=write_data(root)
            append(path,candle(2))
            with self.assertRaisesRegex(ValueError,'Missing, duplicated'):
                m.validate_sources(lock,root,asof_ms=3*HOUR)
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);path,lock,_=write_data(root)
            row=candle(1);row['is_closed']='false';append(path,row)
            with self.assertRaisesRegex(ValueError,'open candle'):
                m.validate_sources(lock,root,asof_ms=2*HOUR)

    def test_after_asof_is_not_a_closed_candle(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);path,lock,_=write_data(root)
            append(path,candle(1))
            with self.assertRaisesRegex(ValueError,'Future or currently unclosed'):
                m.validate_sources(lock,root,asof_ms=HOUR)

    def test_forward_tail_rewrite_rejected_by_previous_record(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);path,lock,_=write_data(root)
            append(path,candle(1))
            first=m.validate_sources(lock,root,asof_ms=2*HOUR)
            prev=m.make_record(lock,first,2*HOUR)
            original=path.read_bytes();path.write_bytes(original.replace(b'105',b'102',1))
            # The naive replace also changes frozen prefix; prefix hash must fail first.
            with self.assertRaises(ValueError):m.validate_sources(lock,root,asof_ms=3*HOUR,previous=prev)
            path.write_bytes(original)
            pos=lock['frozen_data_prefixes'][path.name]['bytes']
            b=path.read_bytes();path.write_bytes(b[:pos]+b[pos:].replace(b'105',b'102',1))
            with self.assertRaisesRegex(ValueError,'REWRITTEN'):
                m.validate_sources(lock,root,asof_ms=3*HOUR,previous=prev)

    def test_stale_missing_closed_hour_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);path,lock,_=write_data(root)
            with self.assertRaisesRegex(ValueError,'Stale or missing'):
                m.validate_sources(lock,root,asof_ms=2*HOUR,require_current=True)

    def test_previous_record_digest_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);_,lock,_=write_data(root)
            files=m.validate_sources(lock,root,asof_ms=None)
            rec=m.make_record(lock,files,HOUR)
            m.verify_record(rec,lock)
            rec['asof_utc']=m.utc(HOUR*4)
            with self.assertRaisesRegex(ValueError,'Invalid prior'):
                m.verify_record(rec,lock)

    def test_checkpoint_must_advance_without_backdating(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);_,lock,_=write_data(root)
            files=m.validate_sources(lock,root,asof_ms=None)
            prior=m.make_record(lock,files,HOUR)
            with self.assertRaisesRegex(ValueError,'backdate'):
                m.make_record(lock,files,HOUR,previous=prior)
            rec=m.make_record(lock,files,2*HOUR,previous=prior)
            self.assertEqual(rec['previous_record_sha256'],prior['record_sha256'])

    def test_metric_no_independent_sample_claim(self):
        z={'BTCUSDT':[],'ETHUSDT':[]}
        d=m.summary_trades(z)
        self.assertEqual(d['observational_completed_trades'],0)
        self.assertIsNone(d['pooled_expectancy_r_descriptive_only'])
        self.assertFalse(d['positive_edge_claim'])
        one={'entry_time_utc':'2026-12-01T00:00:00+00:00','r_multiple':'-1'}
        d=m.summary_trades({'BTCUSDT':[one],'ETHUSDT':[one]})
        self.assertEqual(d['entry_utc_iso_weeks'],1)
        self.assertEqual(d['pooled_expectancy_r_descriptive_only'],-1)
        self.assertFalse(d['readout_gate_200_trades_26_weeks_met'])

    def test_append_collects_all_before_file_write_on_error(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);path,lock,meta=write_data(root)
            extra='ETHUSDT_1h.csv';lock['frozen_data_prefixes'][extra]={**meta,'symbol':'ETHUSDT'}
            # Missing ETH file, so even first symbol must not have been touched.
            old=path.read_bytes()
            with self.assertRaisesRegex(ValueError,'Missing prospective'):
                cap.capture(lock,root,m.parse_utc(m.START),fetcher=lambda *a,**k:[])
            self.assertEqual(path.read_bytes(),old)

    def test_capture_detects_missing_binance_bars_before_append(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);path,lock,meta=write_data(root)
            asof=3*HOUR
            with patch.object(m,'START',m.utc(HOUR)):
                with patch.object(m,'END',m.utc(HOUR*100)):
                    with self.assertRaisesRegex(ValueError,'missing/duplicated'):
                        cap.capture(lock,root,asof,fetcher=lambda *a,**k:[candle(1)])
            self.assertEqual(path.stat().st_size,meta['bytes'])

    def test_capture_dry_run_no_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);path,lock,meta=write_data(root)
            def fake_fetch(s,tf,start,end,client=None):
                return [candle(i) for i in range(start//HOUR,end//HOUR)]
            with patch.object(m,'START',m.utc(HOUR)):
                with patch.object(m,'END',m.utc(HOUR*100)):
                    counts=cap.capture(lock,root,HOUR*3,fetcher=fake_fetch,dry_run=True)
            self.assertEqual(counts[path.name],2)
            self.assertEqual(path.stat().st_size,meta['bytes'])

    def test_snapshot_model_drops_forced_interim_end(self):
        class Fake:
            INTERVALS=m.ALLOWED_FRAMES
            def _prepare_snapshots(self, data,symbol,**kw):
                ts=m.parse_utc(m.START)
                return ([{'open_time':ts,'close_time':ts+HOUR-1}], [{}], {})
            def _simulate_slice(self,rows,snaps,start,end,**kw):
                ts=m.parse_utc(m.START)
                return [{'entry_time_utc':m.utc(ts),'signal_time_utc':m.utc(ts),
                         'exit_time_utc':m.utc(ts+HOUR), 'exit_reason':'split_or_segment_end',
                         'r_multiple':'-1','setup_id':'demo'}]
        lock={'symbols':['BTCUSDT']}
        at=m.parse_utc(m.START)+HOUR
        x=m.simulate_forward(lock,Path('.'),at,engine=Fake())
        self.assertEqual(x['metrics']['observational_completed_trades'],0)
        self.assertEqual(x['status'],'forward_observations_only')


if __name__=='__main__':
    unittest.main()