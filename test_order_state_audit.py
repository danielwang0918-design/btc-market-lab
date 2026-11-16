"""Offline order-state tests; never use lower-than-hour candle for trade decisions."""
import unittest
from datetime import datetime, timezone
from decimal import Decimal

import order_state_audit as audit

HOUR = 3600000


def bar(hour, open_, high, low, close):
    return dict(open_time=hour*HOUR, close_time=(hour+1)*HOUR-1,
                open=str(open_), high=str(high), low=str(low), close=str(close))


def candidate(*, side='long', setup_id='ob-1', zone_low='95', zone_high='100',
              entry_reference='100', stop='94', target='112'):
    return dict(setup_id=setup_id,target_event_id='swing-a',side=side,
                signal_time_ms=HOUR, setup_confirm_ms=0, setup_age_hours=1,
                zone_low=zone_low,zone_high=zone_high,entry_reference=entry_reference,
                stop=stop,target=target,net_rr='1.6')


def snap(candidate_=None,integrity=True):
    return dict(candidate=candidate_,integrity_ok=integrity)


class FakeEngine:
    @staticmethod
    def _utc(ms):
        return datetime.fromtimestamp(ms/1000,tz=timezone.utc).isoformat()
    @staticmethod
    def _number(value, name, *, positive=False):
        n=Decimal(str(value))
        if positive and n<=0:raise ValueError(name)
        return n
    @staticmethod
    def _candidate_allowed(c, min_rr, max_age):
        return bool(c and float(c['net_rr'])>=min_rr and float(c['setup_age_hours'])<=max_age)
    @staticmethod
    def _exit_on_bar(p,b):
        op,hi,lo=(Decimal(b[z]) for z in ('open','high','low'))
        stop,target=p['stop'],p['target']
        if p['side']=='long':
            if op <= stop:return op,'stop_gap'
            if lo <= stop:return stop,'stop'
            if hi >= target:return target,'target'
        else:
            if op >= stop:return op,'stop_gap'
            if hi >= stop:return stop,'stop'
            if lo <= target:return target,'target'
        return None
    @staticmethod
    def _make_trade(p,raw,exit_ms,reason,idx,fees,slip):
        return dict(setup_id=p['setup_id'],exit_reason=reason,bars_held=idx-p['entry_index']+1,
                    entry_time_utc=FakeEngine._utc(p['entry_time_ms']),exit_time_utc=FakeEngine._utc(exit_ms))


class OrderStateTests(unittest.TestCase):
    def setUp(self):
        self.engine=FakeEngine()

    def replay(self, rows, snapshots):
        return audit.trace_split(rows,snapshots,0,len(rows),engine=self.engine)

    def types(self, events):
        return [e['event'] for e in events]

    def test_only_hourly_or_higher_is_used(self):
        self.assertEqual(audit.DECISION_TIMEFRAMES,('1h','4h','1d'))
        self.assertNotIn('1m',audit.DECISION_TIMEFRAMES)
        self.assertNotIn('15m',audit.DECISION_TIMEFRAMES)

    def test_long_ob_target_same_hour_is_canceled_with_unknown_order(self):
        rows=[bar(0,110,111,108,109),bar(1,108,114,96,102)]
        events,trades=self.replay(rows,[snap(candidate()),snap()])
        self.assertEqual(trades,[])
        overlap=[e for e in events if e['event']=='pending_target_and_zone_same_hour_cancel']
        self.assertEqual(len(overlap),1)
        self.assertEqual(overlap[0]['ordering'],'intrabar_target_vs_zone_order_unknown')
        self.assertFalse(overlap[0]['intra_hour_fill_observed'])

    def test_open_inside_zone_and_target_same_hour_is_still_cancelled_by_baseline(self):
        rows=[bar(0,110,111,105,107),bar(1,97,114,96,105)]
        events,trades=self.replay(rows,[snap(candidate()),snap()])
        self.assertEqual(trades,[])
        overlaps=[e for e in events if e['event']=='pending_target_and_zone_same_hour_cancel']
        self.assertEqual(overlaps[0]['ordering'],'zone_present_at_open_but_reference_fill_unproven')
        self.assertTrue(overlaps[0]['zone_at_open'])

    def test_target_at_open_prior_to_ob_touch_is_identified(self):
        rows=[bar(0,110,111,107,109),bar(1,114,115,96,108)]
        events,trades=self.replay(rows,[snap(candidate()),snap()])
        self.assertEqual(trades,[])
        o=[e for e in events if e['event']=='pending_target_and_zone_same_hour_cancel'][0]
        self.assertEqual(o['ordering'],'target_already_at_open_before_zone_approach')

    def test_short_overlap_is_canceled_and_classified(self):
        c=candidate(side='short',zone_low='105',zone_high='110',entry_reference='105',stop='111',target='92')
        rows=[bar(0,99,103,97,100),bar(1,102,109,90,104)]
        events,trades=self.replay(rows,[snap(c),snap()])
        self.assertFalse(trades)
        self.assertIn('pending_target_and_zone_same_hour_cancel',self.types(events))

    def test_zone_touch_stop_after_signal_creates_modeled_one_hour_stop(self):
        rows=[bar(0,110,111,108,109),bar(1,108,109,93,100)]
        events,trades=self.replay(rows,[snap(candidate()),snap()])
        self.assertEqual(len(trades),1)
        self.assertEqual(trades[0]['exit_reason'],'stop')
        self.assertEqual(trades[0]['bars_held'],1)
        self.assertEqual(trades[0]['entry_time_utc'],self.engine._utc(HOUR))
        e=[e for e in events if e['event']=='pending_ob_wick_touch_modeled_fill'][0]
        self.assertTrue(e['open_time_is_fill_proxy'])
        self.assertFalse(e['actual_fill_observed'])

    def test_target_without_zone_cancels(self):
        rows=[bar(0,108,110,107,108),bar(1,108,114,106,110)]
        events,trades=self.replay(rows,[snap(candidate()),snap()])
        self.assertFalse(trades)
        self.assertIn('pending_target_without_zone_cancel',self.types(events))

    def test_no_touch_changed_candidate_cancels(self):
        rows=[bar(0,110,111,107,108),bar(1,108,110,105,109)]
        c=candidate()
        changed=candidate(setup_id='new-setup')
        events,trades=self.replay(rows,[snap(c),snap(changed)])
        self.assertFalse(trades)
        self.assertIn('pending_signal_changed_cancel',self.types(events))
        self.assertEqual(self.types(events).count('signal_accepted_pending_from_closed_1h'),2)
        self.assertIn('pending_split_end_unresolved',self.types(events))

    def test_integrity_failure_cancels_pending(self):
        rows=[bar(0,110,111,107,108),bar(1,108,110,105,109)]
        events,trades=self.replay(rows,[snap(candidate()),snap(None,integrity=False)])
        self.assertFalse(trades)
        self.assertEqual(self.types(events).count('pending_expired_or_integrity_cancel'),1)

    def test_data_gap_cancels_pending_and_never_fills_on_gap(self):
        rows=[bar(0,110,111,107,108),bar(3,108,110,93,100)]
        events,trades=self.replay(rows,[snap(candidate()),snap()])
        self.assertFalse(trades)
        self.assertIn('pending_data_gap_cancel',self.types(events))

    def test_stop_gap_at_open_prevents_new_entry(self):
        rows=[bar(0,110,111,107,108),bar(1,90,100,89,94)]
        events,trades=self.replay(rows,[snap(candidate()),snap()])
        self.assertFalse(trades)
        self.assertIn('pending_stop_already_breached_at_open_cancel',self.types(events))

    def test_pending_cannot_fill_on_signal_candle(self):
        rows=[bar(0,100,112,93,105)]
        events,trades=self.replay(rows,[snap(candidate())])
        self.assertFalse(trades)
        self.assertIn('signal_accepted_pending_from_closed_1h',self.types(events))
        self.assertIn('pending_split_end_unresolved',self.types(events))

    def test_second_signal_same_setup_not_duplicated(self):
        c=candidate()
        rows=[bar(0,110,111,107,108),bar(1,108,114,96,102),bar(2,108,109,106,107)]
        events,trades=self.replay(rows,[snap(c),snap(c),snap(c)])
        self.assertFalse(trades)
        self.assertEqual(self.types(events).count('signal_accepted_pending_from_closed_1h'),1)

    def test_rejects_invalid_snapshot_length(self):
        with self.assertRaisesRegex(ValueError,'Invalid rows'):
            self.replay([bar(0,1,2,1,1)],[])

    def test_overlap_will_not_invent_fill_when_reference_never_touched(self):
        c=candidate(entry_reference='95')
        b=bar(1,97,114,96,105)
        o=audit._overlap_classification(c,b)
        self.assertFalse(o['reference_price_in_hour'])
        self.assertFalse(o['intra_hour_fill_observed'])


if __name__=='__main__':unittest.main()

class AggregationTests(unittest.TestCase):
    def test_complete_verified_triplet_and_subhour_gate(self):
        import tempfile, json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            for s in ('BTCUSDT','ETHUSDT','SOLUSDT'):
                x={'symbol':s,'version':audit.VERSION,'strategy_unchanged':True,
                   'subhour_price_data_used':False,'decision_timeframes_only':['1h','4h','1d'],
                   'splits':{'test':{'modeled_fills':1,'baseline_trades_verified':1,
                     'observed_signal_count':2,'no_fill_pending_cancellations':1,
                     'target_zone_same_1h_cancellations':1,
                     'pending_at_split_end_unresolved':0,
                     'target_zone_sequence_classification':{'intrabar_target_vs_zone_order_unknown':1}}}}
                (p/f'{s}_order_state_audit.json').write_text(json.dumps(x))
            a=audit.aggregate_reports(p,p/'out')
            self.assertEqual(a['test_totals_observational']['baseline_trades_verified'],3)
            self.assertEqual(a['test_totals_observational']['target_zone_same_1h_cancellations'],3)
            self.assertTrue((p/'out'/'order_state_test_summary.md').exists())
            q=p/'SOLUSDT_order_state_audit.json'
            j=json.loads(q.read_text());j['subhour_price_data_used']=True;q.write_text(json.dumps(j))
            with self.assertRaisesRegex(ValueError,'forbids'):
                audit.aggregate_reports(p,p/'out')