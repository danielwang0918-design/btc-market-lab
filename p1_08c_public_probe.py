#!/usr/bin/env python3
"""Four bounded unauthenticated Spot market-data GETs; cost feasibility only.

No credentials, configurable hosts, order endpoints, continuous polling or
signal production. Failed requests are evidence of unavailability, not quotes.
"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from p1_08c_registry import number,load_json,sha_bytes,system_ms,utc

MAX_BYTES=262_144
ENDPOINTS={
    'time':'/api/v3/time',
    'bookTicker':'/api/v3/ticker/bookTicker?symbol=BTCUSDT',
    'depth':'/api/v3/depth?symbol=BTCUSDT&limit=20',
    'trades':'/api/v3/trades?symbol=BTCUSDT&limit=5',
}
BASE='https://data-api.binance.vision'


def metrics(name,p):
    if name=='time':
        if type(p.get('serverTime')) is not int:raise ValueError('No server clock')
        return dict(server_time_ms=p['serverTime'],clock_offset_verified=False)
    if name=='bookTicker':
        if p.get('symbol')!='BTCUSDT':raise ValueError('Wrong symbol')
        bid,ask,bq,aq=[number(p[k]) for k in ('bidPrice','askPrice','bidQty','askQty')]
        if not 0<bid<=ask or min(bq,aq)<0:raise ValueError('Invalid quote')
        return dict(best_bid=float(bid),best_ask=float(ask),spread_bps=float((ask-bid)/((ask+bid)/2)*10000),
                    bid_qty=float(bq),ask_qty=float(aq),realized_slippage_bps=None)
    if name=='depth':
        if type(p.get('lastUpdateId')) is not int:raise ValueError('No depth sequence')
        bid=[(number(a),number(b)) for a,b in p['bids']];ask=[(number(a),number(b)) for a,b in p['asks']]
        if not bid or not ask or max(len(bid),len(ask))>20 or any(a<=0 or b<0 for a,b in bid+ask):raise ValueError('Invalid depth')
        if [a for a,b in bid]!=sorted({a for a,b in bid},reverse=True) or [a for a,b in ask]!=sorted({a for a,b in ask}):raise ValueError('Unordered/duplicate depth')
        if bid[0][0]>ask[0][0]:raise ValueError('Crossed book')
        return dict(last_update_id=p['lastUpdateId'],bid_levels=len(bid),ask_levels=len(ask),
            visible_bid_notional_USDT=float(sum(a*b for a,b in bid)),visible_ask_notional_USDT=float(sum(a*b for a,b in ask)),
            realized_slippage_bps=None,full_book=False)
    if not isinstance(p,list) or not 1<=len(p)<=5:raise ValueError('Invalid recent trades')
    if any(not isinstance(r,dict) or type(r.get('time')) is not int or number(r['price'])<=0 or number(r['qty'])<0 for r in p):raise ValueError('Invalid trade rows')
    return dict(observed_recent_trade_rows=len(p),first_trade_ms=min(r['time'] for r in p),last_trade_ms=max(r['time'] for r in p),
                realized_slippage_bps=None,complete_trade_history=False)


def capture(name,*,opener=None):
    if name not in ENDPOINTS:raise ValueError('Only explicitly allowlisted public GET endpoints')
    opener=opener or urlopen
    request=Request(BASE+ENDPOINTS[name],headers={'Accept':'application/json','User-Agent':'btc-market-lab-p1-08c-research'},method='GET')
    start=system_ms();tick=time.monotonic_ns();raw=b'';status=None;error=None;headers={}
    try:
        with opener(request,timeout=10) as response:
            status=response.status;headers=dict(response.headers);raw=response.read(MAX_BYTES+1)
    except HTTPError as exc:
        status=exc.code;headers=dict(exc.headers or {});raw=exc.read(MAX_BYTES+1)
    except (OSError,TimeoutError) as exc:error=type(exc).__name__
    end=system_ms();elapsed=(time.monotonic_ns()-tick)/1e6;too_big=len(raw)>MAX_BYTES
    raw=raw[:MAX_BYTES];usable=False;derived=None;payload_error=None
    if status==200 and not too_big:
        try:derived=metrics(name,load_json(raw));usable=True
        except (ValueError,KeyError,TypeError,AttributeError) as exc:payload_error=type(exc).__name__
    if too_big:payload_error='ResponseSizeLimit'
    return dict(kind='public_cost_feasibility_probe',endpoint=name,url=request.full_url,http_method='GET',
        request_started_at=utc(start),system_received_at=utc(end),elapsed_ms=elapsed,http_status=status,
        transport_error=error,payload_error=payload_error,response_truncated=too_big,
        http_payload_sha256=sha_bytes(raw),http_payload_utf8=raw.decode('utf-8',errors='replace'),http_headers=headers,
        market_event_at=None,source_published_at=None,
        source_clock_note='REST book/depth omit exchange event timestamp; HTTP Date and server clock are not publication times',
        usable_market_payload=usable,metrics=derived,historical_coverage_verified=False,
        trading_signal_input=False,independent_forward_evidence_claim=False,api_key_used=False,account_authorization_used=False)


def run(*,opener=None):
    rows=[]
    for name in ENDPOINTS:
        row=capture(name,opener=opener);rows.append(row)
        if row['http_status'] in (418,429):break  # Do not retry or continue through rate-limit bans.
    return rows


def cli(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);a=p.parse_args(argv)
    rows=run()
    with a.out.open('x',encoding='utf-8') as f:json.dump(dict(scope='ONE_OFF_COST_FEASIBILITY_ONLY',requests=rows),f,ensure_ascii=False,indent=2,allow_nan=False);f.write('\n')
    print(json.dumps([dict(endpoint=r['endpoint'],http_status=r['http_status'],usable=r['usable_market_payload']) for r in rows]));return 0


if __name__=='__main__':raise SystemExit(cli())
