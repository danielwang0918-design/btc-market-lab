"""Network-free tests of P0 intrabar entry audit; no strategy backtest changes."""
import csv
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock

import execution_audit_02 as a


def utc(ms):
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc).isoformat()


def hourly(o='105',h='108',l='95',c='99',symbol='BTCUSDT'):
    return {'symbol':symbol,'interval':'1h','open_time':'0','open_time_utc':utc(0),
            'close_time':str(a.HOUR-1),'is_closed':'true','open':o,'high':h,'low':l,'close':c}


def trade(**changes):
    t={'symbol':'BTCUSDT','split':'test','setup_id':'test1','signal_time_utc':utc(0),
       'entry_time_utc':utc(0),'exit_time_utc':utc(a.HOUR),'direction':'long',
       'raw_entry_price':'100','stop_price':'96','target_price':'110',
       'exit_reason':'stop','r_multiple':'-1','bars_held':1}
    t.update(changes)
    return t


def minute_fixture(*, stop_first=False, simultaneous=False):
    rows=[]
    for i in range(60):
        o=105 if i==0 else 102
        h=108 if i==0 else 103
        l=101
        c=102
        if simultaneous:
            if i==5: l=95
            elif i==10: l=99
        elif stop_first:
            if i==5: o=98;h=99;l=95;c=98
            elif i==10: l=99
        else:
            if i==5: l=99
            elif i==10: l=95
        rows.append([i*a.MINUTE,str(o),str(h),str(l),str(c),'1',i*a.MINUTE+a.MINUTE-1])
    return rows


class TestP0Audit(unittest.TestCase):
    def test_entry_hour_reference_and_stop_on_approach_side(self):
        r=a.audit_trade(trade(),hourly())
        self.assertTrue(r['reference_in_hourly_range'])
        self.assertTrue(r['same_hour_stop'])
        self.assertTrue(r['first_hour_stop_reference_crossing_before_stop_under_continuous_path'])
        self.assertFalse(r['first_hour_stop_before_reference_possible'])
        self.assertTrue(r['actual_limit_fill_unproven'])

    def test_approach_wrong_side_is_ambiguous(self):
        bar=hourly(o='98',h='108',l='95',c='99')
        r=a.audit_trade(trade(),bar)
        self.assertTrue(r['first_hour_stop_before_reference_possible'])
        self.assertFalse(r['first_hour_stop_reference_crossing_before_stop_under_continuous_path'])

    def test_reference_outside_hour_is_flagged_not_assumed_filled(self):
        bar=hourly(o='99',h='99.5',l='95',c='98')
        r=a.audit_trade(trade(),bar)
        self.assertFalse(r['reference_in_hourly_range'])
        self.assertFalse(r['first_hour_stop_before_reference_possible'])

    def test_invalid_timestamp_holding_period_and_price_order_fail(self):
        for t in [trade(exit_time_utc=utc(0)),trade(bars_held=2),
                  trade(stop_price='120'),trade(entry_time_utc=utc(10)),
                  trade(signal_time_utc='1970-01-01T00:00:00'),trade(direction='sideways')]:
            with self.subTest(t=t),self.assertRaises(ValueError):a.audit_trade(t,hourly())

    def test_unobserved_stop_cannot_be_reported_same_hour(self):
        with self.assertRaisesRegex(ValueError,'stop is not present'):
            a.audit_trade(trade(),hourly(l='97',c='99'))

    def test_minute_validator_complete_60_and_equal_1h(self):
        candle=minute_fixture()
        check={'open':'105','high':'108','low':'95','close':'102'}
        out=a._validate_1m(candle,check,0)
        self.assertEqual(len(out),60)
        self.assertIsNone(a._validate_1m(candle[:59],check,0))
        tampered=list(list(t) for t in candle)
        tampered[1][0]+=1
        self.assertIsNone(a._validate_1m(tampered,check,0))
        self.assertIsNone(a._validate_1m(candle,dict(check,high='109'),0))
        bad=list(list(t) for t in candle)
        bad[5][3]='NaN'
        self.assertIsNone(a._validate_1m(bad,check,0))

    def test_minute_separation_before_after_and_same_minute(self):
        r=a.audit_trade(trade(),hourly())
        result=a.minute_order(r,a._validate_1m(minute_fixture(),{'open':'105','high':'108','low':'95','close':'102'},0))
        self.assertEqual(result['minute_stop_vs_reference'],'reference_minute_before_stop_minute')
        first=a.minute_order(r,a._validate_1m(minute_fixture(stop_first=True),{'open':'105','high':'108','low':'95','close':'102'},0))
        self.assertEqual(first['minute_stop_vs_reference'],'stop_first_observed_before_reference_minute')
        # Force the first simultaneous stop/reference inside a single 1m candle.
        both=[{'time':i*a.MINUTE,'open':a.Decimal('105'),'high':a.Decimal('108'),
               'low':a.Decimal('95') if i==7 else a.Decimal('101'),'close':a.Decimal('102')} for i in range(60)]
        result=a.minute_order(r,both)
        self.assertEqual(result['minute_stop_vs_reference'],'same_1m_bar_order_unresolved')

    def test_verification_request_budget_has_no_network_calls(self):
        c=Mock()
        rows=[a.audit_trade(trade(),hourly()) for _ in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError,'budget'):
                a.verify_minute(rows,{'BTCUSDT':{0:hourly()}},Path(tmp),client=c,max_requests=2)
        c.get_json.assert_not_called()

    def test_verification_uses_only_trade_entry_hour_and_preserves_others(self):
        client=Mock()
        client.get_json.return_value=minute_fixture()
        r=a.audit_trade(trade(),hourly(o='105',h='108',l='95',c='102'))
        train=a.audit_trade(trade(split='train'),hourly(o='105',h='108',l='95',c='102'))
        with tempfile.TemporaryDirectory() as tmp:
            a.verify_minute([r,train],{'BTCUSDT':{0:hourly(o='105',h='108',l='95',c='102')}},Path(tmp),client=client,throttle_seconds=0)
            self.assertEqual(r['minute_status'],'complete_verified')
            self.assertEqual(train['minute_status'],'not_requested')
            params=client.get_json.call_args.args[2]
            self.assertEqual((params['interval'],params['startTime'],params['endTime'],params['limit']),('1m',0,a.HOUR-1,60))
            self.assertEqual(r['minute_stop_vs_reference'],'reference_minute_before_stop_minute')
            self.assertTrue(r['minute_sha256'])

    def test_verification_rejects_hourly_mismatch_without_counting_verified(self):
        c=Mock();c.get_json.return_value=minute_fixture()
        r=a.audit_trade(trade(),hourly()) # 1H close 99, minute closes 102
        with tempfile.TemporaryDirectory() as tmp:
            a.verify_minute([r],{'BTCUSDT':{0:hourly()}},Path(tmp),client=c,throttle_seconds=0)
        self.assertEqual(r['minute_status'],'incomplete_or_hourly_mismatch')

    def test_frozen_archive_sha_verification_and_realistic_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            row=hourly()
            output=io.StringIO()
            writer=csv.DictWriter(output,fieldnames=list(row));writer.writeheader();writer.writerow(row)
            data=output.getvalue().encode('utf-8-sig')
            digest=a.sha(data)
            cov={'status':'complete','symbols':{'BTCUSDT':{'intervals':{'1h':{'csv_sha256':digest}}}}}
            coverage=json.dumps(cov).encode('utf-8')
            hist=root/'hist.zip'
            with zipfile.ZipFile(hist,'w') as z:
                z.writestr('multi_asset_coverage.json',coverage)
                z.writestr('BTCUSDT_1h.csv',data)
            report={'symbol':'BTCUSDT','strategy':'mtf-bos-ob-retest-v1','status':'complete','trades':[trade()],
                    'input_sha256':{'multi_asset_coverage.json':a.sha(coverage),'BTCUSDT_1h.csv':digest}}
            rep=root/'anything.zip'
            with zipfile.ZipFile(rep,'w') as z:
                z.writestr('BTCUSDT_fixed_calendar_baseline.json',json.dumps(report))
            res=a.run(hist,[rep],['BTCUSDT'],root/'out')
            self.assertEqual(res['summary']['test_all_symbols']['same_hour_stops'],1)
            self.assertEqual(res['summary']['test_all_symbols']['wins'],0)
            self.assertTrue((root/'out'/'execution_audit_02_trades.csv').exists())
            bad=dict(report,input_sha256={**report['input_sha256'],'BTCUSDT_1h.csv':'fake'})
            with zipfile.ZipFile(rep,'w') as z:
                z.writestr('BTCUSDT_fixed_calendar_baseline.json',json.dumps(bad))
            with self.assertRaisesRegex(ValueError,'SHA256 mismatch'):
                a.run(hist,[rep],['BTCUSDT'],root/'out2')

    def test_summarize_does_not_treat_none_as_zero(self):
        self.assertIsNone(a.summarize([])['win_rate_pct'])
        self.assertEqual(a.summarize([a.audit_trade(trade(),hourly())])['same_hour_stops'],1)


if __name__=='__main__':unittest.main()