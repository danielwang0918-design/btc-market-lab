"""Network-free robustness and fail-closed audit tests."""
import csv
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import multi_asset_robustness as m


def records():
    examples = [
        ('BTCUSDT','train','2022-06-01T10:00:00+00:00','-1'),
        ('ETHUSDT','train','2022-08-01T10:00:00+00:00','1.5'),
        ('BTCUSDT','validation','2023-06-01T10:00:00+00:00','-1'),
        ('ETHUSDT','validation','2024-06-01T10:00:00+00:00','1.5'),
        ('BTCUSDT','test','2025-07-01T10:00:00+00:00','-1'),
        ('ETHUSDT','test','2025-07-01T11:00:00+00:00','1.5'),
        ('BTCUSDT','test','2025-08-01T10:00:00+00:00','-1'),
        ('ETHUSDT','test','2026-01-01T10:00:00+00:00','1.5'),
    ]
    out = []
    for i, (sym,split,when,rr) in enumerate(examples):
        dt = datetime.fromisoformat(when)
        out.append({'symbol':sym,'split':split,'setup_id':f'setup_{i}',
                    'direction':'long','signal_time_utc':dt.isoformat(),
                    'entry_time_utc':when,
                    'exit_time_utc':datetime.fromtimestamp(dt.timestamp()+3600,tz=timezone.utc).isoformat(),
                    'exit_reason':'target' if float(rr)>0 else 'stop',
                    'r_multiple':rr,'bars_held':'1'})
    return out


def fixture(folder: str):
    root = Path(folder)
    trades = records()
    csv_path = root/'multi_asset_fixed_calendar_trades.csv'
    with csv_path.open('w',encoding='utf-8-sig',newline='') as f:
        writer = csv.DictWriter(f,fieldnames=m.FIELDS)
        writer.writeheader()
        writer.writerows(trades)
    per_symbol = {}
    pool = {}
    for sym in ('BTCUSDT','ETHUSDT'):
        per_symbol[sym]={'status':'complete','splits':{}}
        for split in m.SPLITS:
            subset=[r for r in trades if r['symbol']==sym and r['split']==split]
            per_symbol[sym]['splits'][split]={'trade_count':len(subset),'wins':sum(float(r['r_multiple'])>0 for r in subset)}
    for split in m.SPLITS:
        subset=[r for r in trades if r['split']==split]
        pool[split]={'trade_count':len(subset),'wins':sum(float(r['r_multiple'])>0 for r in subset)}
    agg={'version':'multi-asset-calendar-baseline-v1','report_type':'multi_asset_fixed_baseline_aggregate',
         'status':'complete','symbols_in_universe':['BTCUSDT','ETHUSDT','ASTERUSDT'],
         'complete_symbols':['BTCUSDT','ETHUSDT'],
         'excluded_short_history_symbols':['ASTERUSDT'],
         'missing_or_failed_symbols':[],
         'selection_mode':'top_24h_spot_usdt_volume',
         'universe_selection_asof_utc':'2026-11-16T06:00:00+00:00',
         'fixed_utc_bounds':{
             'train_end_exclusive':'2023-01-01T00:00:00+00:00',
             'validation_end_exclusive':'2025-01-01T00:00:00+00:00',
             'test_end_exclusive':'2026-11-16T00:00:00+00:00'},
         'by_symbol':per_symbol,'pooled_observational':pool}
    base = root/'multi_asset_fixed_calendar_baseline.json'
    base.write_text(json.dumps(agg),encoding='utf-8')
    return base,csv_path


class RobustnessTests(unittest.TestCase):
    def test_descriptive_report_per_year_cluster_and_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            agg,trades=fixture(directory)
            result=m.build_report(agg,trades,Path(directory)/'out',replicas=200)
            test=result['split_summaries']['test']
            self.assertEqual(test['pooled']['trades'],4)
            self.assertEqual(test['pooled']['wins'],2)
            self.assertEqual(test['same_week_overlap']['weeks_with_multi_asset_entries'],1)
            self.assertEqual(test['per_entry_year']['2025']['trades'],3)
            self.assertEqual(test['per_entry_year']['2026']['trades'],1)
            self.assertEqual(test['maximum_single_symbol_trade_share_pct'],50)
            self.assertEqual(test['symbol_concentration_proxy_1_over_HHI'],2)
            self.assertEqual(test['leave_one_symbol_out']['BTCUSDT']['expectancy_r'],1.5)
            self.assertEqual(test['leave_one_symbol_out']['ETHUSDT']['expectancy_r'],-1)
            self.assertEqual(test['calendar_week_cluster_bootstrap']['replicates'],200)
            self.assertFalse(result['claim_of_positive_strategy_edge'])
            self.assertTrue((Path(directory)/'out'/'multi_asset_robustness.md').exists())

    def test_seed_is_deterministic_and_zero_trade_replicas_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            agg,trades=fixture(directory)
            a=m.build_report(agg,trades,Path(directory)/'a',replicas=200)
            b=m.build_report(agg,trades,Path(directory)/'b',replicas=200)
            self.assertEqual(a['split_summaries']['test']['calendar_week_cluster_bootstrap'],
                             b['split_summaries']['test']['calendar_week_cluster_bootstrap'])
        first=datetime.fromisoformat('2025-01-01T00:00:00+00:00')
        last=datetime.fromisoformat('2025-03-01T00:00:00+00:00')
        self.assertIsNone(m._weekly_cluster([],first,last,replicas=100)['expectancy_r_95pct_descriptive'])

    def test_partial_results_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            agg,trades=fixture(directory)
            r=json.loads(agg.read_text());r['status']='partial_failure';r['missing_or_failed_symbols']=['XRPUSDT']
            agg.write_text(json.dumps(r))
            with self.assertRaisesRegex(ValueError,'incomplete'):
                m.build_report(agg,trades,Path(directory)/'out',replicas=100)

    def test_mismatch_pooled_count_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            agg,trades=fixture(directory)
            r=json.loads(agg.read_text());r['pooled_observational']['test']['trade_count']=999
            agg.write_text(json.dumps(r))
            with self.assertRaisesRegex(ValueError,'pooled count'):
                m.build_report(agg,trades,Path(directory)/'out',replicas=100)

    def test_mismatch_symbol_wins_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            agg,trades=fixture(directory)
            r=json.loads(agg.read_text());r['by_symbol']['ETHUSDT']['splits']['test']['wins']=0
            agg.write_text(json.dumps(r))
            with self.assertRaisesRegex(ValueError,'per-symbol count'):
                m.build_report(agg,trades,Path(directory)/'out',replicas=100)

    def test_duplicate_trade_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            agg,trades=fixture(directory)
            with trades.open('a',encoding='utf-8') as f:
                r=records()[0]
                f.write(','.join(r[k] for k in m.FIELDS)+'\n')
            with self.assertRaisesRegex(ValueError,'Duplicate setup'):
                m.build_report(agg,trades,Path(directory)/'out',replicas=100)

    def test_incorrect_split_date_and_unaware_time_rejected(self):
        for wrong in ('2024-12-31T10:00:00+00:00','2025-07-01T10:00:00'):
            with self.subTest(wrong=wrong),tempfile.TemporaryDirectory() as directory:
                agg,trades=fixture(directory)
                content=trades.read_text(encoding='utf-8-sig').replace('2025-07-01T10:00:00+00:00',wrong)
                trades.write_text(content,encoding='utf-8-sig')
                with self.assertRaises(ValueError):
                    m.build_report(agg,trades,Path(directory)/'out',replicas=100)

    def test_nonfinite_returns_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            agg,trades=fixture(directory)
            trades.write_text(trades.read_text(encoding='utf-8-sig').replace(',-1,1\n',',nan,1\n'),encoding='utf-8-sig')
            with self.assertRaisesRegex(ValueError,'Non-finite'):
                m.build_report(agg,trades,Path(directory)/'out',replicas=100)

    def test_bootstrap_rejects_invalid_replicas(self):
        start=datetime.fromisoformat('2025-01-01T00:00:00+00:00')
        end=datetime.fromisoformat('2026-01-01T00:00:00+00:00')
        for n in (0,99,10001,True):
            with self.subTest(n=n),self.assertRaises(ValueError):
                m._weekly_cluster([],start,end,replicas=n)


if __name__=='__main__':
    unittest.main()