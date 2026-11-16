import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

import p1_07_mechanism as m


def outcome(**updates):
    row=dict(event_id='one',symbol='BTCUSDT',side='long',registration_ms='1602720000000',
        entry_time_ms='1602720000000',exit_time_ms='1602723600000',raw_entry='100',
        raw_exit='90',stop='90',target='120',quantity='1',planned_risk='10.3',risk_budget='20',
        fees_usdt='.2',slippage_usdt='.1',net_pnl_usdt='-10.3',r_multiple='-1',atr='5',
        exit_reason='stop',path_dependent_outcome='False',gap_or_zero_volume_path_unknown='False')
    row.update(updates)
    return row


class ArithmeticTests(unittest.TestCase):
    def setUp(self):
        self.a=outcome()
        self.b=outcome(raw_exit='115',stop='85',quantity='.5',planned_risk='7.7',
            fees_usdt='.125',slippage_usdt='.075',net_pnl_usdt='7.3',r_multiple=str(D('7.3')/D('7.7')),
            exit_time_ms='1602730800000',exit_reason='target')

    def test_fixed_quantity_and_resizing_sum_to_archived_delta(self):
        r=m.common_effect(self.a,self.b)
        self.assertEqual(r['delta_net_USDT'],D('17.6'))
        self.assertEqual(r['fixed_qA_gross_price_effect_USDT'],D(25))
        self.assertEqual(r['fixed_qA_cost_increase_USDT'],D('.1'))
        self.assertEqual(r['fixed_qA_outcome_effect_USDT'],D('24.9'))
        self.assertEqual(r['resize_effect_USDT'],D('-7.3'))
        self.assertEqual(r['fixed_qA_outcome_effect_USDT']+r['resize_effect_USDT'],r['delta_net_USDT'])

    def test_cost_share_is_cost_in_normal_stop_risk_not_total_realized_cost(self):
        self.assertEqual(m.cost_share(self.a),(D('10.3')-10)/D('10.3'))

    def test_hold_extension_and_smaller_quantity_are_explicit(self):
        r=m.common_effect(self.a,self.b)
        self.assertEqual(r['quantity_ratio_S_to_A'],D('.5'))
        self.assertEqual(r['holding_hours_change'],D(2))
        self.assertTrue(r['stop_widened'])

    def test_changed_target_cannot_masquerade_as_stop_only(self):
        with self.assertRaisesRegex(ValueError,'entry or target'):
            m.common_effect(self.a,{**self.b,'target':'119'})

    def test_changed_entry_time_cannot_be_paired_as_same_execution(self):
        with self.assertRaisesRegex(ValueError,'entry or target'):
            m.common_effect(self.a,{**self.b,'entry_time_ms':'1602723600000'})

    def test_cost_identity_corruption_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'cost identity'):
            m.common_effect({**self.a,'net_pnl_usdt':'-999'},self.b)

    def test_short_sign_is_mirrored(self):
        a=outcome(side='short',raw_exit='110',stop='110',target='80')
        b=outcome(side='short',raw_exit='85',stop='115',target='80',quantity='.5',planned_risk='7.7',
            fees_usdt='.125',slippage_usdt='.075',net_pnl_usdt='7.3',r_multiple=str(D('7.3')/D('7.7')))
        self.assertEqual(m.common_effect(a,b)['delta_net_USDT'],D('17.6'))


class PopulationTests(unittest.TestCase):
    def setUp(self):
        self.plans={k:dict(event_id=k,symbol='BTCUSDT',side='long',registration_ms='1602720000000',status='eligible') for k in ['one','two','three']}
        self.aplans=dict(self.plans)
        self.bplans={k:dict(v,status='net_RR_below_1.5' if k=='two' else 'pending_closed_state_cancel' if k=='three' else 'eligible') for k,v in self.plans.items()}
        self.a={'one':outcome(),'two':outcome(event_id='two',raw_exit='110.3',fees_usdt='.2',slippage_usdt='.1',net_pnl_usdt='10',r_multiple='1')}
        self.b={'one':outcome(raw_exit='115',stop='85',quantity='.5',planned_risk='7.7',fees_usdt='.125',slippage_usdt='.075',net_pnl_usdt='7.3',r_multiple=str(D('7.3')/D('7.7')))}

    def test_zero_inclusive_population_identity_and_exclusion_sign(self):
        rows=m.population_details(self.aplans,self.bplans,self.a,self.b,'fractional_spot','source_stop_first')
        r=m.summarize(rows)
        self.assertEqual(r['opportunities'],3)
        self.assertEqual(r['both_filled'],1)
        self.assertEqual(r['A_only'],1)
        self.assertEqual(r['neither_filled'],1)
        self.assertEqual(r['common_fill_delta_USDT'],D('17.6'))
        self.assertEqual(r['exclusion_effect_USDT'],D(-10))
        self.assertEqual(r['total_delta_USDT'],D('7.6'))
        self.assertEqual(r['delta_USDT_per_opportunity'],D('7.6')/3)

    def test_unsupported_spot_short_is_not_mislabelled_RR_exclusion(self):
        self.aplans['one']['side']='short';self.bplans['one']['side']='short'
        self.a['one']['side']='short';self.b['one']['side']='short'
        rows=m.population_details(self.aplans,self.bplans,self.a,self.b,'fractional_spot','source_stop_first')
        one=next(r for r in rows if r['event_id']=='one')
        self.assertEqual(one['fill_class'],'unsupported_direction')
        self.assertEqual(one['delta_net_USDT'],0)

    def test_empty_group_metrics_are_undefined_not_zero_accuracy(self):
        r=m.summarize([])
        self.assertIsNone(r['delta_USDT_per_opportunity'])
        self.assertIsNone(r['A_win_rate_pct'])

    def test_missing_opportunity_on_one_side_fails_closed(self):
        with self.assertRaisesRegex(ValueError,'opportunity'):
            m.population_details(self.aplans,{},self.a,self.b,'fractional_spot','source_stop_first')


class InputTests(unittest.TestCase):
    def test_unpinned_source_is_rejected_without_result_fabrication(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'source.zip';p.write_bytes(b'wrong')
            with self.assertRaisesRegex(ValueError,'SHA256'):
                m.run(p,Path(tmp)/'result')

    def test_nonempty_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/'result';out.mkdir();(out/'old.txt').write_text('keep')
            with self.assertRaisesRegex(ValueError,'empty'):
                m.run(root/'source.zip',out)
            self.assertEqual((out/'old.txt').read_text(),'keep')


if __name__=='__main__':unittest.main()
