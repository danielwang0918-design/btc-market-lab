"""P1-04 tests written against the fixed pre-result RVOL specification."""
import csv
import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

import p1_candlestick_research as core
import p1_volume_rvol as m

H=core.FRAMES['1h']


def bars(volumes,frame='1h',gap_at=None):
    step=core.FRAMES[frame]
    return [core.Candle((i+(1 if gap_at is not None and i>=gap_at else 0))*step,
                        100,102,99,101,v) for i,v in enumerate(volumes)]


def observation(h=24,status='evaluated',net='1',directional='2'):
    return dict(symbol='BTCUSDT',timeframe='1h',pattern='bullish_engulfing',direction='long',
        split='train',signal_open_ms=str(20*H),signal_close_utc=core.utc(21*H),
        prior_trend_3='range',atr14_before_signal='1',horizon_hours=str(h),
        outcome_status=status,censor_reason='',entry_next_open_utc=core.utc(21*H),
        exit_close_utc=core.utc((21+h)*H),directional_favorable_pct=directional,paper_net_pct=net)


class RvolTests(unittest.TestCase):
    def test_denominator_is_previous_twenty_excluding_current(self):
        source=m.VolumeSource(bars([10]*20+[30]),'1h')
        f=source.feature_at(20)
        self.assertEqual(f['prior_mean_volume'],Decimal(10))
        self.assertEqual(f['rvol'],Decimal(3))
        self.assertEqual(f['prior_window_end_exclusive_ms'],20*H)
        self.assertEqual(f['available_at_ms'],21*H)

    def test_changing_current_does_not_change_denominator(self):
        a=m.VolumeSource(bars([10]*20+[1]),'1h').feature_at(20)
        b=m.VolumeSource(bars([10]*20+[1000]),'1h').feature_at(20)
        self.assertEqual(a['prior_mean_volume'],b['prior_mean_volume'])
        self.assertNotEqual(a['rvol'],b['rvol'])

    def test_append_or_mutate_future_cannot_change_feature(self):
        a=m.VolumeSource(bars([10]*20+[12]),'1h').feature_at(20)
        b=m.VolumeSource(bars([10]*20+[12]+[Decimal('1e30')]*20),'1h').feature_at(20)
        self.assertEqual(a,b)

    def test_window_rolls_and_drops_oldest_prior_bar(self):
        f=m.VolumeSource(bars([1000]+[10]*20+[20]),'1h').feature_at(21)
        self.assertEqual(f['prior_mean_volume'],Decimal(10))
        self.assertEqual(f['rvol'],Decimal(2))

    def test_first_twenty_bars_are_insufficient_not_low_volume(self):
        source=m.VolumeSource(bars([10]*21),'1h')
        for i in (0,1,19):
            f=source.feature_at(i)
            self.assertEqual(f['rvol_bin'],'insufficient_history')
            self.assertIsNone(f['rvol'])
        self.assertEqual(source.feature_at(20)['rvol_bin'],'normal')

    def test_gap_resets_full_twenty_bar_window(self):
        source=m.VolumeSource(bars([10]*43,gap_at=21),'1h')
        self.assertEqual(source.feature_at(20)['rvol'],1)
        self.assertEqual(source.feature_at(21)['rvol_bin'],'gap_in_lookback')
        self.assertEqual(source.feature_at(40)['rvol_bin'],'gap_in_lookback')
        self.assertEqual(source.feature_at(41)['rvol'],1)

    def test_zero_mean_is_undefined_not_infinite(self):
        for current in (0,100):
            f=m.VolumeSource(bars([0]*20+[current]),'1h').feature_at(20)
            self.assertEqual(f['rvol_bin'],'zero_prior_mean')
            self.assertEqual(f['prior_mean_volume'],0)
            self.assertIsNone(f['rvol'])

    def test_zero_current_is_valid_low_volume(self):
        f=m.VolumeSource(bars([10]*20+[0]),'1h').feature_at(20)
        self.assertEqual(f['rvol'],0)
        self.assertEqual(f['rvol_bin'],'low')

    def test_exact_inclusive_boundaries_without_epsilon(self):
        for current,expected in [('0.079999999999','low'),('0.08','normal'),
                                  ('0.119999999999','normal'),('0.12','high')]:
            with self.subTest(current=current):
                f=m.VolumeSource(bars([Decimal('.1')]*20+[Decimal(current)]),'1h').feature_at(20)
                self.assertEqual(f['rvol_bin'],expected)

    def test_extreme_values_are_retained_without_float_overflow_or_capping(self):
        f=m.VolumeSource(bars([Decimal('1e-30')]*20+[Decimal('1e30')]),'1h').feature_at(20)
        self.assertEqual(f['rvol'],Decimal('1e60'))
        self.assertTrue(f['extreme_rvol'])
        self.assertEqual(f['rvol_bin'],'high')

    def test_twenty_bars_on_each_allowed_frame(self):
        for frame in core.FRAMES:
            source=m.VolumeSource(bars([1]*20+[2],frame),frame)
            self.assertEqual(source.feature_at(20)['rvol'],2)
            self.assertEqual(source.feature_at(20)['available_at_ms'],21*core.FRAMES[frame])

    def test_subhour_frames_forbidden(self):
        for frame in ('1m','5m','15m','30m'):
            with self.subTest(frame=frame),self.assertRaises(ValueError):
                m.VolumeSource([],frame)

    def test_duplicate_unsorted_and_misaligned_times_rejected(self):
        a=bars([1,1])
        for bad in ([a[0],a[0]],list(reversed(a)),[core.Candle(1,100,102,99,101,1)]):
            with self.assertRaises(ValueError):m.VolumeSource(bad,'1h')

    def test_negative_and_nonfinite_volume_rejected(self):
        for v in ('-1','NaN','Infinity'):
            with self.subTest(v=v),self.assertRaises(ValueError):m.VolumeSource(bars([v]),'1h')

    def test_out_of_range_feature_index_rejected(self):
        source=m.VolumeSource(bars([1]),'1h')
        for i in (-1,1):
            with self.assertRaises(ValueError):source.feature_at(i)

    def test_frozen_protocol_is_not_outcome_tuned(self):
        p=json.loads(Path(m.__file__).with_name('P1_04_VOLUME_RVOL_PROTOCOL.json').read_text())
        self.assertEqual(p['lookback_bars'],20)
        self.assertEqual(p['low_if_rvol_less_than'],'0.8')
        self.assertEqual(p['high_if_rvol_at_least'],'1.2')
        self.assertFalse(p['new_out_of_sample'])


class SourceTests(unittest.TestCase):
    def raw(self,closed='true'):
        return ('symbol,interval,open_time,close_time,open,high,low,close,volume,is_closed,source\n'
                f'BTCUSDT,1h,0,3599999,100,102,99,101,0.123456789123456789,{closed},Binance Spot REST API\n').encode()

    def meta(self,raw):
        return dict(sha256=core.sha(raw),bytes=len(raw),frozen_bars=1,
                    frozen_last_close_exclusive_ms=H)

    def test_volume_precision_and_source_metadata_preserved(self):
        raw=self.raw();out=m.read_source(raw,'BTCUSDT','1h',self.meta(raw))
        self.assertEqual(out[0].v,Decimal('0.123456789123456789'))

    def test_source_sha_and_bytes_checked(self):
        raw=self.raw()
        for field,value in [('sha256','0'*64),('bytes',1)]:
            meta=self.meta(raw);meta[field]=value
            with self.assertRaisesRegex(ValueError,'SHA256|bytes'):
                m.read_source(raw,'BTCUSDT','1h',meta)

    def test_unclosed_wrong_symbol_and_cutoff_rejected(self):
        for raw in (self.raw('false'),self.raw().replace(b'BTCUSDT',b'ETHUSDT')):
            with self.assertRaises(ValueError):m.read_source(raw,'BTCUSDT','1h',self.meta(raw))
        raw=self.raw();meta=self.meta(raw);meta['frozen_last_close_exclusive_ms']=H-1
        with self.assertRaises(ValueError):m.read_source(raw,'BTCUSDT','1h',meta)

    def test_corrupted_context_evidence_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'context.csv').write_bytes(b'valid')
            m.check_known_files(root,{'context.csv':core.sha(b'valid')})
            (root/'context.csv').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'SHA256'):
                m.check_known_files(root,{'context.csv':core.sha(b'valid')})

    def test_duplicate_context_event_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'context.csv';row=observation()
            with p.open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=row.keys());w.writeheader();w.writerows([row,row])
            with self.assertRaisesRegex(ValueError,'Duplicate'):
                m.load_context(p)


class JoinAndStatisticsTests(unittest.TestCase):
    def contexts(self):
        r=observation();base={k:r[k] for k in ('symbol','timeframe','pattern','signal_open_ms','direction','split')}
        z={**base,'reference_frame':'4h','location':'near_confirmed_zone'}
        g={**base,'pattern_start_open_ms':str(19*H),'regime_data_end_ms':str(19*H),
           'trend':'bullish','volatility':'normal'}
        return r,z,g

    def test_exact_identity_context_join(self):
        r,z,g=self.contexts();out=m.attach_context(r,z,g)
        self.assertEqual(out['location'],'near_confirmed_zone')
        self.assertEqual(out['trend'],'bullish')
        self.assertEqual(out['volatility'],'normal')

    def test_context_mismatch_and_future_data_rejected(self):
        for field,value in [('signal_open_ms','0'),('split','validation'),
                            ('regime_data_end_ms',str(21*H)),('trend','unknown')]:
            r,z,g=self.contexts();g[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):m.attach_context(r,z,g)

    def test_complete_three_horizons_are_one_signal(self):
        groups=list(m.iter_pattern_groups(iter([observation(h) for h in (24,72,168)])))
        self.assertEqual(len(groups),1)
        self.assertEqual(len(groups[0]),3)

    def test_missing_duplicate_or_inconsistent_horizons_rejected(self):
        for rows in ([observation(24),observation(72)],
                     [observation(24),observation(24),observation(168)],
                     [observation(24),{**observation(72),'split':'validation'},observation(168)]):
            with self.assertRaises(ValueError):list(m.iter_pattern_groups(iter(rows)))

    def test_directional_positive_does_not_mean_positive_net(self):
        s=m.Stats();s.add(observation(net='-0.1',directional='0.1'))
        r=s.result()
        self.assertEqual(r['directional_positive_pct'],100)
        self.assertEqual(r['paper_net_positive_pct'],0)

    def test_unavailable_and_censored_are_separate_and_never_zero_returns(self):
        s=m.Stats()
        for status,reason in [('right_censored_or_split_boundary','data_gap'),
                              ('unavailable_fixed_time_fill','zero_volume_entry')]:
            r=observation(status=status,net='',directional='');r['censor_reason']=reason
            s.add(r)
        result=s.result()
        self.assertEqual((result['observed_events'],result['evaluated'],result['right_censored'],result['paper_fill_unavailable']),(2,0,1,1))
        self.assertEqual(result['unevaluated_pct'],100)
        self.assertIsNone(result['mean_paper_net_pct'])

    def test_invalid_non_evaluated_returns_and_nonfinite_values_rejected(self):
        for r in (observation(status='unavailable_fixed_time_fill',net='0',directional=''),
                  observation(status='right_censored_or_split_boundary',net='1',directional=''),
                  observation(net='NaN'),observation(status='unexpected')):
            with self.assertRaises(ValueError):m.Stats().add(r)

    def test_equal_notional_event_pf_and_mean(self):
        s=m.Stats()
        for net in ('4','-2','0'):s.add(observation(net=net))
        r=s.result()
        self.assertEqual(r['paper_event_profit_factor'],2)
        self.assertAlmostEqual(r['mean_paper_net_pct'],2/3,places=7)
        self.assertEqual(r['median_paper_net_pct'],0)
        self.assertEqual(r['paper_net_positive_pct'],round(100/3,6))

    def test_pf_without_losses_is_explicitly_undefined(self):
        s=m.Stats();s.add(observation(net='1'))
        self.assertIsNone(s.result()['paper_event_profit_factor'])
        self.assertEqual(s.result()['paper_event_pf_status'],'no_negative_returns')

    def test_full_grid_preserves_empty_and_undefined_groups(self):
        s=m.Stats();s.add(observation())
        rows=list(m.summary_rows({('BTCUSDT','low'):s},
            {'symbol':('BTCUSDT',),'rvol_bin':m.RVOL_BINS}))
        self.assertEqual(len(rows),6)
        self.assertEqual(sum(r['observed_events'] for r in rows),1)
        self.assertTrue(all(r['mean_paper_net_pct'] is None for r in rows if r['rvol_bin']!='low'))


if __name__=='__main__':unittest.main()
