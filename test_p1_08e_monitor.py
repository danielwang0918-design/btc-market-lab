"""Pure synthetic calendar and alert fixtures; not 7 days of real data."""
from datetime import datetime,timezone
import unittest
import p1_08e_monitor as m

H=3_600_000;D=24*H
T=int(datetime(2030,1,1,tzinfo=timezone.utc).timestamp()*1000)


class MonitorTests(unittest.TestCase):
    def test_partial_day_does_not_count_future_bars(self):
        self.assertEqual(m.expected_closes(T,T+3*H,'1h'),[T+H,T+2*H,T+3*H])
        self.assertEqual(m.expected_closes(T,T+3*H,'4h'),[])

    def test_midnight_close_belongs_to_preceding_operating_day(self):
        self.assertEqual(m.expected_closes(T,T+D,'1d'),[T+D])

    def test_seven_days_are_calendar_duration_not_request_count(self):
        s=m.seven_day_status(None,T+100*D)
        self.assertEqual(s['status'],'not_started');self.assertFalse(s['elapsed_168h'])
        self.assertFalse(m.seven_day_status(T,T+7*D-1)['elapsed_168h'])
        self.assertTrue(m.seven_day_status(T,T+7*D)['elapsed_168h'])

    def test_minimum_time_alone_does_not_pass_level2(self):
        self.assertFalse(m.seven_day_status(T,T+8*D)['level2_pass'])

    def test_duplicates_and_bootstrap_do_not_inflate_success(self):
        rows=[dict(symbol='BTCUSDT',timeframe='1h',status='new',close_exclusive_ms=T,
                   wire_received_at=m.utc(T+10)),
              dict(symbol='BTCUSDT',timeframe='1h',status='new',close_exclusive_ms=T+H,wire_received_at=m.utc(T+H+1000)),
              dict(symbol='BTCUSDT',timeframe='1h',status='duplicate',close_exclusive_ms=T+H,wire_received_at=m.utc(T+2*H))]
        r=m.feed_report(rows,'BTCUSDT','1h',T,T+2*H,T+2*H+300001)
        self.assertEqual(r['expected'],2);self.assertEqual(r['confirmed'],1);self.assertEqual(r['missing_due'],1)
        self.assertEqual(r['duplicates'],1);self.assertEqual(r['on_time_success_rate'],0.5)

    def test_grace_period_and_late_are_separate(self):
        r=m.feed_report([],'BTCUSDT','1h',T,T+H,T+H+1000)
        self.assertEqual(r['pending_grace'],1);self.assertEqual(r['missing_due'],0)
        row=dict(symbol='BTCUSDT',timeframe='1h',status='new',close_exclusive_ms=T+H,wire_received_at=m.utc(T+H+300001))
        r=m.feed_report([row],'BTCUSDT','1h',T,T+H,T+2*H)
        self.assertEqual(r['late'],1);self.assertEqual(r['on_time_success_rate'],0)

    def test_zero_denominator_is_unknown(self):
        self.assertIsNone(m.feed_report([],'BTCUSDT','1d',T,T+H,T+H)['on_time_success_rate'])

    def test_short_real_outage_does_not_invent_hourly_gaps(self):
        self.assertEqual(m.outage_closes(T+H+1,T+H+10000,'1h'),[])
        self.assertEqual(m.outage_closes(T+H-1000,T+H+1000,'1h'),[T+H])


if __name__=='__main__':unittest.main()
