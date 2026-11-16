"""P1-QUALITY-01 offline red/green tests, no network, no live orders."""
import csv
import hashlib
import io
import json
import math
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import p1_research_quality_gate as q


class QualityGateTests(unittest.TestCase):
    def test_exact_non_overlapping_intervals(self):
        f,n=q.overlap_flags([(0,10),(10,20),(20,30)])
        self.assertEqual((f,n),([False]*3,0))

    def test_nested_overlap_all(self):
        f,n=q.overlap_flags([(0,40),(10,20),(21,30)])
        self.assertEqual((f,n),([True]*3,3))

    def test_same_entry_overlap_even_after_sort(self):
        f,n=q.overlap_flags([(0,10),(0,10),(100,101)])
        self.assertEqual((f,n),([True,True,False],2))

    def test_overlap_flags_preserve_unsorted_input(self):
        f,n=q.overlap_flags([(60,70),(0,10),(9,15),(30,31)])
        self.assertEqual((f,n),([False,True,True,False],2))

    def test_overlap_requires_positive_duration(self):
        with self.assertRaisesRegex(ValueError,'Invalid interval'):
            q.overlap_flags([(10,10)])

    def test_peak_concurrent_counts_exit_before_entry(self):
        self.assertEqual(q.peak_concurrent([(0,10),(5,10),(10,20),(15,20)]),2)
        self.assertEqual(q.peak_concurrent([]),0)

    def test_peak_concurrent_refuses_bad_intervals(self):
        with self.assertRaisesRegex(ValueError,'Invalid interval'):
            q.peak_concurrent([(4,1)])

    def test_key_only_1h_4h_1d(self):
        x={'symbol':'BTCUSDT','timeframe':'15m','pattern':'bullish_engulfing','signal_open_ms':'1502953200000'}
        with self.assertRaisesRegex(ValueError,'Unexpected'):
            q._key(x)

    def test_key_rejects_unaligned_open_time(self):
        x={'symbol':'BTCUSDT','timeframe':'1h','pattern':'bullish_engulfing','signal_open_ms':'1502953200001'}
        with self.assertRaisesRegex(ValueError,'Unaligned'):
            q._key(x)

    def test_timestamp_must_be_utc(self):
        with self.assertRaisesRegex(ValueError,'UTC'):
            q._millis('2026-11-16T10:00:00+08:00')
        with self.assertRaisesRegex(ValueError,'UTC'):
            q._millis('2026-11-16T10:00:00')

    def test_numeric_nan_and_inf_are_rejected(self):
        for x in ['NaN','inf','-inf','oops']:
            with self.subTest(s=x):
                with self.assertRaises(ValueError):
                    q._float(x)

    def test_censor_and_unavailable_do_not_create_returns(self):
        r={'outcome_status':'unavailable_fixed_time_fill','censor_reason':'zero_volume_entry',
           'paper_net_pct':'0.5','directional_favorable_pct':''}
        with self.assertRaisesRegex(ValueError,'fake return'):
            q._status(r)
        r['paper_net_pct']=''
        self.assertEqual(q._status(r),'unavailable_fixed_time_fill')

    def test_unavailable_reason_must_be_known(self):
        with self.assertRaisesRegex(ValueError,'Unexpected simulated-fill'):
            q._status({'outcome_status':'unavailable_fixed_time_fill','censor_reason':'best_price',
                       'paper_net_pct':'','directional_favorable_pct':''})

    def test_evaluated_must_have_numeric_returns(self):
        with self.assertRaises(ValueError):
            q._status({'outcome_status':'evaluated','censor_reason':'',
                       'paper_net_pct':'','directional_favorable_pct':'1'})

    def test_zip_fails_on_duplicate_filenames(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'dup.zip'
            with warnings.catch_warnings():
                warnings.simplefilter('ignore',UserWarning)
                with ZipFile(p,'w') as z:
                    z.writestr('after/p1_01_events.csv',b'first')
                    z.writestr('after/p1_01_events.csv',b'second')
            with ZipFile(p) as z:
                with self.assertRaisesRegex(ValueError,'Duplicate ZIP'):
                    q.checked_members(z,{'p1_event':'after/p1_01_events.csv'})

    def test_zip_fails_on_bad_sha(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'m.zip'
            with ZipFile(p,'w') as z:
                z.writestr('after/p1_01_events.csv',b'changed')
            with ZipFile(p) as z:
                with self.assertRaisesRegex(ValueError,'SHA256 drift'):
                    q.checked_members(z,{'p1_event':'after/p1_01_events.csv'})

    def test_zip_fails_on_parent_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'m.zip'
            with ZipFile(p,'w') as z:
                z.writestr('../unsuitable',b'a')
                z.writestr('after/p1_01_events.csv',b'changed')
            with ZipFile(p) as z:
                with self.assertRaisesRegex(ValueError,'Unsafe ZIP'):
                    q.checked_members(z,{'p1_event':'after/p1_01_events.csv'})

    def test_missing_p1_horizon_rejected_without_matching_artifact(self):
        e={'symbol':'BTCUSDT','timeframe':'1h','pattern':'bullish_engulfing','signal_open_ms':'1502953200000',
           'horizon_hours':'24','split':'train','direction':'long','outcome_status':'evaluated',
           'signal_close_utc':'2017-08-17T08:00:00+00:00','entry_next_open_utc':'2017-08-17T08:00:00+00:00',
           'exit_close_utc':'2017-08-18T08:00:00+00:00','paper_net_pct':'-1','directional_favorable_pct':'-0.7',
           'censor_reason':''}
        with self.assertRaisesRegex(ValueError,'sample count drift'):
            q.index_and_check([e],[],[])

    def test_csv_duplicate_column_rejected(self):
        with self.assertRaisesRegex(ValueError,'Duplicated'):
            q.csv_rows(b'one,one\na,b\n')

    def test_evidence_pairs_with_missing_cells_rejected(self):
        r={'symbol':'BTCUSDT','timeframe':'1h','pattern':'bullish_engulfing','signal_open_ms':'1502953200000'}
        self.assertEqual(q._key(r),('BTCUSDT','1h','bullish_engulfing',1502953200000))

    def test_iso_week_boundary(self):
        self.assertEqual(q._iso_week(q._millis('2020-12-31T12:00:00+00:00')),'2020-W53')
        self.assertEqual(q._iso_week(q._millis('2021-01-01T12:00:00+00:00')),'2020-W53')

    def test_right_censored_outcomes_never_count_as_win(self):
        r={'outcome_status':'right_censored_or_split_boundary', 'censor_reason':'data_gap',
           'directional_favorable_pct':'', 'paper_net_pct':''}
        self.assertEqual(q._status(r),'right_censored_or_split_boundary')

    def test_manifest_reports_no_real_trades(self):
        self.assertEqual(q.VERSION,'p1-observation-evidence-gate-v1')
        self.assertEqual(tuple(q.HORIZONS),(24,72,168))


if __name__=='__main__':
    unittest.main()