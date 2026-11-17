"""SYNTHETIC anomaly fixtures; never substitute these for live acceptance."""
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import p1_08d_receiver as mod
from p1_08c_registry import FRAMES,sha_file,timestamp,utc

H=FRAMES['1h'];T=1_893_456_000_000


def row(op,frame='1h',close='101',volume='10'):
    return [op,'100','102','99',close,volume,op+FRAMES[frame]-1,'1000',10,'5','500','0']


class Response:
    status=200;headers={}
    def __init__(self,value):self.raw=json.dumps(value).encode()
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def read(self,n):return self.raw[:n]


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)/'receiver';self.now=T+2*H+10000;self.offset=0;self.rows=[row(T),row(T+H),row(T+2*H)]
        self.failure=None;self.calls=[]
        self.r=mod.Receiver.create(self.root,mode='synthetic',symbols=['BTCUSDT'],frames=['1h'],
            clock=lambda:self.now,opener=self.open_it)

    def open_it(self,req,timeout):
        from urllib.parse import parse_qs,urlparse
        self.calls.append(req.full_url);path=urlparse(req.full_url).path
        if path.endswith('/time'):return Response({'serverTime':self.now+self.offset})
        if self.failure=='network':raise OSError('synthetic connection loss')
        if self.failure=='429':raise HTTPError(req.full_url,429,'synthetic rate limit',{},io.BytesIO(b'{"code":-1003}'))
        if path.endswith('/klines'):return Response(self.rows)
        if path.endswith('/bookTicker'):return Response({'symbol':'BTCUSDT','bidPrice':'100','askPrice':'101','bidQty':'1','askQty':'2'})
        return Response({'lastUpdateId':123,'bids':[['100','1']],'asks':[['101','2']]})

    def collect(self,liquidity=False):return self.r.collect(include_liquidity=liquidity)

    def test_real_mode_rejects_injected_transport_or_clock(self):
        with self.assertRaises(ValueError):mod.Receiver.create(self.root.parent/'real',mode='real_public',clock=lambda:self.now,opener=self.open_it)

    def test_closed_evidence_and_raw_receipt_link_to_original_registry(self):
        result=self.collect();self.assertEqual(result['new_evidence'],2);self.assertEqual(result['open_rows'],1)
        v=self.r.verify();self.assertEqual(v['normalized_evidence'],2);self.assertEqual(v['candidates'],0)
        self.assertFalse(v['external_timestamp_attestation_verified'])
        records=self.r.registry._records()
        for r in records[1:]:
            self.assertEqual(r['kind'],'evidence');self.assertIsNone(r['payload']['source']['published_at'])
            self.assertLessEqual(timestamp(r['payload']['close_at']),self.now-1000)

    def test_receipt_time_is_now_not_historical_candle_close(self):
        self.collect()
        values=self.r.processed_rows()
        accepted=[x for x in values if x['status']=='new']
        self.assertTrue(all(x['wire_received_at']==utc(self.now) for x in accepted))
        self.assertTrue(all(x['availability_class']=='bootstrap_backfill' for x in accepted))
        self.assertTrue(all(x['source_published_at'] is None for x in accepted))

    def test_unclosed_rows_never_project_as_known_features(self):
        self.collect();features=self.r.projections()
        self.assertEqual(features['BTCUSDT/1h']['close'],101.0)
        self.assertEqual(features['BTCUSDT/1h']['close_at'],utc(T+2*H))
        self.assertFalse(features['BTCUSDT/1h']['candidate_registration_enabled'])

    def test_exact_close_safety_boundary_is_conservative(self):
        self.now=T+2*H+500;self.collect()
        self.assertEqual(self.r.verify()['normalized_evidence'],1)

    def test_restart_and_duplicates_do_not_create_extra_evidence(self):
        self.collect();cp=self.r.checkpoint();self.now+=1000
        resumed=mod.Receiver(self.root,clock=lambda:self.now,opener=self.open_it)
        self.assertTrue(resumed.verify(anchor=cp)['anchor_consistent'])
        result=resumed.collect(include_liquidity=False)
        self.assertEqual(result['new_evidence'],0);self.assertEqual(result['duplicate_rows'],2)
        self.assertEqual(resumed.verify()['normalized_evidence'],2)

    def test_revision_is_kept_without_overwriting_original(self):
        self.collect();original=self.r.registry._records()[1:];self.rows[0][4]='100.5';self.now+=1000
        result=self.collect();self.assertEqual(result['revision_rows'],1)
        self.assertEqual(self.r.registry._records()[1:],original)
        self.assertIn('source_revision',self.r.projections()['BTCUSDT/1h']['quality_flags'])

    def test_non_ohlcv_revision_is_also_detected(self):
        self.collect();self.rows[0][8]=11;self.now+=1000
        self.assertEqual(self.collect()['revision_rows'],1)

    def test_network_outage_is_recorded_not_empty_success(self):
        self.failure='network';result=self.collect()
        self.assertGreater(result['failed_requests'],0);self.assertEqual(result['new_evidence'],0)
        self.assertEqual(result['status'],'partial')
        self.assertTrue(self.r.verify()['raw_receipts']>=2)

    def test_rate_limit_stops_request_sequence_without_retry(self):
        self.failure='429';self.collect(liquidity=True)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.r.verify()['normalized_evidence'],0)

    def test_gap_is_not_filled_or_called_contiguous(self):
        self.collect();self.now=T+6*H+10000;self.rows=[row(T+4*H),row(T+5*H),row(T+6*H)]
        result=self.collect();self.assertGreater(result['gap_intervals'],0)
        p=self.r.projections()['BTCUSDT/1h'];self.assertIn('missing_candles',p['quality_flags'])
        self.assertFalse(p['data_quality_ready'])

    def test_zero_volume_and_invalid_ohlc_are_explicit(self):
        self.rows=[row(T,volume='0'),row(T+H,close='1000')]
        r=self.collect();self.assertEqual(r['zero_volume_rows'],1);self.assertEqual(r['invalid_rows'],1)
        self.assertFalse(self.r.projections()['BTCUSDT/1h']['data_quality_ready'])

    def test_server_clock_incompatible_rejects_closed_claim(self):
        self.offset=100000;result=self.collect()
        self.assertEqual(result['new_evidence'],0)
        self.assertIn('server_clock_unverified',result['quality_flags'])

    def test_wall_clock_rollback_persists_diagnostic_and_no_evidence(self):
        self.collect();self.now-=1000;before=self.r.verify()['normalized_evidence']
        r=self.collect();self.assertIn('clock_rollback',r['quality_flags'])
        self.assertEqual(self.r.verify()['normalized_evidence'],before)

    def test_raw_tampering_detected_before_any_more_requests(self):
        self.collect();file=next((self.root/'batches').glob('*/*.body'));file.chmod(0o644);file.write_bytes(b'fake')
        n=len(self.calls)
        with self.assertRaises(ValueError):self.collect()
        self.assertEqual(len(self.calls),n)

    def test_unknown_or_pending_files_stop_restart(self):
        self.collect();(self.root/'.pending-crash').mkdir()
        with self.assertRaises(ValueError):mod.Receiver(self.root,clock=lambda:self.now,opener=self.open_it).verify()

    def test_registry_advance_without_batch_is_not_silently_accepted(self):
        self.collect();self.r.registry.ingest(dict(symbol='BTCUSDT',timeframe='1h',open_at=utc(T-H),close_at=utc(T),
            ohlcv=dict(open='100',high='102',low='99',close='101',volume='10',quote_volume='1000'),
            source=dict(id='extra-unbound',uri='synthetic://unbound',market_event_at=utc(T-H),published_at=None,unknown_reason='fixture')))
        with self.assertRaises(ValueError):self.r.verify()

    def test_checkpoint_detects_deleted_batch_suffix(self):
        import shutil
        self.collect();self.now+=1000;self.collect();cp=self.r.checkpoint()
        shutil.rmtree(sorted((self.root/'batches').iterdir())[-1])
        with self.assertRaises(ValueError):self.r.verify(anchor=cp)

    def test_snapshot_restore_into_new_directory_and_resume(self):
        self.collect();cp=self.r.checkpoint();archive=self.root.parent/'external-snapshot.zip';self.r.snapshot(archive)
        dest=self.root.parent/'restored'
        restored=mod.Receiver.restore(archive,dest,expected_sha256=sha_file(archive),anchor=cp,clock=lambda:self.now,opener=self.open_it)
        self.now+=1000;self.assertEqual(restored.collect(include_liquidity=False)['duplicate_rows'],2)
        self.assertEqual(restored.verify()['normalized_evidence'],2)

    def test_restore_refuses_wrong_digest_or_overwrite(self):
        self.collect();cp=self.r.checkpoint();archive=self.root.parent/'external.zip';self.r.snapshot(archive)
        for target,digest in [(self.root.parent/'wrong','b'*64),(self.root,sha_file(archive))]:
            with self.subTest(target=target),self.assertRaises(ValueError):mod.Receiver.restore(archive,target,expected_sha256=digest,anchor=cp,clock=lambda:self.now,opener=self.open_it)

    def test_small_liquidity_snapshots_are_not_realized_slippage(self):
        result=self.collect(liquidity=True);self.assertEqual(result['liquidity_snapshots'],2)
        self.assertEqual(result['new_evidence'],2)
        self.assertFalse(result['actual_execution_verified'])

    def test_symbol_frame_and_endpoint_allowlists(self):
        for symbols,frames in [(['BADUSDT'],['1h']),(['BTCUSDT'],['5m'])]:
            with self.subTest(symbols=symbols,frames=frames),self.assertRaises(ValueError):mod.Receiver.create(self.root.parent/'bad',mode='synthetic',symbols=symbols,frames=frames,clock=lambda:self.now,opener=self.open_it)

    def test_real_and_synthetic_cannot_mix_on_reopen(self):
        self.collect()
        with self.assertRaises(ValueError):mod.Receiver(self.root,expected_mode='real_public')

    def test_no_candidate_or_order_path_is_activated(self):
        self.collect()
        self.assertEqual(self.r.verify()['candidates'],0)
        self.assertTrue(all('/order' not in u and '/account' not in u for u in self.calls))


if __name__=='__main__':unittest.main()
