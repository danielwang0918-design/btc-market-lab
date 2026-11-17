"""Synthetic evidence checks; the separate live drill must really kill a process."""
import unittest
import p1_08e_drill as d


class DrillEvidenceTests(unittest.TestCase):
    def test_no_hour_close_in_short_outage_is_not_a_fabricated_gap(self):
        r=d.outage_summary(3600001,3610000)
        self.assertEqual(r['market_closes_during_outage']['1h'],[])
        self.assertEqual(r['missed_scheduled_poll_nodes'],[])

    def test_market_close_and_minute02_collection_node_are_distinct(self):
        r=d.outage_summary(3599000,3601000)
        self.assertEqual(len(r['market_closes_during_outage']['1h']),1)
        self.assertEqual(r['missed_scheduled_poll_nodes'],[])

    def test_fake_success_without_os_death_is_rejected(self):
        with self.assertRaises(ValueError):d.validate_death(dict(process_dead=True,returncode=0,pid=123))


if __name__=='__main__':unittest.main()
