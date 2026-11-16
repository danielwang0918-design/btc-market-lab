"""Deterministic P0 state-timeline audit tests; no network, trading or subhour candles."""
from __future__ import annotations
import csv
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import order_state_bos_timeline as a


def ms(hour):return hour*3_600_000

def evt(direction, i):return {'direction':direction,'event_id':f'bos-{direction}-{i}','confirmed_at_close_ms':ms(i)-1}

def vector(d='bullish', override=None):
    z={iv:evt(d,1) for iv in a.INTERVALS}
    for iv,v in (override or {}).items():z[iv]=v
    return z


def record(start=1,end=6,symbol='BTCUSDT',side='long'):
    return {'symbol':symbol,'setup_id':'frozen-ob','original_side':side,'pending_hours':end-start,
            'accepted_open_utc':a.utc(ms(start)),'canceled_open_utc':a.utc(ms(end))}


class FakeIndex:
    def __init__(self,values):self.values=values
    def at(self,ms_):
        return self.values[ms_]


class BosTimelineTests(unittest.TestCase):
    def test_fixed_decision_timeframes_are_1h_and_above(self):
        self.assertEqual(a.INTERVALS,('1d','4h','1h'))
        for forbidden in ('1m','5m','15m'):
            self.assertNotIn(forbidden,a.INTERVALS)

    def test_first_4h_reversal_only_at_cancellation(self):
        states={ms(k):vector(override={'4h':evt('bearish',6)}) if k==7 else vector() for k in range(2,8)}
        out,path=a.first_divergence(record(),FakeIndex(states))
        self.assertEqual(out['first_deviating_intervals'],['4h'])
        self.assertEqual(out['cancel_deviating_intervals'],['4h'])
        self.assertEqual(out['first_bos_mismatch_delay_hours'],5)
        self.assertFalse(out['detected_before_formal_cancel'])
        self.assertEqual(out['first_deviation_detail'][0]['type'],'new_opposite_confirmed_bos')
        self.assertEqual(len(path),5)

    def test_1h_and_4h_simultaneous_change(self):
        states={ms(k):vector(override={'4h':evt('bearish',6),'1h':evt('bearish',6)}) if k==7 else vector() for k in range(2,8)}
        out,_=a.first_divergence(record(),FakeIndex(states))
        self.assertEqual(out['first_deviating_intervals'],['4h','1h'])
        self.assertEqual(out['canceled_bos_directions'],{'1d':'bullish','4h':'bearish','1h':'bearish'})

    def test_1d_reversal_from_short_acceptance(self):
        states={ms(k):vector('bearish',{'1d':evt('bullish',6)}) if k==7 else vector('bearish') for k in range(2,8)}
        out,_=a.first_divergence(record(side='short'),FakeIndex(states))
        self.assertEqual(out['first_deviating_intervals'],['1d'])

    def test_window_expiration_is_not_mislabeled_as_opposite_bos(self):
        states={ms(k):vector(override={'4h':None}) if k==7 else vector() for k in range(2,8)}
        with self.assertRaisesRegex(ValueError,'not reproduced'):
            a.first_divergence(record(),FakeIndex(states))

    def test_detects_early_change_in_trajectory(self):
        states={ms(k):vector(override={'1h':evt('bearish',4)}) if k>=5 else vector() for k in range(2,8)}
        out,_=a.first_divergence(record(),FakeIndex(states))
        self.assertTrue(out['detected_before_formal_cancel'])
        self.assertEqual(out['first_bos_mismatch_delay_hours'],3)

    def test_accepted_signal_must_reconstruct_aligned(self):
        states={ms(k):vector(override={'1d':evt('bearish',1)}) for k in range(2,8)}
        with self.assertRaisesRegex(ValueError,'accepted OB'):
            a.first_divergence(record(),FakeIndex(states))

    def test_mtf_at_cancel_must_reconstruct_mixed(self):
        states={ms(k):vector() for k in range(2,8)}
        with self.assertRaisesRegex(ValueError,'not reproduced'):
            a.first_divergence(record(),FakeIndex(states))

    def test_naive_date_rejected(self):
        with self.assertRaisesRegex(ValueError,'timezone'):
            a.parse_ms('2025-01-01T00:00:00')
        with self.assertRaisesRegex(ValueError,'1H'):
            a.parse_ms('2025-01-01T00:15:00Z')

    def test_fail_closed_missing_symbol_aggregate(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):a.aggregate(Path(tmp))

    def test_aggregate_expects_exact_frozen_94_case_cohort(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for sym,n in [('BTCUSDT',23),('ETHUSDT',36),('SOLUSDT',35)]:
                body={'version':a.VERSION,'symbol':sym,'status':'verified_from_closed_formal_bos_events',
                      'uses_subhour_candles':False,'strategy_changed':False,
                      'decision_timeframes':list(a.INTERVALS),'test_cancellations_stage03_reason_mtf':n,
                      'first_deviating_interval_counts_nonexclusive':{'4h':n},
                      'first_deviating_interval_counts_exclusive':{'4h':n},
                      'first_deviation_types':{'new_opposite_confirmed_bos':n},
                      'cancellation_opposing_interval_combinations':{'4h':n}}
                (root/f'{sym}_bos_timeline.json').write_text(json.dumps(body))
            z=a.aggregate(root)
            self.assertEqual(z['test_mtf_mismatch_cancellations'],94)
            self.assertEqual(z['first_deviating_interval_counts_exclusive']['4h'],94)
            d=json.loads((root/'BTCUSDT_bos_timeline.json').read_text());d['test_cancellations_stage03_reason_mtf']=22
            (root/'BTCUSDT_bos_timeline.json').write_text(json.dumps(d))
            with self.assertRaisesRegex(ValueError,'cohort changed'):a.aggregate(root)

    def test_rejects_unverified_stage03_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);d=root/'data';d.mkdir();c=root/'causes';c.mkdir();replay=root/'replay';replay.mkdir()
            for iv in a.INTERVALS:(d/f'BTCUSDT_{iv}.csv').write_text('frozen')
            (d/'multi_asset_coverage.json').write_text('{}')
            (replay/'BTCUSDT_order_state_events.csv').write_text('events')
            (replay/'BTCUSDT_order_state_audit.json').write_text('{}')
            rows=[{'symbol':'BTCUSDT','split':'test','setup_id':'frozen-ob',
                   'accepted_open_utc':a.utc(ms(1)),'canceled_open_utc':a.utc(ms(4)),
                   'pending_hours':'3','category':a.CATEGORY,'original_side':'long',
                   'new_mtf_status':'mixed','snapshot_close_utc':a.utc(ms(5))}]
            with (c/'BTCUSDT_cancellation_causes.csv').open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
            source={'coverage':a.sha(d/'multi_asset_coverage.json'),
                    'order_state_events_csv':a.sha(replay/'BTCUSDT_order_state_events.csv'),
                    'order_state_json':a.sha(replay/'BTCUSDT_order_state_audit.json')}
            source.update({f'BTCUSDT_{iv}.csv':a.sha(d/f'BTCUSDT_{iv}.csv') for iv in a.INTERVALS})
            meta={'version':a.SOURCE_VERSION,'symbol':'BTCUSDT','status':'verified_reconstructed_test_cancellation_snapshots',
                  'strategy_changed':False,'uses_subhour_data':False,'trade_decision_timeframes':list(a.INTERVALS),
                  'test_canceled_signals':1,'reason_counts':{a.CATEGORY:1},'source_sha256':source}
            (c/'BTCUSDT_cancellation_causes.json').write_text(json.dumps(meta))
            pair={'setup_id':'frozen-ob','accepted_open_ms':ms(1),'canceled_open_ms':ms(4),'pending_hours':3}
            mock=SimpleNamespace(_read_inputs=lambda *args,**kwargs: ({},[],[pair]))
            result=a.read_causes(c,'BTCUSDT',d,replay,prior=mock)
            self.assertEqual(len(result),1)
            (d/'BTCUSDT_4h.csv').write_text('modified')
            with self.assertRaisesRegex(ValueError,'prior stage-03'):a.read_causes(c,'BTCUSDT',d,replay,prior=mock)

    def test_rejects_incomplete_per_trade_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);d=root/'data';d.mkdir();c=root/'causes';c.mkdir();replay=root/'replay';replay.mkdir()
            for iv in a.INTERVALS:(d/f'BTCUSDT_{iv}.csv').write_text('frozen')
            (d/'multi_asset_coverage.json').write_text('{}')
            (replay/'BTCUSDT_order_state_events.csv').write_text('events')
            (replay/'BTCUSDT_order_state_audit.json').write_text('{}')
            source={'coverage':a.sha(d/'multi_asset_coverage.json'),
                    'order_state_events_csv':a.sha(replay/'BTCUSDT_order_state_events.csv'),
                    'order_state_json':a.sha(replay/'BTCUSDT_order_state_audit.json')}
            source.update({f'BTCUSDT_{iv}.csv':a.sha(d/f'BTCUSDT_{iv}.csv') for iv in a.INTERVALS})
            meta={'version':a.SOURCE_VERSION,'symbol':'BTCUSDT','status':'verified_reconstructed_test_cancellation_snapshots',
                  'strategy_changed':False,'uses_subhour_data':False,'trade_decision_timeframes':list(a.INTERVALS),
                  'test_canceled_signals':1,'reason_counts':{a.CATEGORY:1},'source_sha256':source}
            (c/'BTCUSDT_cancellation_causes.json').write_text(json.dumps(meta))
            (c/'BTCUSDT_cancellation_causes.csv').write_text('symbol,setup_id\nBTCUSDT,frozen-ob\n')
            pair={'setup_id':'frozen-ob','accepted_open_ms':ms(1),'canceled_open_ms':ms(4),'pending_hours':3}
            mock=SimpleNamespace(_read_inputs=lambda *args,**kwargs: ({},[],[pair]))
            with self.assertRaisesRegex(ValueError,'incomplete'):a.read_causes(c,'BTCUSDT',d,replay,prior=mock)


if __name__=='__main__':unittest.main()