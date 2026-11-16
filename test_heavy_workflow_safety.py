"""Protect expensive historical research from PR cumulative-diff retrigger loops."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent
HEAVY = (
    "multi-asset-baseline.yml",
    "multi-asset-history.yml",
    "stop-loss-research.yml",
    "winrate-research.yml",
)


class HeavyWorkflowSafetyTests(unittest.TestCase):
    def test_heavy_workflows_not_triggered_by_all_pr_updates(self):
        for name in HEAVY:
            with self.subTest(name=name):
                body = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
                trigger = body.split("\npermissions:", 1)[0]
                self.assertIn("\n  push:\n", trigger)
                self.assertIn("\n  workflow_dispatch:", trigger)
                self.assertNotIn("\n  pull_request:", trigger)
                self.assertIn('branches: ["main", "codex/**"]', trigger)
                self.assertNotIn('".github/workflows/', trigger)
                self.assertIn("cancel-in-progress: false", body)

    def test_unit_tests_still_auto_run_on_pull_requests(self):
        body = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertIn("\n  pull_request:\n", body)
        self.assertIn("python -m unittest -v", body)

    def test_multisymbol_aggregate_produces_descriptive_robustness_evidence(self):
        body = (ROOT / ".github" / "workflows" / "multi-asset-baseline.yml").read_text(encoding="utf-8")
        step = body.index("      - name: Produce calendar-week correlated-sample robustness audit")
        publication = body.index("      - name: Publish readable strategy scoreboard")
        self.assertLess(step, publication)
        self.assertIn("python -u multi_asset_robustness.py", body)
        self.assertIn("research/multi_asset/robustness/multi_asset_robustness.json", body)
        self.assertIn("research/multi_asset/robustness/multi_asset_robustness.md", body)


if __name__ == "__main__":
    unittest.main()
