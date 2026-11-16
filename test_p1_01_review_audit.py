"""Acceptance checks for the review's independent evidence accounting."""
import csv
import io
import tempfile
import unittest
from pathlib import Path

import p1_01_review_audit as audit
from p1_candlestick_research import Candle, FRAMES


class ReviewEvidenceTests(unittest.TestCase):
    def rows(self):
        return [
            {'symbol':'BTCUSDT','timeframe':'1h','pattern':'bullish_engulfing',
             'split':'train','signal_open_ms':str(i*3_600_000),'horizon_hours':'24',
             'outcome_status':'evaluated' if i<2 else 'right_censored_or_split_boundary',
             'directional_favorable_pct':'1' if i<2 else '',
             'paper_net_pct':'-0.2' if i<2 else ''} for i in (1,0,48)]

    def scan(self,rows):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'events.csv'
            with path.open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=rows[0].keys())
                writer.writeheader();writer.writerows(rows)
            return audit.scan(path)

    def test_overlap_counts_both_events_and_never_counts_censored(self):
        report,*_=self.scan(self.rows())
        overlap=report['overlap_by_symbol_frame_horizon'][0]
        self.assertEqual(overlap['evaluated_windows'],2)
        self.assertEqual(overlap['windows_overlapping_another'],2)
        self.assertEqual(overlap['overlap_pair_count'],1)
        all_metrics=next(x for x in report['aggregate_metrics'] if x['scope']=='all')
        self.assertEqual(all_metrics['right_censored'],1)
        self.assertEqual(all_metrics['directional_positive_percent'],100)
        self.assertEqual(all_metrics['net_positive_percent_paper'],0)

    def test_touching_window_endpoints_are_not_overlap(self):
        rows=self.rows()[:2]
        rows[0]['signal_open_ms']=str(24*3_600_000)
        report,*_=self.scan(rows)
        self.assertEqual(report['overlap_by_symbol_frame_horizon'][0]['overlap_pair_count'],0)

    def test_duplicate_event_is_rejected(self):
        rows=self.rows()
        with self.assertRaisesRegex(ValueError,'Duplicate pattern'):
            self.scan(rows+[rows[0]])

    def test_missing_zero_group_is_zero_count_with_null_performance(self):
        row={'symbol':'BTCUSDT','timeframe':'1h','pattern':'three_black_crows',
             'split':'train','horizon_hours':24,'observed_signals':0,'evaluated':0,
             'right_censored':0,'paper_fill_unavailable':0,'directional_positive_percent':None,
             'net_positive_percent_paper':None,'mean_paper_net_pct':None,
             'median_paper_net_pct':None}
        result=audit.comparison([], [row],audit.GROUP)[0]
        self.assertEqual(result['observed_signals_delta'],0)
        self.assertIsNone(result['mean_paper_net_pct_delta'])

    def test_summary_tampering_cannot_pass_independent_csv_check(self):
        report,_,_,_,_,groups,_,_=self.scan(self.rows())
        metrics=next(x for x in report['aggregate_metrics'] if x['scope']=='all')
        row={k:self.rows()[0][k] for k in audit.GROUP}
        row.update({k:metrics[k] for k in audit.METRICS})
        audit.validate_summary([row],groups)
        row['net_positive_percent_paper']=100
        with self.assertRaisesRegex(ValueError,'independently counted'):
            audit.validate_summary([row],groups)

    def test_cross_frame_open_disagreement_is_reported_without_rewriting_source(self):
        hourly=[Candle(i*FRAMES['1h'],100+i,102+i,99+i,101+i,1) for i in range(4)]
        higher=[Candle(0,'100.1',105,99,104,4)]
        report=audit.compare_timeframe(hourly,higher,'4h')
        self.assertEqual(report['checked_complete_windows'],1)
        self.assertEqual(report['mismatch_windows'],1)
        self.assertEqual(set(report['mismatches'][0]['differences']),{'open'})
        self.assertEqual(str(higher[0].o),'100.1')

    def test_missing_hour_is_skipped_in_cross_frame_audit(self):
        hourly=[Candle(i*FRAMES['1h'],100+i,102+i,99+i,101+i,1) for i in (0,2,3)]
        higher=[Candle(0,100,105,99,104,4)]
        report=audit.compare_timeframe(hourly,higher,'4h')
        self.assertEqual(report['checked_complete_windows'],0)
        self.assertEqual(report['skipped_incomplete_hourly_windows'],1)
        self.assertEqual(report['mismatch_windows'],0)

    def test_leading_zero_volume_explains_native_exchange_open_difference(self):
        hourly=[Candle(0,100,100,100,100,0)]+[
            Candle(i*FRAMES['1h'],100+i,102+i,99+i,101+i,1) for i in range(1,4)]
        higher=[Candle(0,101,105,100,104,3)]
        report=audit.compare_timeframe(hourly,higher,'4h')
        self.assertEqual(report['mismatch_windows'],1)
        self.assertEqual(report['unexplained_mismatch_windows'],0)
        self.assertTrue(report['mismatches'][0]['matches_nonzero_volume_hours'])

    def compare_single_row(self,after):
        before={**self.rows()[0], 'entry_next_open_utc':'entry', 'exit_close_utc':'exit'}
        fields=tuple(before)
        saved={audit.identity(before):tuple(before[k] for k in fields)}
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'after.csv'
            with path.open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=after.keys());w.writeheader();w.writerow(after)
            return audit.scan(path,baseline_rows=saved,legacy_fields=fields,
                delta_writer=csv.DictWriter(io.StringIO(),fieldnames=['change',*fields]))

    def test_unavailable_fill_preserves_the_old_return_in_review_evidence(self):
        after={**self.rows()[0],'entry_next_open_utc':'','exit_close_utc':'',
               'outcome_status':'unavailable_fixed_time_fill','paper_net_pct':'',
               'directional_favorable_pct':'','censor_reason':'zero_volume_entry'}
        report,_,_,_,_,_,changes,_=self.compare_single_row(after)
        self.assertEqual(report['common_rows_unchanged'],0)
        self.assertEqual(changes[0]['paper_net_pct_before'],'-0.2')
        self.assertEqual(changes[0]['reason'],'zero_volume_entry')

    def test_changed_cost_or_signal_cannot_hide_as_an_expected_review_change(self):
        after={**self.rows()[0],'entry_next_open_utc':'entry','exit_close_utc':'exit',
               'paper_net_pct':'-0.1'}
        with self.assertRaisesRegex(ValueError,'Unexpected existing'):
            self.compare_single_row(after)
        after.update(outcome_status='unavailable_fixed_time_fill',
            entry_next_open_utc='',exit_close_utc='',paper_net_pct='',
            directional_favorable_pct='',split='validation',censor_reason='zero_volume_entry')
        with self.assertRaisesRegex(ValueError,'Unexpected existing'):
            self.compare_single_row(after)


if __name__=='__main__':
    unittest.main()
