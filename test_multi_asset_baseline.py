"""Unit tests for multi-asset fixed calendar and provenance guards (network-free)."""
from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import multi_asset_baseline as m


def ms(date: str) -> int:
    return int(datetime.strptime(date, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp() * 1000)


def freeze(root: Path, selections: dict[str, list[int]]) -> tuple[Path, Path]:
    data_dir = root / 'data'
    data_dir.mkdir()
    meta = {}
    for symbol, times in selections.items():
        intervals = {}
        for interval in ('1d', '4h', '1h'):
            path = data_dir / f'{symbol}_{interval}.csv'
            if interval == '1h':
                with path.open('w', encoding='utf-8', newline='') as f:
                    w = csv.DictWriter(f, fieldnames=['symbol', 'interval', 'open_time', 'is_closed'])
                    w.writeheader()
                    for t in times:
                        w.writerow({'symbol': symbol, 'interval': interval, 'open_time': str(t), 'is_closed': 'true'})
            else:
                path.write_text('frozen-but-engine-mocked\n', encoding='utf-8')
            intervals[interval] = {'bars': len(times) if interval == '1h' else 10,
                                   'csv_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        meta[symbol] = {'status': 'complete', 'intervals': intervals}
    universe = {'version': 'multi-asset-history-v1', 'selection_mode': 'top_24h_spot_usdt_volume',
                'selected_at_utc': '2026-11-16T06:01:45+00:00',
                'selected_at_server_ms': ms('2026-11-16') + 6 * 3_600_000,
                'symbols': [{'symbol': s} for s in selections]}
    coverage = {'version': 'multi-asset-history-v1', 'status': 'complete',
                'universe': universe, 'symbols': meta}
    target = data_dir / 'multi_asset_coverage.json'
    target.write_text(json.dumps(coverage), encoding='utf-8')
    return data_dir, target


class FakeEngine:
    def __init__(self, times):
        self.times = times
        self.calls = []
        self.options = None

    def _prepare_snapshots(self, data_dir, symbol, **kwargs):
        self.options = kwargs
        return ([{'open_time': str(t)} for t in self.times],
                [dict(candidate=None) for _ in self.times], {'test_fixture': True})

    def _simulate_slice(self, rows, snapshots, a, b, **options):
        self.calls.append((a, b, options))
        assert a < b
        entry = m._utc(int(rows[a]['open_time']))
        return [{'setup_id': f'dummy-{a}', 'direction': 'long', 'entry_time_utc': entry,
                 'exit_time_utc': m._utc(int(rows[a]['open_time']) + 3_600_000),
                 'signal_time_utc': entry, 'exit_reason': 'target', 'r_multiple': '1.5', 'bars_held': 1}]


class BaselineTests(unittest.TestCase):
    def test_fixed_calendar_not_symbol_fraction_splits(self):
        ts = [ms('2022-01-01'), ms('2022-12-31'), ms('2023-05-01'),
              ms('2024-05-01'), ms('2025-04-01'), ms('2026-04-01')]
        b = m._interval_bounds(ts, ms('2023-01-01'), ms('2025-01-01'), ms('2026-11-16'))
        self.assertEqual(b, {'train': (0, 2), 'validation': (2, 4), 'test': (4, 6)})

    def test_late_listings_are_preserved_as_excluded(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data, coverage = freeze(root, {'ASTERUSDT': [ms('2025-10-06'), ms('2026-01-01')]})
            result = m.run_symbol(data, coverage, 'ASTERUSDT', root / 'out', minimum_hourly=1)
            self.assertEqual(result['status'], 'excluded_insufficient_calendar_coverage')
            self.assertEqual(result['hourly_candles_by_split'], {'train': 0, 'validation': 0, 'test': 2})
            self.assertFalse(result['splits'])
            self.assertTrue((root / 'out' / 'ASTERUSDT_fixed_calendar_baseline.json').exists())

    def test_same_fixed_formal_parameters_to_all_calendar_slices(self):
        times = [ms('2022-12-31'), ms('2023-01-01'), ms('2025-01-01')]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data, coverage = freeze(root, {'BTCUSDT': times})
            engine = FakeEngine(times)
            r = m.run_symbol(data, coverage, 'BTCUSDT', root / 'out', engine=engine, minimum_hourly=1)
            self.assertEqual(r['status'], 'complete')
            self.assertEqual([(a, b) for a, b, _ in engine.calls], [(0, 1), (1, 2), (2, 3)])
            self.assertEqual(r['splits']['test']['metrics']['trade_count'], 1)
            self.assertEqual(r['splits']['test']['metrics']['wins'], 1)
            self.assertEqual(engine.options['minimum_net_rr'], 1.5)
            self.assertEqual(engine.options['max_setup_age_hours'], 168)
            for _, _, opts in engine.calls:
                self.assertEqual(opts['fee_bps'], 10)
                self.assertEqual(opts['slippage_bps'], 5)
            self.assertIn('multi_asset_coverage.json', r['input_sha256'])

    def test_wrong_sha_or_universe_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data, coverage = freeze(root, {'BTCUSDT': [ms('2022-01-01')]})
            (data / 'BTCUSDT_1h.csv').write_text('tampered', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'SHA256'):
                m.run_symbol(data, coverage, 'BTCUSDT', root / 'out')
            with self.assertRaisesRegex(ValueError, 'not in frozen'):
                m.run_symbol(data, coverage, 'DOGEUSDT', root / 'out')

    def test_utc_day_closes_asof_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            data, coverage = freeze(Path(folder), {'BTCUSDT': [ms('2022-01-01')]})
            _, _, last = m._manifest(json.loads(coverage.read_text()))
            self.assertEqual(m._utc(last), '2026-11-16T00:00:00+00:00')

    def test_wilson_interval_and_zero_trade_flags(self):
        self.assertIsNone(m._wilson(0, 0))
        self.assertEqual(m._metrics([])['win_rate_percent'], None)
        self.assertEqual(m._metrics([{'r_multiple': '1.5'}])['wins'], 1)
        self.assertGreater(m._wilson(1, 1)[0], 0)
        self.assertLess(m._wilson(1, 1)[0], 100)

    def test_aggregate_records_missing_outputs_instead_of_silent_drop(self):
        times = [ms('2022-12-31'), ms('2023-01-01'), ms('2025-01-01')]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data, coverage = freeze(root, {'BTCUSDT': times, 'ETHUSDT': times})
            m.run_symbol(data, coverage, 'BTCUSDT', root / 'individual', engine=FakeEngine(times), minimum_hourly=1)
            a = m.aggregate(coverage, root / 'individual', root / 'summary')
            self.assertEqual(a['status'], 'partial_failure')
            self.assertEqual(a['missing_or_failed_symbols'], ['ETHUSDT'])
            self.assertEqual(a['pooled_observational']['test']['trade_count'], 1)
            self.assertEqual(a['overlapping_test_entry_days']['utc_days_with_two_or_more_symbols'], 0)
            self.assertTrue((root / 'summary' / 'multi_asset_fixed_calendar_baseline.md').exists())

    def test_aggregate_clusters_same_day_and_does_not_equal_portfolio(self):
        ts = [ms('2022-12-31'), ms('2023-01-01'), ms('2025-01-01')]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data, coverage = freeze(root, {'BTCUSDT': ts, 'ETHUSDT': ts})
            for s in ('BTCUSDT', 'ETHUSDT'):
                m.run_symbol(data, coverage, s, root / 'individual', engine=FakeEngine(ts), minimum_hourly=1)
            a = m.aggregate(coverage, root / 'individual', root / 'summary')
            self.assertEqual(a['status'], 'complete')
            self.assertEqual(a['pooled_observational']['test']['trade_count'], 2)
            self.assertEqual(a['overlapping_test_entry_days']['maximum_distinct_symbols_entering_same_utc_day'], 2)
            self.assertTrue(any('NOT a portfolio' in text for text in a['limitations']))

    def test_bad_or_duplicate_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            data, coverage = freeze(Path(folder), {'BTCUSDT': [ms('2022-01-01')]})
            r = json.loads(coverage.read_text())
            r['universe']['symbols'] = [{'symbol': 'BTCUSDT'}, {'symbol': 'BTCUSDT'}]
            with self.assertRaisesRegex(ValueError, 'duplicated'):
                m._manifest(r)
            r['universe']['symbols'] = [{'symbol': '../../etc'}]
            with self.assertRaises(ValueError):
                m._manifest(r)


if __name__ == '__main__':
    unittest.main()