"""Isolated, deterministic tests for multi-asset Binance research. No API access."""
import csv
import json
import tempfile
import types
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

import multi_asset_history as multi


class FakePublicBinance:
    def __init__(self):
        self.paths = []

    def get_json(self, base, path, params=None):
        self.paths.append((base, path))
        if path == "/api/v3/time":
            return {"serverTime": 1_800_000_000_000}
        if path == "/api/v3/exchangeInfo":
            def pair(base, **opts):
                return {**{"symbol": base + "USDT", "baseAsset": base,
                        "quoteAsset": "USDT", "status": "TRADING",
                        "isSpotTradingAllowed": True}, **opts}
            return {"symbols": [pair("BTC"), pair("ETH"), pair("SOL"), pair("DOGE"),
                                 pair("USDC"), pair("BTCUP"), pair("ADA", status="BREAK"),
                                 pair("LTC", isSpotTradingAllowed=False),
                                 {"symbol": "XRPEUR", "baseAsset": "XRP", "quoteAsset": "EUR", "status": "TRADING"}]}
        if path == "/api/v3/ticker/24hr":
            return [{"symbol": "BTCUSDT", "quoteVolume": "100000000"},
                    {"symbol": "ETHUSDT", "quoteVolume": "90000000"},
                    {"symbol": "SOLUSDT", "quoteVolume": "120000000"},
                    {"symbol": "DOGEUSDT", "quoteVolume": "700000"},
                    {"symbol": "USDCUSDT", "quoteVolume": "100000000000"},
                    {"symbol": "BTCUPUSDT", "quoteVolume": "900000000"},
                    {"symbol": "ADAUSDT", "quoteVolume": "300000000"},
                    {"symbol": "LTCUSDT", "quoteVolume": "40000000"}]
        raise AssertionError(path)


class MultiAssetHistoryTests(unittest.TestCase):
    def test_top_quote_volume_spot_usdt_excludes_stable_leverage_and_inactive(self):
        c = FakePublicBinance()
        u = multi.select_universe(c, explicit=None, top=5)
        self.assertEqual([x['symbol'] for x in u['symbols']], ['SOLUSDT', 'BTCUSDT', 'ETHUSDT'])
        self.assertEqual(u['selected_at_server_ms'], 1_800_000_000_000)
        self.assertIn('hindsight', u['selection_bias_warning'])
        self.assertEqual(c.paths[0][0], multi.SPOT_PUBLIC_DATA_BASE)

    def test_explicit_list_validated_against_live_exchangeinfo(self):
        u = multi.select_universe(FakePublicBinance(), explicit='ETHUSDT,BTCUSDT')
        self.assertEqual([x['symbol'] for x in u['symbols']], ['ETHUSDT', 'BTCUSDT'])
        self.assertTrue(all(x['quote_volume_24h_usdt'] is None for x in u['symbols']))
        with self.assertRaises(ValueError):
            multi.select_universe(FakePublicBinance(), explicit='BTCUSDT,ADAUSDT')
        with self.assertRaises(ValueError):
            multi.select_universe(FakePublicBinance(), explicit='BTCUSDT,BTCUSDT')
        with self.assertRaises(ValueError):
            multi.select_universe(FakePublicBinance(), explicit='BTCUSDT,ETHEUR')

    def test_unexpected_api_shapes_fail_closed(self):
        class Bad(FakePublicBinance):
            def get_json(self, base, path, params=None):
                if path == '/api/v3/time': return {'serverTime':'not a number'}
                return super().get_json(base, path, params)
        with self.assertRaises(ValueError):
            multi.select_universe(Bad(), explicit=None)
        with self.assertRaises(ValueError):
            multi.select_universe(FakePublicBinance(), explicit=None, top=0)
        with self.assertRaises(ValueError):
            multi.select_universe(FakePublicBinance(), explicit=None, minimum_24h_usdt=float('nan'))

    def test_csv_audit_and_gap_detection(self):
        step = multi.INTERVAL_MS['1h']
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'ETHUSDT_1h.csv'
            rows = [{'symbol':'ETHUSDT','interval':'1h','open_time':str(i*step),
                     'close_time':str((i+1)*step-1), 'is_closed':'true', 'source':'mock', 'open':'100', 'high':'110', 'low':'90', 'close':'105', 'volume':'1'} for i in [0,1,3]]
            with path.open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
            audit = multi.scan_csv(path,'ETHUSDT','1h')
            self.assertEqual((audit['bars'],audit['gap_segments'],audit['missing_bars_in_observed_range']), (3,1,1))
            self.assertEqual(len(audit['csv_sha256']),64)
            self.assertTrue(audit['first_observed_is_not_proven_listing_time'])
            rows[2]['open_time']=rows[1]['open_time']
            with path.open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
            with self.assertRaises(ValueError):multi.scan_csv(path,'ETHUSDT','1h')

    def test_closed_only_and_interval_validation(self):
        step = multi.INTERVAL_MS['4h']
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'BTCUSDT_4h.csv'
            row={'symbol':'BTCUSDT','interval':'4h','open_time':'0','close_time':str(step-1),'is_closed':'false','source':'mock','open':'100','high':'110','low':'90','close':'105','volume':'1'}
            with path.open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(row));writer.writeheader();writer.writerow(row)
            with self.assertRaises(ValueError):multi.scan_csv(path,'BTCUSDT','4h')
            row['is_closed']='true';row['close_time']='123'
            with path.open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(row));writer.writeheader();writer.writerow(row)
            with self.assertRaises(ValueError):multi.scan_csv(path,'BTCUSDT','4h')

    def test_corrupt_ohlcv_fail_closed(self):
        step = multi.INTERVAL_MS['1d']
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'ETHUSDT_1d.csv'
            base={'symbol':'ETHUSDT','interval':'1d','open_time':'0',
                  'close_time':str(step-1),'is_closed':'true','source':'mock',
                  'open':'100','high':'110','low':'90','close':'105','volume':'1'}
            for field,value in [('high','99'),('low','101'),('volume','nan'),('volume','-1')]:
                bad=dict(base);bad[field]=value
                with path.open('w', newline='') as stream:
                    writer=csv.DictWriter(stream,fieldnames=list(base));writer.writeheader();writer.writerow(bad)
                with self.subTest(field=field,value=value):
                    with self.assertRaises(ValueError):multi.scan_csv(path,'ETHUSDT','1d')

    def test_selection_manifest_is_deterministic_and_atomic(self):
        data=multi.select_universe(FakePublicBinance(),explicit='SOLUSDT')
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'manifest.json';multi.write_json(path,data)
            self.assertEqual(json.loads(path.read_text()),data)
            self.assertFalse(path.with_suffix('.tmp').exists())

    def test_download_universe_multi_symbol_and_partial_failure(self):
        step_by_interval = multi.INTERVAL_MS
        def fake_download(symbol, interval, path, *, start, end, client):
            if symbol == 'SOLUSDT' and interval == '4h':
                raise ValueError('synthetic missing pair interval')
            file = path / f'{symbol}_{interval}.csv'
            file.parent.mkdir(parents=True, exist_ok=True)
            step = step_by_interval[interval]
            row = {'symbol':symbol,'interval':interval,'open_time':'0',
                   'close_time':str(step-1),'is_closed':'true','source':'test',
                   'open':'100','high':'110','low':'90','close':'105','volume':'10'}
            with file.open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=list(row));writer.writeheader();writer.writerow(row)
            return {'downloaded_count':1}
        stub=types.ModuleType('historical_backtest')
        stub.download_to_csv=fake_download
        u=multi.select_universe(FakePublicBinance(),explicit='BTCUSDT,SOLUSDT')
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(sys.modules,{'historical_backtest':stub}):
                report, failures=multi.download_universe(Path(d),u,start='2017-08-17',end=None,client=object())
            self.assertEqual(failures,1)
            self.assertEqual(report['status'],'partial_failure')
            self.assertEqual(report['symbols']['BTCUSDT']['status'],'limited_history_under_365_days')
            self.assertEqual(report['symbols']['SOLUSDT']['status'],'download_or_integrity_failure')
            self.assertEqual(report['symbols']['BTCUSDT']['intervals']['1h']['bars'],1)

    def test_malformed_universe_manifest_rejected_before_download(self):
        stub=types.ModuleType('historical_backtest')
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(sys.modules,{'historical_backtest':stub}):
                for bad in [['../BTCUSDT'],['BTCUSDT','BTCUSDT']]:
                    universe={'symbols':[{'symbol':x} for x in bad]}
                    with self.assertRaises(ValueError):
                        multi.download_universe(Path(d),universe,start=None,end=None,client=object())

    def test_no_unqualified_symbol_in_universe(self):
        for bad in ['BTCEUR','ETHUSDC','BTCUPUSDT','', 'BTCUSDT,,ETHUSDT']:
            if bad in ('BTCUPUSDT',):
                with self.assertRaises(ValueError):multi.select_universe(FakePublicBinance(),explicit=bad)
            else:
                with self.assertRaises(ValueError):multi.validate_symbols(bad)

if __name__=='__main__': unittest.main()