"""End-to-end SYNTHETIC fixtures; no market performance evidence."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
import p1_08c_demo as demo
from p1_08c_registry import Registry,sha_file


class DemoTests(unittest.TestCase):
    def test_explicit_synthetic_eight_symbols_pending_unknown_and_results_separated(self):
        with tempfile.TemporaryDirectory() as d:
            m=demo.run(Path(d)/'out')
            self.assertTrue(m['synthetic_only']);self.assertEqual(m['real_forward_candidates'],0)
            self.assertEqual(m['qualified_strategy_count'],0);self.assertEqual(m['unique_events'],8)
            self.assertEqual(m['candidate_versions'],9);self.assertEqual(m['complete_labels'],6)
            self.assertEqual(m['incomplete_labels'],2);self.assertEqual(m['new_backtests'],0)
            root=Path(d)/'out';r=Registry(root/'synthetic_registry')
            a=json.loads((root/'registration_checkpoints.json').read_text())
            self.assertEqual(len(a),9)
            for checkpoint in a:self.assertTrue(r.verify(anchor=checkpoint)['anchor_consistent'])
            for name,digest in m['output_sha256'].items():self.assertEqual(sha_file(root/name),digest)
            self.assertFalse(m['external_timestamp_attestation_verified'])

    def test_repeated_output_directory_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'out';demo.run(root)
            with self.assertRaises(ValueError):demo.run(root)

    def test_no_strategy_or_network_calls(self):
        import p1_portfolio,historical_backtest,urllib.request
        with tempfile.TemporaryDirectory() as d,patch.object(p1_portfolio,'run_account',side_effect=AssertionError('no replay')),patch.object(urllib.request,'urlopen',side_effect=AssertionError('offline')):
            self.assertEqual(demo.run(Path(d)/'out')['new_backtests'],0)

    def test_wrong_baseline_zip_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'wrong.zip';p.write_bytes(b'not a source archive')
            with self.assertRaises(ValueError):demo.verify_baseline(p)


if __name__=='__main__':unittest.main()
