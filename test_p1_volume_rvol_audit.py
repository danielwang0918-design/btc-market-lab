"""Evidence audit must catch changed prices, future volume and omitted groups."""
import csv
import tempfile
import unittest
from pathlib import Path

import p1_volume_rvol as study
import p1_volume_rvol_audit as audit
from test_p1_volume_rvol import bars,observation


class VolumeAuditTests(unittest.TestCase):
    def feature(self):
        source=bars([10]*20+[100])
        raw=study.VolumeSource(source,'1h').feature_at(20)
        return source,{k:'' if v is None else str(v) for k,v in raw.items()}

    def test_independent_fraction_reference_accepts_causal_rvol(self):
        source,row=self.feature()
        audit.check_feature(row,source,20,'1h',50)

    def test_including_current_in_mean_cannot_pass_independent_audit(self):
        source,row=self.feature();row['prior_mean_volume']='14.5'
        with self.assertRaisesRegex(ValueError,'feature'):
            audit.check_feature(row,source,20,'1h',50)

    def test_lookback_gap_cannot_be_presented_as_valid_rvol(self):
        source=bars([10]*25,gap_at=15)
        row=study.VolumeSource(source,'1h').feature_at(24)
        row={k:'' if v is None else str(v) for k,v in row.items()}
        row['rvol']='1';row['rvol_bin']='normal'
        with self.assertRaisesRegex(ValueError,'feature'):
            audit.check_feature(row,source,24,'1h',50)

    def test_changed_old_return_or_censor_status_cannot_pass(self):
        original=observation();changed={**original,'paper_net_pct':'100'}
        with self.assertRaisesRegex(ValueError,'P1-01'):
            audit.check_legacy_row(original,changed)
        changed={**original,'outcome_status':'unavailable_fixed_time_fill'}
        with self.assertRaisesRegex(ValueError,'P1-01'):
            audit.check_legacy_row(original,changed)

    def summary_fixture(self,root):
        s=study.Stats();row=observation();s.add(row)
        grid={'symbol':('BTCUSDT',),'rvol_bin':study.RVOL_BINS}
        report=list(study.summary_rows({('BTCUSDT','low'):s},grid))
        path=root/'summary.csv'
        study.write_csv(path,tuple(report[0]),report)
        expected=audit.EvidenceStats();expected.add(row)
        return path,grid,{('BTCUSDT','low'):expected},report

    def test_every_empty_and_undefined_group_is_required(self):
        with tempfile.TemporaryDirectory() as d:
            path,grid,expected,report=self.summary_fixture(Path(d))
            self.assertEqual(audit.check_summary(path,grid,expected)['rows'],6)
            study.write_csv(path,tuple(report[0]),report[:-1])
            with self.assertRaisesRegex(ValueError,'group'):
                audit.check_summary(path,grid,expected)

    def test_mean_and_profit_factor_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as d:
            path,grid,expected,report=self.summary_fixture(Path(d))
            report[0]['mean_paper_net_pct']=10
            study.write_csv(path,tuple(report[0]),report)
            with self.assertRaisesRegex(ValueError,'metric'):
                audit.check_summary(path,grid,expected)

    def test_independent_counts_exclude_unavailable_from_returns(self):
        s=audit.EvidenceStats()
        s.add(observation(net='-1',directional='1'))
        r=observation(status='unavailable_fixed_time_fill',net='',directional='')
        r['censor_reason']='zero_volume_exit';s.add(r)
        out=s.result()
        self.assertEqual(out['evaluated'],1)
        self.assertEqual(out['paper_fill_unavailable'],1)
        self.assertEqual(out['unevaluated_pct'],50)
        self.assertEqual(out['directional_positive_pct'],100)
        self.assertEqual(out['paper_net_positive_pct'],0)


if __name__=='__main__':unittest.main()
