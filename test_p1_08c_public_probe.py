"""Bounded public GET feasibility, all responses below are synthetic fixtures."""
import json
import unittest
from urllib.error import HTTPError
from unittest.mock import patch
import io

import p1_08c_public_probe as probe


class Response:
    status=200
    headers={}
    def __init__(self,body):self.body=body
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def read(self,n):return self.body[:n]


class PublicProbeTests(unittest.TestCase):
    def test_only_four_bounded_public_requests_no_keys(self):
        seen=[]
        def open_it(req,timeout):
            seen.append(req);return Response(b'{}')
        rows=probe.run(opener=open_it)
        self.assertEqual(len(rows),4)
        self.assertTrue(all(r.full_url.startswith('https://data-api.binance.vision/api/v3/') for r in seen))
        self.assertTrue(all(r.get_method()=='GET' for r in seen))
        self.assertTrue(all(not any('key' in k.lower() or 'authorization' in k.lower() for k in r.headers) for r in seen))
        self.assertTrue(all(not r['trading_signal_input'] for r in rows))

    def test_http_unavailable_is_not_observed_liquidity(self):
        def fail(req,timeout):raise HTTPError(req.full_url,451,'restricted',{},io.BytesIO(b'{"msg":"unavailable"}'))
        rows=probe.run(opener=fail)
        self.assertTrue(all(r['http_status']==451 and not r['usable_market_payload'] for r in rows))
        self.assertTrue(all(r['metrics'] is None for r in rows))

    def test_transport_failure_retained_without_retry(self):
        calls=[]
        def fail(req,timeout):calls.append(req);raise OSError('no route')
        rows=probe.run(opener=fail)
        self.assertEqual(len(calls),4)
        self.assertTrue(all(r['transport_error']=='OSError' for r in rows))

    def test_valid_quote_spread_and_missing_exchange_time_are_distinct(self):
        q={'symbol':'BTCUSDT','bidPrice':'100','askPrice':'101','bidQty':'2','askQty':'3'}
        r=probe.capture('bookTicker',opener=lambda *a,**k:Response(json.dumps(q).encode()))
        self.assertTrue(r['usable_market_payload']);self.assertIsNone(r['source_published_at'])
        self.assertIsNone(r['market_event_at'])
        self.assertAlmostEqual(r['metrics']['spread_bps'],(101-100)/100.5*10000)
        self.assertEqual(r['http_payload_sha256'],probe.sha_bytes(json.dumps(q).encode()))

    def test_crossed_nonfinite_or_wrong_symbol_quote_not_usable(self):
        for changed in [{'askPrice':'99'},{'askPrice':'NaN'},{'symbol':'ETHUSDT'}]:
            q={'symbol':'BTCUSDT','bidPrice':'100','askPrice':'101','bidQty':'2','askQty':'3'};q.update(changed)
            r=probe.capture('bookTicker',opener=lambda *a,**k:Response(json.dumps(q).encode()))
            self.assertFalse(r['usable_market_payload']);self.assertIsNone(r['metrics'])

    def test_depth_not_called_precise_impact_or_historical_depth(self):
        q={'lastUpdateId':1,'bids':[['100','2']],'asks':[['101','3']]}
        r=probe.capture('depth',opener=lambda *a,**k:Response(json.dumps(q).encode()))
        self.assertTrue(r['usable_market_payload'])
        self.assertEqual(r['metrics']['visible_ask_notional_USDT'],303)
        self.assertIsNone(r['metrics']['realized_slippage_bps'])
        self.assertFalse(r['historical_coverage_verified'])

    def test_oversize_json_and_api_error_not_success(self):
        for raw in [b'bad-json',b'{"code":-1,"msg":"error"}',b'x'*(probe.MAX_BYTES+1)]:
            r=probe.capture('bookTicker',opener=lambda *a,**k:Response(raw))
            self.assertFalse(r['usable_market_payload'])

    def test_endpoint_allowlist_prevents_orders_or_arbitrary_urls(self):
        for name in ['order','account','https://api.binance.com/api/v3/order']:
            with self.subTest(name=name),self.assertRaises(ValueError):probe.capture(name)


if __name__=='__main__':unittest.main()
