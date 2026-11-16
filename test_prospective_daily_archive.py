"""P0-08 offline archive tests. No Binance requests, no orders."""
from __future__ import annotations

import csv
import io
import json
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from zipfile import ZipFile, ZIP_DEFLATED

import prospective_daily_archive as archive
import prospective_validation as gate

FIELDS=['symbol','interval','open_time','open_time_utc','open','high','low','close','volume',
        'close_time','close_time_utc','exchange_close_time','exchange_close_time_utc','is_closed','source']
T0=gate.parse_utc('2026-11-16T00:00:00+00:00')
START=gate.parse_utc(gate.START)


def candle(symbol, interval, op):
    step=gate.MS_PER[interval]
    return {'symbol':symbol,'interval':interval,'open_time':op,'open_time_utc':gate.utc(op),
            'open':'100','high':'104','low':'98','close':'102','volume':'2',
            'close_time':op+step-1,'close_time_utc':gate.utc(op+step-1),
            'exchange_close_time':op+step-1,'exchange_close_time_utc':gate.utc(op+step-1),
            'is_closed':'true','source':gate.SOURCE}


def csv_bytes(bars):
    with io.StringIO(newline='') as f:
        writer=csv.DictWriter(f,fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(bars)
        return b'\xef\xbb\xbf'+f.getvalue().encode('utf-8')


class DailyArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.lock={'symbols':['BTCUSDT'],'frozen_data_prefixes':{}}
        self.frozen={}
        for interval in ('1h','4h','1d'):
            name=f'BTCUSDT_{interval}.csv'
            step=gate.MS_PER[interval]
            raw=csv_bytes([candle('BTCUSDT',interval,START-step)])
            self.frozen[name]=raw
            self.lock['frozen_data_prefixes'][name]={'symbol':'BTCUSDT','interval':interval,
                    'bytes':len(raw),'sha256':gate.sha_bytes(raw),'frozen_bars':1,
                    'frozen_last_close_exclusive_ms':START}
        self.seed=self.root/'seed.zip'
        with ZipFile(self.seed,'w',ZIP_DEFLATED) as z:
            for name,data in self.frozen.items():z.writestr(name,data)
        self.genesis={'version':gate.VERSION,'lock_sha256':gate.LOCK_SHA256,
            'asof_utc':gate.utc(START-gate.MS_PER['1h']),
            'previous_record_sha256':None,
            'files':{name:{'full_bytes':len(raw),'full_sha256':gate.sha_bytes(raw)}
                     for name,raw in self.frozen.items()},
            'result':{'status':'preregistered_not_started'}}
        self.genesis['record_sha256']=gate.record_hash(self.genesis)
        self.genesis_path=self.root/'genesis.json';gate.dump_json(self.genesis_path,self.genesis)
        self.dir=self.root/'archive'
        self.patcher=mock.patch.object(archive,'GENESIS_RECORD_SHA256',self.genesis['record_sha256'])
        self.patcher.start();self.addCleanup(self.patcher.stop)

    def seed_it(self):
        return archive.bootstrap(self.dir,self.seed,self.genesis_path,self.lock)

    def test_bootstrap_checks_and_round_trip(self):
        result=self.seed_it()
        self.assertEqual(result['status'],'bootstrapped')
        self.assertEqual(archive.reconstruct(self.dir,self.root/'restored',self.lock)['archived_days'],0)
        for name,raw in self.frozen.items():
            self.assertEqual((self.root/'restored'/name).read_bytes(),raw)

    def test_bootstrap_refuses_any_existing_archive(self):
        self.seed_it()
        with self.assertRaisesRegex(ValueError,'reinitialize'):
            self.seed_it()

    def test_seed_missing_or_extra_entry_is_rejected(self):
        with ZipFile(self.seed,'w') as z:
            z.writestr('BTCUSDT_1h.csv',self.frozen['BTCUSDT_1h.csv'])
        with self.assertRaisesRegex(ValueError,'missing, surplus'):
            self.seed_it()

    def test_seed_hash_corruption_is_rejected(self):
        with ZipFile(self.seed,'w') as z:
            for name,raw in self.frozen.items():z.writestr(name,raw+b'X' if name.endswith('1h.csv') else raw)
        with self.assertRaisesRegex(ValueError,'immutable frozen bytes'):
            self.seed_it()

    def test_genesis_wrong_digest_rejected(self):
        self.genesis['files']['BTCUSDT_1h.csv']['full_sha256']='0'*64
        self.genesis['record_sha256']=gate.record_hash(self.genesis)
        gate.dump_json(self.genesis_path,self.genesis)
        with self.assertRaisesRegex(ValueError,'original pre-start'):
            self.seed_it()

    def test_corrupt_archive_seed_fails_closed(self):
        self.seed_it()
        with (self.dir/archive.SEED_NAME).open('ab') as f:f.write(b'changed')
        with self.assertRaisesRegex(ValueError,'frozen seed'):
            archive.reconstruct(self.dir,self.root/'restored',self.lock)

    def test_corrupt_latest_record_fails_closed(self):
        self.seed_it()
        record=gate.read_json(self.dir/archive.LAST_NAME)
        record['asof_utc']='2027-01-01T00:00:00+00:00'
        gate.dump_json(self.dir/archive.LAST_NAME,record)
        with self.assertRaisesRegex(ValueError,'latest pointer'):
            archive.reconstruct(self.dir,self.root/'restored',self.lock)

    def test_invalid_day_directory_rejected(self):
        self.seed_it()
        (self.dir/'days'/'traversal-not-a-date').mkdir()
        with self.assertRaisesRegex(ValueError,'exact YYYY-MM-DD'):
            archive.reconstruct(self.dir,self.root/'restored',self.lock)

    def test_archive_must_have_complete_delta_files(self):
        self.seed_it()
        day=self.dir/'days'/'2026-11-17';(day/'delta').mkdir(parents=True)
        gate.dump_json(day/'checkpoint.json',self.genesis)
        with self.assertRaisesRegex(ValueError,'Broken hash-chain'):
            archive.reconstruct(self.dir,self.root/'restored',self.lock)

    def test_daily_future_rejected_before_fetch(self):
        self.seed_it()
        with self.assertRaisesRegex(ValueError,'Future UTC'):
            archive.daily(self.dir,self.lock,START,fetcher=lambda *x,**k:())

    def test_daily_rejects_nonutc_midnight(self):
        self.seed_it()
        with self.assertRaisesRegex(ValueError,'UTC day'):
            archive.daily(self.dir,self.lock,START+gate.MS_PER['1h'])

    def test_daily_rejects_missing_candle_without_mutating_archive(self):
        self.seed_it()
        future=type('FutureDateTime',(datetime,),{'now':classmethod(lambda cls,tz:datetime(2026,11,19,0,0,tzinfo=timezone.utc))})
        with mock.patch.object(archive,'datetime',future), mock.patch.dict('sys.modules',{'historical_backtest':types.SimpleNamespace(COLUMNS=FIELDS)}):
            with self.assertRaisesRegex(ValueError,'missing/duplicated'):
                archive.daily(self.dir,self.lock,START+gate.MS_PER['1d'],fetcher=lambda *a,**k:[])
        self.assertEqual(list((self.dir/'days').iterdir()),[])
        self.assertEqual(gate.read_json(self.dir/'latest.json')['record_sha256'],self.genesis['record_sha256'])

    def test_daily_success_rebuild_and_no_replay(self):
        self.seed_it()
        future=type('FutureDateTime',(datetime,),{'now':classmethod(lambda cls,tz:datetime(2026,11,19,0,0,tzinfo=timezone.utc))})
        def fetch(symbol,interval,start,end,**kwargs):
            return [candle(symbol,interval,t) for t in range(start,end,gate.MS_PER[interval])]
        with mock.patch.object(archive,'datetime',future),mock.patch.dict('sys.modules',{'historical_backtest':types.SimpleNamespace(COLUMNS=FIELDS)}):
            done=archive.daily(self.dir,self.lock,START,fetcher=fetch)
        self.assertEqual(done['new_closed_bars'],0)  # frozen data ends exactly at asof in fixture
        self.assertEqual(done['daily_file_count'],3)
        verified=archive.reconstruct(self.dir,self.root/'restored',self.lock)
        self.assertEqual(verified['archived_days'],1)
        self.assertEqual(verified['last_record']['result']['status'],'daily_data_archived_not_scored')
        self.assertIsNone(verified['last_record']['result']['forward_trade_count'])
        self.assertEqual(verified['last_record']['previous_record_sha256'],self.genesis['record_sha256'])
        self.assertEqual((self.root/'restored'/'BTCUSDT_1h.csv').read_bytes(),self.frozen['BTCUSDT_1h.csv'])
        with mock.patch.object(archive,'datetime',future):
            self.assertEqual(archive.daily(self.dir,self.lock,START,fetcher=fetch)['status'],
                             'already_archived_verified')

    def test_forward_day_success_with_real_new_candles_and_tamper_guard(self):
        self.seed_it()
        future=type('FutureDateTime',(datetime,),{'now':classmethod(lambda cls,tz:datetime(2026,11,19,0,0,tzinfo=timezone.utc))})
        def fetch(symbol,interval,start,end,**kwargs):
            return [candle(symbol,interval,t) for t in range(start,end,gate.MS_PER[interval])]
        with mock.patch.object(archive,'datetime',future),mock.patch.dict('sys.modules',{'historical_backtest':types.SimpleNamespace(COLUMNS=FIELDS)}):
            done=archive.daily(self.dir,self.lock,START+gate.MS_PER['1d'],fetcher=fetch)
        self.assertEqual(done['new_closed_bars'],31)
        self.assertEqual(done['status'],'daily_data_archived_not_scored')
        restored=archive.reconstruct(self.dir,self.root/'restored',self.lock)
        self.assertEqual(restored['archived_days'],1)
        self.assertEqual(restored['last_record']['files']['BTCUSDT_1h.csv']['appended_closed_candles'],24)
        self.assertEqual(restored['last_record']['files']['BTCUSDT_4h.csv']['appended_closed_candles'],6)
        self.assertEqual(restored['last_record']['files']['BTCUSDT_1d.csv']['appended_closed_candles'],1)
        delta=(self.dir/'days'/'2026-11-18'/'delta'/'BTCUSDT_1h.csv')
        delta.write_bytes(delta.read_bytes()+b'forged price\r\n')
        with self.assertRaisesRegex(ValueError,'forward K-line delta differs'):
            archive.reconstruct(self.dir,self.root/'restored2',self.lock)

    def test_tampered_delta_detection(self):
        self.seed_it()
        # Authentic checkpoint would need a new forward candle, but a corrupt delta must fail.
        dest=self.dir/'days'/'2026-11-17';(dest/'delta').mkdir(parents=True)
        for name in self.frozen:(dest/'delta'/name).write_bytes(b'')
        doc=dict(self.genesis)
        doc.update(asof_utc='2026-11-17T00:00:00+00:00',
                   previous_record_sha256=self.genesis['record_sha256'],
                   result={'status':'daily_data_archived_not_scored'})
        doc['record_sha256']=gate.record_hash(doc)
        gate.dump_json(dest/'checkpoint.json',doc);gate.dump_json(self.dir/'latest.json',doc)
        (dest/'delta'/'BTCUSDT_1h.csv').write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError,'delta differs'):
            archive.reconstruct(self.dir,self.root/'restored',self.lock)

    def test_seed_file_defends_path_traversal(self):
        with ZipFile(self.seed,'w') as z:
            z.writestr('../BTCUSDT_1h.csv',b'')
            for name,raw in self.frozen.items():z.writestr(name,raw)
        with self.assertRaisesRegex(ValueError,'missing, surplus'):
            self.seed_it()

    def test_pre_registered_clock_range(self):
        for day in ['2026-11-16','2028-01-02','2026-2-01','../../file','2026-11-17/../18']:
            with self.subTest(day=day):
                with self.assertRaises(ValueError):archive._safe_day(day)
        self.assertEqual(archive._safe_day('2026-11-17'),'2026-11-17')


if __name__=='__main__':unittest.main()