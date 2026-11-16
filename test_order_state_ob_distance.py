"""Network-free P0-ORDER-STATE-05 tests; no sub-hour trading data ever read."""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

import order_state_ob_distance as a


class DistanceAuditTests(unittest.TestCase):
    def test_locked_timeframes(self):
        self.assertEqual(a.INTERVALS, ('1d','4h','1h'))
        self.assertNotIn('1m', a.INTERVALS)
        self.assertEqual(sum(a.EXPECTED_BOS.values()),94)
        self.assertEqual(sum(a.EXPECTED_CHANGED.values()),136)

    def test_ob_id_exact_direction_and_formation(self):
        self.assertEqual(a.ob_from_setup_id('BTCUSDT:1h:order_block:bullish:3600000:7199999:100', 'BTCUSDT','long'),3600000)
        with self.assertRaises(ValueError):
            a.ob_from_setup_id('BTCUSDT:1h:order_block:bullish:3600000:7199999:100','BTCUSDT','short')

    def test_reject_invalid_ob_identifier(self):
        for x in ('../../etc:1h:order_block:bullish:3600000:7199999:100',
                  'BTCUSDT:1m:order_block:bullish:3600000:7199999:100',
                  'BTCUSDT:1h:order_block:bullish:3600001:7199999:100',
                  'BTCUSDT:1h:order_block:bullish:3600000:7200000:100',
                  'BTCUSDT:1h:order_block:bullish:3600000:7199999:nan'):
            with self.subTest(x=x),self.assertRaises(ValueError):
                a.ob_from_setup_id(x,'BTCUSDT','long')

    def test_timestamp_must_be_aware_aligned(self):
        self.assertEqual(a.utc_ms('2025-01-01T00:00:00+00:00'),1735689600000)
        with self.assertRaises(ValueError):a.utc_ms('2025-01-01T00:30:00+00:00')
        with self.assertRaises(ValueError):a.utc_ms('2025-01-01T00:00:00')

    def test_price_distance_correct_and_signed(self):
        self.assertEqual(a.nearest_zone_gap(109,95,100),9)
        self.assertEqual(a.nearest_zone_gap(90,95,100),5)
        self.assertEqual(a.nearest_zone_gap(97,95,100),0)
        self.assertEqual(a.directional_gap(109,95,100,'long'),9)
        self.assertEqual(a.directional_gap(90,95,100,'short'),5)
        self.assertEqual(a.directional_gap(90,95,100,'long'),-10)

    def test_oor_side_or_zone_rejected(self):
        with self.assertRaises(ValueError):a.directional_gap(100,95,110,'buy')
        with self.assertRaises(ValueError):a.nearest_zone_gap(100,110,95)

    def sample(self,high='102',low='98',close='100'):
        return [{'open_time':str(i*a.HOUR_MS),'high':high,'low':low,'close':close} for i in range(20)]

    def test_atr14_no_lookahead_includes_previous_close(self):
        bars=self.sample()
        bars[15]['high']='200' # Must not affect ATR as of idx14
        self.assertAlmostEqual(a.atr14_at(bars,14),4.0)
        self.assertGreater(a.atr14_at(bars,15),4.0)

    def test_atr14_requires_15_candles(self):
        with self.assertRaises(ValueError):a.atr14_at(self.sample(),13)

    def test_atr14_gap_is_not_silently_accepted(self):
        b=self.sample();b[6]['open_time']=str(7*a.HOUR_MS)
        with self.assertRaises(ValueError):a.atr14_at(b,14)

    def test_atr14_nonpositive_true_range_rejected(self):
        with self.assertRaises(ValueError):a.atr14_at(self.sample('100','100','100'),14)

    def test_atr14_uses_gap_vs_prior_close(self):
        b=self.sample(high='110',low='100',close='104')
        b[1]['high']='130';b[1]['low']='105';b[1]['close']='109'
        self.assertGreater(a.atr14_at(b,14),10)

    def test_summarize_is_descriptive_only(self):
        rows=[{'waiting_hours':h,'ob_width_atr14_acceptance':.8,'gap_at_signal_atr14':2,
               'gap_before_cancel_atr14':d,'gap_before_cancel_bps':100,
               'gap_at_cancel_atr14':1.1,'gap_at_cancel_bps':40,
               'original_side':'long','canceled_open_utc':'2025-01-01T00:00:00+00:00'} for h,d in [(4,1.4),(8,2.5),(12,3)]]
        t=a.summarize(rows)
        self.assertEqual(t['n'],3)
        self.assertEqual(t['wait_hours']['median'],8)
        self.assertEqual(t['gap_before_cancel_atr14']['median'],2.5)
        self.assertEqual(t['gap_shrank_before_cancel'],1)
        self.assertEqual(t['side_distribution'],{'long':3})

    def test_summarize_consistent_for_csv_numeric_strings(self):
        numeric=[{'waiting_hours':8,'ob_width_atr14_acceptance':.5,
                  'gap_at_signal_atr14':10.0,'gap_before_cancel_atr14':2.0,
                  'gap_before_cancel_bps':120,'gap_at_cancel_atr14':1.0,
                  'gap_at_cancel_bps':0.0,'original_side':'short',
                  'canceled_open_utc':'2025-01-01T00:00:00+00:00'}]
        strings=[{k:(str(v) if isinstance(v,(int,float)) else v) for k,v in record.items()} for record in numeric]
        self.assertEqual(a.summarize(strings),a.summarize(numeric))
        self.assertEqual(a.summarize(strings)['gap_shrank_before_cancel'],1)

    def test_empty_summary(self):
        self.assertEqual(a.summarize([]),{'n':0})

    def test_invalid_price(self):
        for p in ['nan','inf','0','-10','unknown']:
            with self.subTest(p=p),self.assertRaises(ValueError):a.valid_price(p)

    def test_symbol_extraction_checks_verified_archives_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for d in ['data','s3','s4','replay','out']:(root/d).mkdir()
            (root/'s4'/'BTCUSDT_bos_timeline.json').write_text(json.dumps({'status':'unverified'}))
            (root/'s3'/'BTCUSDT_cancellation_causes.json').write_text(json.dumps({}))
            with self.assertRaisesRegex(ValueError,'verification status'):
                a.extract_symbol(root/'data',root/'s3',root/'s4',root/'replay','BTCUSDT',root/'out')

    def test_unknown_symbol_rejected(self):
        with self.assertRaisesRegex(ValueError,'frozen'):
            a.extract_symbol(Path('.'),Path('.'),Path('.'),Path('.'),'NVDABUSDT',Path('.'))


if __name__=='__main__': unittest.main()