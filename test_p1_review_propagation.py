import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import p1_review_propagation_audit as m
import p1_zone_context as zone
import p1_market_regime as regime

class PropagationTests(unittest.TestCase):
    def test_readonly_frames_and_fixed_thresholds(self):
        self.assertEqual(list(regime.FRAMES), ['1h','4h','1d'])
        self.assertEqual(list(zone.FRAMES), ['1h','4h','1d'])
        self.assertEqual(zone.LOOKBACK_DAYS,180)
        self.assertEqual(regime.MA_PERIOD,200)
        self.assertEqual(regime.ATR_PERIOD,14)

    def test_expected_revised_input_and_source_pins(self):
        self.assertEqual(zone.P1_EVENTS_SHA,m.NEW_P1_EVENTS_SHA256)
        self.assertEqual(regime.P1_EVENTS_SHA,m.NEW_P1_EVENTS_SHA256)
        self.assertEqual(zone.P1_MANIFEST_SHA,m.NEW_P1_MANIFEST_SHA256)
        self.assertEqual(regime.P1_MANIFEST_SHA,m.NEW_P1_MANIFEST_SHA256)
        self.assertEqual(zone.P1_RUN,43857787070)
        self.assertEqual(regime.P1_RUN,43857787070)

    def test_zone_unavailable_not_assigned_win_or_loss(self):
        from collections import defaultdict
        state=defaultdict(lambda:dict(events=0,evaluated=0,censored=0,unavailable=0,net_sum=0.,net_pos=0,directional_pos=0))
        row={'symbol':'BTCUSDT','timeframe':'1h','pattern':'bullish_engulfing','split':'train','horizon_hours':'24',
             'outcome_status':'unavailable_fixed_time_fill','censor_reason':'zero_volume_exit','paper_net_pct':'', 'directional_favorable_pct':''}
        zone._summary_accumulate(state,row,'near_confirmed_zone')
        group=list(state.values())[0]
        self.assertEqual((group['events'],group['evaluated'],group['censored'],group['unavailable']),(1,0,0,1))
        row['paper_net_pct']='100.0'
        with self.assertRaises(ValueError):zone._summary_accumulate(state,row,'near_confirmed_zone')

    def test_regime_unavailable_not_counted_as_censored(self):
        state={'events':0,'evaluated':0,'censored':0,'unavailable':0,'directional_favorable':0,'paper_net_positive':0,'paper_nets':[]}
        row={'outcome_status':'unavailable_fixed_time_fill','censor_reason':'zero_volume_entry','paper_net_pct':'','directional_favorable_pct':''}
        regime.update_outcome(state,row)
        self.assertEqual([state[x] for x in ('events','evaluated','censored','unavailable')],[1,0,0,1])
        row['censor_reason']='not_known'
        with self.assertRaises(ValueError):regime.update_outcome(state,row)

    def test_compare_detects_drift_missing_and_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);old=p/'old.csv';new=p/'new.csv';fields=list(m.K)+['trend']
            def save(path,vals):
                with path.open('w',newline='') as f:
                    w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(vals)
            a=dict(zip(fields,('BTCUSDT','1h','bullish_engulfing','100','bullish')))
            b=dict(zip(fields,('ETHUSDT','4h','bearish_engulfing','200','bearish')))
            save(old,[a]);save(new,[a,b]);digest=m.digest(old)
            counts,added=m.compare(old,new,expected_hash=digest,fields=('trend',))
            self.assertEqual(counts['added_events'],1)
            self.assertEqual(added[0]['symbol'],'ETHUSDT')
            changed={**a,'trend':'bearish'};save(new,[changed,b])
            with self.assertRaisesRegex(ValueError,'drift'):m.compare(old,new,expected_hash=digest,fields=('trend',))
            save(new,[a,a])
            with self.assertRaisesRegex(ValueError,'duplicated'):m.compare(old,new,expected_hash=digest,fields=('trend',))
            save(new,[b])
            with self.assertRaisesRegex(ValueError,'disappeared'):m.compare(old,new,expected_hash=digest,fields=('trend',))
            save(new,[a,b]);save(old,[changed])
            with self.assertRaisesRegex(ValueError,'changed'):m.compare(old,new,expected_hash=digest,fields=('trend',))

if __name__=='__main__':unittest.main()