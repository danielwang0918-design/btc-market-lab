"""Synthetic causal/integrity contracts; no observed forward signals or orders."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

import p1_08c_registry as registry

H = 3_600_000
T = int(datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
DIGEST = 'a' * 64


def protocol():
    return dict(study_key='SYNTHETIC_ONLY', hypothesis=dict(id='fixture',
        statement='Infrastructure fixture, no trading hypothesis', falsification='Any causal/integrity violation'),
        rule=dict(id='fixture', version='test-v1', code_sha256=DIGEST),
        study_start_at=registry.utc(T), review_dates=[registry.utc(T+366*24*H), registry.utc(T+731*24*H)],
        arms=['all'], features={'last_close': dict(definition='last closed price; fixture only',
            code_sha256=DIGEST, dtype='number', lookback_bars={'1h': 1})},
        label_names=['mfe_R0', 'mae_R0'], label_calculator_sha256=DIGEST,
        horizons_hours=[6, 12, 24, 48, 72], observation_anchor='next_1h_boundary_after_registration',
        costs=dict(fee_bps_per_side=10, slippage_bps_per_side=[5, 15], paper_initial_USDT=1000, risk_fraction=0.02))


def candle(open_ms=T, *, frame='1h', symbol='BTCUSDT'):
    return dict(symbol=symbol, timeframe=frame, open_at=registry.utc(open_ms),
        close_at=registry.utc(open_ms+registry.FRAMES[frame]),
        ohlcv=dict(open='100', high='102', low='99', close='101', volume='10', quote_volume=None),
        source=dict(id='SYNTHETIC_FIXTURE', uri='synthetic://closed-candle',
                    market_event_at=None, published_at=None, unknown_reason='source time not supplied'))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)/'journal'; self.t=T-1
        self.r=registry.Registry.create(self.root, protocol(), mode='synthetic', clock=lambda:self.t)
        self.t=T+H+1000
        self.e=self.r.ingest(candle())

    def candidate(self, **changes):
        c=dict(market_event_key='fixture-1', symbol='BTCUSDT', timeframe='1h',
            formed_at=registry.utc(T+H), confirmed_at=registry.utc(T+H), state='confirmed', stance='long',
            arm='all', observation_start_at=registry.utc(T+2*H), evidence_ids=[self.e['record_sha256']],
            features={'last_close': dict(value=101, known_at=registry.utc(self.t),
                evidence_ids=[self.e['record_sha256']], unknown_reason=None)},
            quality=dict(data_gaps=[], delays=[], unknown_fields=[]), previous_candidate_sha256=None)
        c.update(changes); return c

    def register(self): return self.r.register(self.candidate())

    def future_evidence(self, *, skip=None, zero=None):
        ids=[]
        for i in range(6):
            if i==skip: continue
            self.t=T+(3+i)*H+1; c=candle(T+(2+i)*H)
            if i==zero: c['ohlcv']['volume']='0'
            ids.append(self.r.ingest(c)['record_sha256'])
        return ids

    def label(self, candidate, ids, **changes):
        p=dict(candidate_sha256=candidate['record_sha256'], horizon_hours=6,
            values={'mfe_R0':0.5, 'mae_R0':0.25}, status='complete', evidence_ids=ids,
            intrabar_order='unknown', limitations=[])
        p.update(changes); return p

    def test_empty_real_hypothesis_cannot_activate(self):
        p=protocol();p['hypothesis']={}
        with self.assertRaises(ValueError):registry.Registry.create(self.root/'new',p,mode='prospective')

    def test_injected_clock_forbidden_outside_synthetic(self):
        with self.assertRaises(ValueError):registry.Registry.create(self.root/'new',protocol(),mode='prospective',clock=lambda:T)

    def test_protocol_is_immutable_and_reinitialization_refused(self):
        with self.assertRaises(ValueError):registry.Registry.create(self.root,protocol(),mode='synthetic',clock=lambda:T)

    def test_actual_writer_time_not_user_supplied(self):
        c=self.candidate();c['registered_at']=registry.utc(T)
        with self.assertRaises(ValueError):self.r.register(c)

    def test_protocol_must_precede_real_study(self):
        p=protocol();p['study_start_at']=registry.utc(T-10*H)
        with patch.object(registry,'system_ms',return_value=T):
            with self.assertRaises(ValueError):registry.Registry.create(self.root/'new',p,mode='prospective')

    def test_review_calendar_and_costs_frozen(self):
        for key,value in [('review_dates',[registry.utc(T+H)]),('costs',{})]:
            with self.subTest(key=key):
                p=protocol();p[key]=value
                with self.assertRaises(ValueError):registry.Registry.create(self.root/key,p,mode='synthetic',clock=lambda:T-1)

    def test_unknown_publication_not_invented_as_close(self):
        self.assertIsNone(self.e['payload']['source']['published_at'])
        c=self.register()['payload'];self.assertIn('source_publication_time_unknown',c['quality']['unknown_fields'])
        self.assertEqual(c['data_available_at'],registry.utc(self.t))

    def test_source_unknown_publication_needs_reason(self):
        c=candle(T-H);c['source']['unknown_reason']=''
        with self.assertRaises(ValueError):self.r.ingest(c)

    def test_future_publication_and_market_time_rejected(self):
        for name in ('published_at','market_event_at'):
            c=candle(T-H);c['source'][name]=registry.utc(self.t+1)
            with self.subTest(name=name), self.assertRaises(ValueError):self.r.ingest(c)

    def test_cannot_supply_received_at(self):
        c=candle(T-H);c['received_at']=registry.utc(T-H)
        with self.assertRaises(ValueError):self.r.ingest(c)

    def test_unclosed_candle_rejected(self):
        with self.assertRaises(ValueError):self.r.ingest(candle(T+H))

    def test_subhour_frame_rejected(self):
        c=candle(T-H);c['timeframe']='15m'
        with self.assertRaises(ValueError):self.r.ingest(c)

    def test_symbol_frame_alignment_and_geometry(self):
        changes=[dict(symbol='BADUSDT'),dict(open_at=registry.utc(T-H+1)),
                 dict(close_at=registry.utc(T-1))]
        for x in changes:
            c=candle(T-H);c.update(x)
            with self.subTest(x=x),self.assertRaises(ValueError):self.r.ingest(c)
        for field,value in [('low','103'),('volume','-1'),('high','NaN'),('close','Infinity')]:
            c=candle(T-H);c['ohlcv'][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):self.r.ingest(c)

    def test_zero_volume_is_explicit(self):
        c=candle(T-H);c['ohlcv']['volume']='0';e=self.r.ingest(c)
        self.assertIn('zero_volume',e['payload']['quality_flags'])

    def test_duplicate_evidence_rejected(self):
        with self.assertRaises(ValueError):self.r.ingest(candle())

    def test_formation_must_use_registered_closed_frame(self):
        for changes in [dict(formed_at=registry.utc(T-1)),dict(formed_at=registry.utc(T+H-1)),
                        dict(confirmed_at=registry.utc(T+2*H)),dict(evidence_ids=[])]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.r.register(self.candidate(**changes))

    def test_observation_strictly_after_registration_and_fixed_anchor(self):
        for ms in [self.t-1,self.t,self.t+1,T+3*H]:
            with self.subTest(ms=ms),self.assertRaises(ValueError):self.r.register(self.candidate(observation_start_at=registry.utc(ms)))

    def test_pending_stays_watch_and_does_not_get_confirmation(self):
        for changes in [dict(state='pending',stance='long',confirmed_at=None),
                        dict(state='pending',stance='watch'),dict(state='confirmed',confirmed_at=None)]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.r.register(self.candidate(**changes))

    def test_append_confirmation_preserves_original_pending_record(self):
        pending=self.r.register(self.candidate(state='pending',stance='watch',confirmed_at=None))
        original=json.dumps(pending,sort_keys=True)
        self.t=T+2*H+1000;e2=self.r.ingest(candle(T+H))
        c=self.candidate(confirmed_at=registry.utc(T+2*H),observation_start_at=registry.utc(T+3*H),
            previous_candidate_sha256=pending['record_sha256'],evidence_ids=[self.e['record_sha256'],e2['record_sha256']])
        confirmed=self.r.register(c)
        self.assertEqual(confirmed['payload']['event_id'],pending['payload']['event_id'])
        self.assertEqual(confirmed['payload']['revision'],2)
        self.assertEqual(json.dumps(self.r.get(pending['record_sha256']),sort_keys=True),original)
        self.assertIsNone(pending['payload']['confirmed_at'])

    def test_duplicate_candidate_and_stale_revision_rejected(self):
        c=self.register()
        with self.assertRaises(ValueError):self.register()
        with self.assertRaises(ValueError):self.r.register(self.candidate(previous_candidate_sha256='b'*64))
        self.assertEqual(c['payload']['revision'],1)

    def test_feature_future_unknown_and_result_injection_rejected(self):
        for name,value in [('mfe_R0',1),('final_pnl',1),('source_final_status','win')]:
            c=self.candidate();c['features'][name]={'value':value}
            with self.subTest(name=name),self.assertRaises(ValueError):self.r.register(c)
        c=self.candidate();c['features']['last_close']['known_at']=registry.utc(self.t+1)
        with self.assertRaises(ValueError):self.r.register(c)

    def test_feature_cannot_precede_actual_receipt(self):
        c=self.candidate();c['features']['last_close']['known_at']=registry.utc(T+H)
        with self.assertRaises(ValueError):self.r.register(c)

    def test_unknown_feature_has_reason_and_no_invented_value(self):
        c=self.candidate();c['features']['last_close']=dict(value=None,known_at=None,evidence_ids=[],unknown_reason='insufficient history')
        got=self.r.register(c)
        self.assertIsNone(got['payload']['features']['last_close']['value'])

    def test_missing_or_wrong_type_feature_rejected(self):
        for value in [True, 'future high', float('nan')]:
            c=self.candidate();c['features']['last_close']['value']=value
            with self.subTest(value=value),self.assertRaises(ValueError):self.r.register(c)
        c=self.candidate();c['features']={}
        with self.assertRaises(ValueError):self.r.register(c)

    def test_feature_lookback_gap_is_not_silently_accepted(self):
        p=protocol();p['features']['last_close']['lookback_bars']={'1h':2}
        self.t=T-1
        other=registry.Registry.create(self.root.parent/'second',p,mode='synthetic',clock=lambda:self.t)
        self.t=T+H+1000
        a=other.ingest(candle(T-2*H));b=other.ingest(candle())
        c=self.candidate(evidence_ids=[a['record_sha256'],b['record_sha256']])
        c['features']['last_close']['evidence_ids']=c['evidence_ids']
        with self.assertRaises(ValueError):other.register(c)

    def test_equivalent_utc_representation_cannot_duplicate_an_event(self):
        self.register();c=self.candidate()
        c['formed_at']=c['formed_at'].replace('+00:00','Z')
        with self.assertRaises(ValueError):self.r.register(c)

    def test_receipt_unknown_fields_remain_visible(self):
        self.assertIn('quote_volume_unknown',self.e['payload']['quality_flags'])
        self.assertIn('source_market_event_time_unknown',self.e['payload']['quality_flags'])

    def test_write_that_reaches_observation_start_is_not_committed(self):
        c=self.candidate();before=self.r.verify()['records'];ticks=iter([self.t,T+2*H])
        self.r.clock=lambda:next(ticks)
        with self.assertRaises(ValueError):self.r.register(c)
        self.assertEqual(self.r.verify()['records'],before)

    def test_changed_first_record_universe_is_not_valid_even_with_new_hash(self):
        file=self.root/'protocols'/'0000000001.json';r=json.loads(file.read_text())
        r['payload']['allowed_timeframes']=['15m']
        r['record_sha256']=registry.sha_bytes(registry.canonical({k:v for k,v in r.items() if k!='record_sha256'}))
        # Remove the suffix to isolate validation of first-record metadata.
        for f in (self.root/'evidence').glob('*.json'):f.unlink()
        file.chmod(0o644);file.write_bytes(registry.canonical(r)+b'\n')
        with self.assertRaises(ValueError):self.r.verify()

    def test_label_with_cross_symbol_or_wrong_window_evidence_rejected(self):
        c=self.register();ids=self.future_evidence()
        other=self.r.ingest(candle(T+2*H,symbol='ETHUSDT'))
        wrong=self.r.ingest(candle(T+H))
        for rid in [other['record_sha256'],wrong['record_sha256']]:
            with self.subTest(rid=rid),self.assertRaises(ValueError):self.r.label(self.label(c,[rid]+ids[1:]))

    def test_candidate_does_not_accept_outcome_fields(self):
        for key in ('mfe_R0','label_completed_at','exit_time','realized_net_R'):
            c=self.candidate();c[key]=1
            with self.subTest(key=key),self.assertRaises(ValueError):self.r.register(c)

    def test_label_cannot_be_written_before_window_end(self):
        c=self.register()
        with self.assertRaises(ValueError):self.r.label(self.label(c,[]))

    def test_results_stored_separately_and_candidate_hash_unchanged(self):
        c=self.register();before=json.dumps(c,sort_keys=True);ids=self.future_evidence()
        label=self.r.label(self.label(c,ids))
        self.assertEqual(self.r.get(c['record_sha256']),c)
        self.assertEqual(before,json.dumps(c,sort_keys=True))
        self.assertEqual(label['payload']['candidate_sha256'],c['record_sha256'])
        self.assertTrue(list((self.root/'labels').glob('*.json')))
        self.assertNotIn('values',c['payload'])
        self.assertFalse(label['payload']['calculation_verified'])

    def test_missing_zero_and_wrong_symbol_observation_cannot_be_complete(self):
        for missing,zero in [(2,None),(None,1)]:
            with self.subTest(missing=missing,zero=zero):
                # Each case is isolated to retain the strict duplicate-evidence contract.
                temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
                self.root=Path(temp.name)/'journal';self.t=T-1
                self.r=registry.Registry.create(self.root,protocol(),mode='synthetic',clock=lambda:self.t)
                self.t=T+H+1000;self.e=self.r.ingest(candle());c=self.register()
                ids=self.future_evidence(skip=missing,zero=zero);self.t=T+8*H+1
                with self.assertRaises(ValueError):self.r.label(self.label(c,ids))
                incomplete=self.label(c,ids,status='incomplete',values={'mfe_R0':None,'mae_R0':None},limitations=['missing/zero observation'])
                self.assertEqual(self.r.label(incomplete)['payload']['status'],'incomplete')

    def test_label_requires_matching_frozen_horizon_candidate_and_keys(self):
        c=self.register();ids=self.future_evidence()
        for changes in [dict(horizon_hours=7),dict(candidate_sha256=self.e['record_sha256']),dict(values={'profit':1}),
                        dict(intrabar_order='high_before_low')]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.r.label(self.label(c,ids,**changes))

    def test_duplicate_label_rejected(self):
        c=self.register();ids=self.future_evidence();p=self.label(c,ids);self.r.label(p)
        with self.assertRaises(ValueError):self.r.label(p)

    def test_hash_chain_anchor_and_tampering_detected(self):
        c=self.register();anchor=self.r.checkpoint();self.assertTrue(self.r.verify(anchor=anchor)['anchor_consistent'])
        file=self.root/'candidates'/f"{c['sequence']:010d}.json"
        file.chmod(0o644);obj=json.loads(file.read_text());obj['payload']['stance']='short'
        file.write_text(json.dumps(obj))
        with self.assertRaises(ValueError):self.r.verify(anchor=anchor)

    def test_checkpoint_detects_suffix_deletion(self):
        c=self.register();anchor=self.r.checkpoint()
        (self.root/'candidates'/f"{c['sequence']:010d}.json").unlink()
        with self.assertRaises(ValueError):self.r.verify(anchor=anchor)

    def test_clock_rollback_rejected(self):
        self.register();self.t-=100
        with self.assertRaises(ValueError):self.r.ingest(candle(T-H))

    def test_exclusive_writer_lock_fails_closed(self):
        (self.root/'.write-lock').mkdir()
        with self.assertRaises(ValueError):self.register()

    def test_noncanonical_duplicate_json_keys_and_extra_files_rejected(self):
        with self.assertRaises(ValueError):registry.load_json('{"a":1,"a":2}')
        with self.assertRaises(ValueError):registry.load_json('{"a":NaN}')
        (self.root/'unexpected.json').write_text('{}')
        with self.assertRaises(ValueError):self.r.verify()

    def test_synthetic_is_never_forward_evidence(self):
        self.register();v=self.r.verify()
        self.assertEqual(v['mode'],'synthetic');self.assertFalse(v['independent_forward_evidence_claim'])

    def test_market_event_versions_and_overlapping_assets_not_iid(self):
        self.register();c=candle(symbol='ETHUSDT');e=self.r.ingest(c)
        candidate=self.candidate(symbol='ETHUSDT',market_event_key='same-global-shock',evidence_ids=[e['record_sha256']])
        candidate['features']['last_close']['evidence_ids']=[e['record_sha256']]
        self.r.register(candidate);a=self.r.audit()
        self.assertEqual(a['unique_events'],2);self.assertEqual(a['cross_symbol_overlap_pairs'],1)
        self.assertIsNone(a['proven_independent_sample_count'])


if __name__=='__main__':unittest.main()
