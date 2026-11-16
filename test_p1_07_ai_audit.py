import unittest
from decimal import Decimal
import p1_07_ai_audit as m


def plan(key='x', status='eligible', **extra):
    row=dict(event_id=key,policy='A',status=status,side='long',symbol='BTCUSDT',
        registration_ms='1602720000000',signal_time_ms='1602720000000',setup_confirm_ms='1602719999999',
        entry_time_ms='1602723600000',feature_cutoff_ms='1602723600000',raw_entry='100',stop='95',target='115',
        planned_net_RR='2.7',SL_distance_pct='5',TP_distance_pct='15',entry_delay_hours='1',atr='2',
        MA_1h='bullish',MA_4h='bullish',MA_1d='bullish',volatility='normal')
    row.update(extra)
    return row


def outcome(key='x', **extra):
    row=dict(event_id=key,policy='A',scenario='source_stop_first',exit_time_ms='1602727200000',
        net_pnl_usdt='20',r_multiple='1',exit_reason='target',path_dependent_outcome='False',
        gap_or_zero_volume_path_unknown='False')
    row.update(extra)
    return row


class AuditTests(unittest.TestCase):
    def test_untraded_is_unknown_not_negative_meta_label(self):
        rows=m.audit_rows([plan(status='invalid_before_entry')],[])
        self.assertEqual(rows[0]['label_net_positive'],'')
        self.assertEqual(rows[0]['audit_status'],'no_execution_label')

    def test_extra_scenarios_and_policies_do_not_multiply_labels(self):
        rows=m.audit_rows([plan(),plan(policy='S_ATR1')],[outcome(),outcome(scenario='feasible_upper'),outcome(policy='S_ATR1')])
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['label_net_positive'],1)

    def test_duplicate_source_labels_fail_closed(self):
        with self.assertRaisesRegex(ValueError,'duplicate'):
            m.audit_rows([plan()],[outcome(),outcome()])

    def test_missing_eligible_outcome_fails_instead_of_loss_label(self):
        with self.assertRaisesRegex(ValueError,'eligible'):
            m.audit_rows([plan()],[])

    def test_path_ambiguity_and_gaps_are_not_trusted_targets(self):
        for flag in ['path_dependent_outcome','gap_or_zero_volume_path_unknown']:
            rows=m.audit_rows([plan()],[outcome(**{flag:'True'})])
            self.assertEqual(rows[0]['label_net_positive'],'')
            self.assertFalse(rows[0]['complete_case_train_ready'])

    def test_missing_label_quality_is_unknown_not_assumed_clean(self):
        label=outcome();del label['path_dependent_outcome']
        row=m.audit_rows([plan()],[label])[0]
        self.assertEqual(row['label_net_positive'],'')
        self.assertEqual(row['audit_status'],'unknown_label_quality')

    def test_feature_after_entry_is_a_violation(self):
        rows=m.audit_rows([plan(feature_cutoff_ms='1602723600001')],[outcome()])
        self.assertEqual(rows[0]['audit_status'],'future_feature')
        self.assertEqual(rows[0]['label_net_positive'],'')

    def test_train_label_cannot_cross_validation_boundary(self):
        rows=m.audit_rows([plan()],[outcome(exit_time_ms=str(m.VALIDATION+1))])
        self.assertEqual(rows[0]['audit_status'],'label_crosses_split')

    def test_missing_ATR_is_not_filled_using_all_history(self):
        rows=m.audit_rows([plan(atr='')],[outcome()])
        self.assertEqual(rows[0]['label_net_positive'],1)
        self.assertFalse(rows[0]['complete_case_train_ready'])
        self.assertIn('atr_fraction',rows[0]['missing_numeric_features'])

    def test_stop_label_is_distinct_from_net_profit_label(self):
        rows=m.audit_rows([plan()],[outcome(net_pnl_usdt='-1',exit_reason='max_hold_72h')])
        self.assertEqual(rows[0]['label_stop'],0)
        self.assertEqual(rows[0]['label_net_positive'],0)

    def test_overlap_counts_use_half_open_label_intervals(self):
        rows=[dict(entry_time_ms=0,exit_time_ms=10,event_id='a'),dict(entry_time_ms=5,exit_time_ms=15,event_id='b'),dict(entry_time_ms=15,exit_time_ms=20,event_id='c')]
        self.assertEqual(m.overlap(rows),dict(pair_overlaps=1,events_with_overlap=2,maximum_concurrent_labels=2))

    def test_realized_fields_and_identifiers_never_enter_feature_whitelist(self):
        blocked={'net_pnl_usdt','r_multiple','exit_time_ms','path_dependent_outcome','status','anchor_status','quantity','equity_before','event_id','setup_id'}
        self.assertFalse(blocked & set(m.NUMERIC+m.CATEGORICAL))


def training_rows(n=200):
    rows=[]
    for i in range(n):
        p=plan(key=str(i),planned_net_RR=str(1.5+(i%7)),SL_distance_pct=str(1+i%3),atr='2')
        r=m.audit_rows([p],[outcome(key=str(i),net_pnl_usdt='1' if i%2 else '-1')])[0]
        rows.append(r)
    return rows


class MetaTests(unittest.TestCase):
    def test_sample_gate_does_not_fabricate_probabilities(self):
        state,predictions,scores=m.fit_meta(training_rows(199))
        self.assertFalse(state['model_trained'])
        self.assertTrue(all(r['probability']=='' and not r['accept_ml'] for r in predictions))
        self.assertTrue(all(isinstance(r['accept_fixed'],bool) for r in predictions))

    def test_validation_prices_and_labels_cannot_change_train_scaler_or_coefficients(self):
        rows=training_rows()
        val=m.audit_rows([plan(key='v',registration_ms=str(m.VALIDATION),signal_time_ms=str(m.VALIDATION),
            setup_confirm_ms=str(m.VALIDATION-1),entry_time_ms=str(m.VALIDATION+3600000),feature_cutoff_ms=str(m.VALIDATION+3600000))],
            [outcome(key='v',exit_time_ms=str(m.VALIDATION+7200000))])[0]
        a,_,_=m.fit_meta(rows+[val])
        b,_,_=m.fit_meta(rows+[dict(val,planned_net_RR=Decimal('9999'),label_net_positive=0)])
        self.assertEqual(a['imputer_statistics'],b['imputer_statistics'])
        self.assertEqual(a['scaler_mean'],b['scaler_mean'])
        self.assertEqual(a['coefficients'],b['coefficients'])

    def test_future_quality_mask_never_prevents_exante_prediction(self):
        rows=training_rows()
        unknown=m.audit_rows([plan(key='unknown')],[outcome(key='unknown',gap_or_zero_volume_path_unknown='True')])[0]
        state,predictions,_=m.fit_meta(rows+[unknown])
        self.assertTrue(state['model_trained'])
        p=next(r for r in predictions if r['event_id']=='unknown')
        self.assertIsInstance(p['probability'],float)
        self.assertEqual(state['training_rows'],200)

    def test_train_label_at_cutoff_is_purged_before_sample_gate(self):
        rows=training_rows()
        rows[-1]['exit_time_ms']=m.VALIDATION
        state,_,_=m.fit_meta(rows)
        self.assertFalse(state['model_trained'])
        self.assertEqual(state['training_rows'],199)

    def test_prediction_population_with_future_features_fails_closed(self):
        row=m.audit_rows([plan(key='future',feature_cutoff_ms='1602723600001')],[outcome(key='future')])[0]
        with self.assertRaisesRegex(ValueError,'future feature'):
            m.fit_meta(training_rows()+[row])


if __name__=='__main__':unittest.main()
