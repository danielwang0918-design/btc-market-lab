"""P1-05 finite-cash execution model. Formal P0 modules remain read-only."""
from __future__ import annotations
import bisect,csv,gzip,itertools,json,hashlib
from collections import Counter
from datetime import datetime,timezone
from decimal import Decimal as D,localcontext,ROUND_FLOOR
from pathlib import Path
import p1_candlestick_research as core
import p1_zone_context as zone
import p1_market_regime as regime
import p1_volume_rvol as vol

H=3600000
START=1602720000000
END=1794787200000
CUTS=(1672531200000,1735689600000,END)
POLICIES=('A','B','C','D','E_ob','E_2b')
MODELS=('strict_spot','fractional_spot','fractional_spot_target_first','fractional_spot_slip15','hypothetical_1x_long_short')

def ms(s):return int(datetime.fromisoformat(s.replace('Z','+00:00')).timestamp()*1000)
def utc(t):return core.utc(t)
def fill(price,side,entry=True,slip=D(5)):
    adverse=slip/D(10000)
    return price*(1+adverse if (side=='long')==entry else 1-adverse)
def net_per_unit(entry,exit_,side,slip=D(5)):
    e=fill(entry,side,True,slip);x=fill(exit_,side,False,slip)
    return (x-e if side=='long' else e-x)-(e+x)*D('.001')
def unit_risk(entry,stop,side):return -net_per_unit(entry,stop,side)

def size_order(equity,entry,stop,side,rules=None,strict=False,actual_slip=D(5)):
    if strict and rules is None:return {'status':'historical_filters_unavailable'}
    if side not in ('long','short') or min(equity,entry,stop)<=0 or not (stop<entry if side=='long' else stop>entry):return {'status':'invalid_prices_or_cash'}
    with localcontext() as ctx:
        ctx.prec=50
        risk=unit_risk(entry,stop,side);budget=equity*D('.02')
        e=fill(entry,side,True,actual_slip)
        if risk<=0:return {'status':'invalid_risk'}
        q=min(budget/risk,equity/(e*D('1.001')))
        quantum=D(rules['step']) if rules else D('1e-18')
        q=(q/quantum).to_integral_value(rounding=ROUND_FLOOR)*quantum
        if q<=0:return {'status':'quantity_zero'}
        if rules and (q<D(rules['min_qty']) or q*e<D(rules['min_notional'])):return {'status':'below_exchange_minimum'}
        return dict(status='sized',quantity=q,risk_budget=budget,planned_risk=q*risk,
                    cash_required=q*e*D('1.001'),entry_fill=e,unit_risk=risk,
                    cash_capped=q*risk<budget-D('1e-12'))

def exit_price(side,o,h,l,stop,target,priority='stop_first'):
    if side=='long':gap=o<=stop;sl=l<=stop;tp=h>=target
    else:gap=o>=stop;sl=h>=stop;tp=l<=target
    if gap:return o,'stop_gap'
    if sl and (priority=='stop_first' or not tp):return stop,'stop'
    if tp:return target,'target'
    if sl:return stop,'stop'
    return None

def legacy_replay(records):
    cash=D(1000);busy=-1;trades=[]
    for r in sorted(records,key=lambda x:(ms(x['entry_time_utc']),x['symbol'],x['setup_id'])):
        t=ms(r['entry_time_utc'])
        if t<busy or len(trades)>=1000:continue
        risk=cash*D('.02');pnl=risk*D(r['r_multiple']);before=cash;cash+=pnl
        trades.append({**r,'equity_before':before,'risk_budget':risk,'net_pnl':pnl,'equity_after':cash})
        busy=ms(r['exit_time_utc'])
    return {'final_equity':cash,'trades':trades}

class Asset:
    def __init__(self,symbol,data,tapes):
        self.symbol=symbol;self.frames={}
        lock=core.read_lock(Path(__file__).with_name('PROSPECTIVE_LOCK_V1.json'))
        for frame in core.FRAMES:
            name=f'{symbol}_{frame}.csv';raw=(data/name).read_bytes();meta=lock['frozen_data_prefixes'][name]
            if core.sha(raw)!=meta['sha256'] or len(raw)!=meta['bytes']:raise ValueError('Frozen source changed')
            self.frames[frame]=core.parse_bars(raw,symbol,frame,meta['frozen_bars'],closed_through_ms=meta['frozen_last_close_exclusive_ms'])
        self.bars=self.frames['1h'];self.times=[b.open_ms for b in self.bars];self.by_time={t:i for i,t in enumerate(self.times)}
        with gzip.open(tapes/f'{symbol}.json.gz','rt') as f:tape=json.load(f)
        if tape['open_times']!=self.times:raise ValueError('Canonical tape clock mismatch')
        if tape['canonical_code_sha256']!=hashlib.sha256(Path(__file__).with_name('historical_backtest.py').read_bytes()).hexdigest():raise ValueError('Canonical engine changed')
        for frame in core.FRAMES:
            if tape['input_sha256'][f'{symbol}_{frame}.csv']!=lock['frozen_data_prefixes'][f'{symbol}_{frame}.csv']['sha256']:raise ValueError('Canonical tape source changed')
        self.snaps=tape['snapshots'];self.starts=core.contiguous_segment_start(self.bars,H)
        self.volume=vol.VolumeSource(self.bars,'1h')
        cv=lambda bars:[regime.Candle(b.open_ms,float(b.o),float(b.h),float(b.l),float(b.c)) for b in bars]
        self.reg=regime.RegimeSource(cv(self.bars),'1h')
        self.refs=zone.Context.from_bars('4h',[zone.Bar(b.open_ms,float(b.o),float(b.h),float(b.l),float(b.c)) for b in self.frames['4h']])
        hb=[zone.Bar(b.open_ms,float(b.o),float(b.h),float(b.l),float(b.c)) for b in self.bars]
        self.pivot_lows,self.pivot_highs=zone.confirmed_pivots(hb,self.starts)
        self.cache={}

    def features(self,t,price,side):
        key=(t,str(price),side)
        if key in self.cache:return self.cache[key]
        i=bisect.bisect_left(self.times,t)-1
        out=dict(feature_cutoff_ms=t,location='reference_data_gap_or_stale',trend='insufficient_prior_ma',
                 volatility='insufficient_prior_atr_distribution',pattern_ids='',rvol=None,rvol_bin='insufficient_history',atr=None)
        if i<0 or self.times[i]+H!=t:return out
        p=i;st=self.starts[p];atr=self.reg.atr[p];out['atr']=atr
        patterns=core.confirmed_at(self.bars,p,self.starts[p]);out['pattern_ids']='|'.join(k for k,s in patterns if s==side)
        vf=self.volume.feature_at(p);out['rvol']=vf['rvol'];out['rvol_bin']=vf['rvol_bin']
        if p-20-200+1>=st and atr is not None and atr>0:
            ma=self.reg.ma(p);prev=self.reg.ma(p-20);c=float(self.bars[p].c)
            out['trend']='bullish' if c>ma and ma>prev else 'bearish' if c<ma and ma<prev else 'transition_or_range'
        if atr is not None and p-200+1>=st+14:
            hist=self.reg.atr_pct[p-199:p+1];cur=hist[-1]
            rank=(sum(x<cur for x in hist)+.5*sum(x==cur for x in hist))/200
            out['volatility']='low' if rank<=.25 else 'high' if rank>=.75 else 'normal'
        ref=self.refs;step=core.FRAMES['4h'];j=bisect.bisect_right(ref.timestamps,t-step)-1
        if atr is None or atr<=0:out['location']='insufficient_prior_atr'
        elif j<0:out['location']='no_preexisting_pivot'
        elif ref.bars[j].t+step!=(t//step)*step:pass
        else:
            piv=ref.lows if side=='long' else ref.highs
            eligible=[val for pidx,cidx,val in piv.rows if ref.bars[pidx].t>=t-180*86400000
                and pidx>=ref.starts[j] and ref.bars[cidx].t+step<=t]
            if not eligible:out['location']='no_preexisting_pivot'
            else:
                anchor=min(eligible,key=lambda v:abs(float(price)-v));near=[v for v in eligible if abs(v-anchor)<=.5*atr]
                distance=abs(float(price)-anchor)/atr
                out['location']='near_confirmed_zone' if distance<=.75 and len(near)>=2 else 'near_single_pivot_unconfirmed' if distance<=.75 else 'far_from_nearest_pivot'
        self.cache[key]=out
        return out

    def two_b_pivot(self,t,side,lo,hi):
        piv=self.pivot_lows if side=='long' else self.pivot_highs
        eligible=[(p,v) for p,c,v in piv if self.times[c]+H<=t and float(lo)<=v<=float(hi)
                  and self.starts[c]==self.starts[self.by_time[t]]]
        return D(str(eligible[-1][1])) if eligible else None

def passes(policy,features,side):
    b=features['location']=='near_confirmed_zone' and features['trend']==('bullish' if side=='long' else 'bearish')
    c=bool(features['pattern_ids']) and features['rvol'] is not None and features['rvol']>=D('1.2')
    return policy=='A' or policy=='B' and b or policy=='C' and c or policy=='D' and b and c and features['volatility']=='normal'

def run_account(policy,model,start,end,assets):
    with localcontext() as context:
        context.prec=50
        return _run_account(policy,model,start,end,assets)

def _run_account(policy,model,start,end,assets):
    cash=D(1000);position=None;pending={};consumed=set();trades=[];curve=[];logs=[];counts=Counter();peak=D(1000);maxdd=D(0);in_market=0
    slip=D(15) if model.endswith('slip15') else D(5);priority='target_first' if model.endswith('target_first') else 'stop_first'
    phase_start=start;last_marks={};max_positions=0;unpriced_hours=0
    def log(t,sym,signal,event,**extra):
        logs.append(dict(time_ms=t,time_utc=utc(t),symbol=sym,setup_id=signal.get('setup_id',''),event=event,
            direction=signal.get('side',''),entry_low=signal.get('zone_low',''),entry_high=signal.get('zone_high',''),
            entry_reference=signal.get('entry_reference',''),SL=signal.get('stop',''),TP1=signal.get('target',''),TP2='',
            TP2_status='not_defined_by_original_strategy',net_RR=signal.get('net_rr',''),
            basis='BOS_OB_'+policy,action='offline_simulated_only' if event=='entered' else 'watch',
            invalidation='OB_stop_target_age168h_data_gap_or_required_confirmation_failed',**extra));counts[event]+=1
    def close(t,raw,reason):
        nonlocal cash,position
        p=position;q=p['quantity'];side=p['side'];x=fill(raw,side,False,slip);exitfee=q*x*D('.001')
        cash+=(q*x-exitfee) if side=='long' else q*(p['entry_fill']-x)-exitfee
        net=cash-p['equity_before'];planned=p['planned_risk']
        rawgross=q*(raw-p['raw_entry'])*(1 if side=='long' else -1)
        fee=p['entry_fee']+exitfee;slippage=rawgross-(net+fee)
        trades.append({**p,'exit_time_ms':t,'exit_time_utc':utc(t),'raw_exit':raw,'exit_fill':x,'exit_reason':reason,
            'exit_fee':exitfee,'fees_usdt':fee,'slippage_usdt':slippage,'net_pnl_usdt':net,'r_multiple':net/planned,
            'equity_after':cash,'loss_exceeds_budget':-net>p['risk_budget']+D('1e-9'),
            'loss_exceeds_planned_risk':-net>planned+D('1e-9')})
        log(t,p['symbol'],p,'position_closed',exit_reason=reason,net_pnl_usdt=net,equity_after=cash)
        position=None
    for t in range(start,end,H):
        was_held=position is not None;changed=False;offers=[]
        # Quotes at current hour are execution data only; feature inputs stop at t.
        for sym,a in sorted(assets.items()):
            i=a.by_time.get(t)
            if i is None:continue
            bar=a.bars[i];last_marks[sym]=bar.c
            p=pending.get(sym)
            if not p:continue
            if i and a.times[i]-a.times[i-1]!=H:
                log(t,sym,p,'pending_data_gap_cancel');pending.pop(sym);continue
            side=p['side'];stop=D(p['stop']);target=D(p['target']);lo=D(p['zone_low']);hi=D(p['zone_high'])
            age=(t-int(p['setup_confirm_ms']))/H
            if age>168 or not p.get('market_at') and t-p.get('first_touch',t)>=3*H and p.get('first_touch') is not None:
                log(t,sym,p,'expired');pending.pop(sym);continue
            if p.get('market_at')==t:
                raw=bar.o
                if not (stop<raw<target if side=='long' else target<raw<stop) or net_per_unit(raw,target,side)/unit_risk(raw,stop,side)<D('1.5'):
                    log(t,sym,p,'E_invalid_open_or_RR');pending.pop(sym);continue
                offers.append((sym,p,raw,stop,target,a.features(t,raw,side)));pending.pop(sym);continue
            touch=bar.l<=hi and bar.h>=lo
            target_hit=bar.h>=target if side=='long' else bar.l<=target
            stop_gap=bar.o<=stop if side=='long' else bar.o>=stop
            if target_hit or stop_gap:
                log(t,sym,p,'target_zone_order_unknown_cancel' if target_hit and touch else 'invalid_before_entry');pending.pop(sym);continue
            if policy.startswith('E'):
                if bar.v==0 and touch:
                    log(t,sym,p,'E_zero_volume_touch_unavailable');pending.pop(sym);continue
                if touch and 'first_touch' not in p:
                    p['first_touch']=t;p['pivot']=a.two_b_pivot(t,side,lo,hi)
                if 'first_touch' not in p:continue
                if (bar.l<=stop if side=='long' else bar.h>=stop):log(t,sym,p,'E_stop_invalidated');pending.pop(sym);continue
                f=a.features(t,D(p['entry_reference']),side)
                good=(bar.c>bar.o and bar.c>hi) if side=='long' else (bar.c<bar.o and bar.c<lo)
                if policy=='E_2b':
                    level=p.get('pivot');good=level is not None and ((bar.l<level<bar.c and bar.c>bar.o) if side=='long' else (bar.h>level>bar.c and bar.c<bar.o))
                    if good:
                        if not f['atr']:good=False
                        else:p['stop']=str(min(stop,bar.l)-D('.1')*D(str(f['atr'])) if side=='long' else max(stop,bar.h)+D('.1')*D(str(f['atr'])))
                if good:
                    p['market_at']=t+H;log(t+H,sym,p,'E_closed_confirmation',confirmation_open_ms=t)
                continue
            if touch:
                raw=D(p['entry_reference']);pending.pop(sym)
                if not bar.l<=raw<=bar.h:log(t,sym,p,'reference_outside_bar');continue
                f=a.features(t,raw,side)
                if not passes(policy,f,side):log(t,sym,p,'research_filter_rejected',**f);continue
                offers.append((sym,p,raw,stop,target,f))
        # Hold a global position for its entire 1H execution interval, including exit hour.
        for sym,p,raw,stop,target,f in sorted(offers,key=lambda x:(x[0],x[1]['setup_id'])):
            a=assets[sym];bar=a.bars[a.by_time[t]]
            if position is not None or was_held:log(t,sym,p,'global_position_busy');continue
            if len(trades)>=1000:log(t,sym,p,'first_1000_cap');continue
            if p['side']=='short' and model!='hypothetical_1x_long_short':log(t,sym,p,'spot_short_unavailable');continue
            if bar.v==0:log(t,sym,p,'zero_volume_entry_unavailable');continue
            sized=size_order(cash,raw,stop,p['side'],strict=model=='strict_spot',actual_slip=slip)
            if sized['status']!='sized':log(t,sym,p,sized['status']);continue
            before=cash;q=sized['quantity'];e=sized['entry_fill'];fee=q*e*D('.001')
            cash-=q*e+fee if p['side']=='long' else fee
            position=dict(symbol=sym,setup_id=p['setup_id'],side=p['side'],signal_time_ms=int(p['signal_time_ms']),
                feature_cutoff_ms=t,entry_time_ms=t,entry_time_utc=utc(t),raw_entry=raw,entry_fill=e,
                stop=stop,target=target,entry_fee=fee,equity_before=before,
                quantity=q,risk_budget=sized['risk_budget'],planned_risk=sized['planned_risk'],
                cash_required=sized['cash_required'],cash_capped=sized['cash_capped'],
                execution_status='conditional_OHLC_fractional_not_exchange_verified',**{k:v for k,v in f.items() if k!='feature_cutoff_ms'})
            if cash<0:raise ValueError('Negative available cash')
            log(t,sym,p,'entered',equity_before=before,quantity=q,planned_risk=sized['planned_risk'])
            max_positions=max(max_positions,1);changed=True
        if position:
            in_market+=1;p=position;a=assets[p['symbol']];i=a.by_time.get(t)
            if i is None or a.bars[i].v==0:unpriced_hours+=1
            if i is not None:
                b=a.bars[i];result=None
                if b.v>0:
                    # Missing data are discovered now; never fabricate an exit at an earlier close.
                    if i and a.times[i]-a.times[i-1]!=H:result=(b.o,'data_gap_first_quote')
                    else:result=exit_price(p['side'],b.o,b.h,b.l,p['stop'],p['target'],priority)
                    if result is None and not a.snaps[i]['integrity_ok']:result=(b.c,'integrity_review_exit')
                    if result is None and policy.startswith('E') and t+H-p['entry_time_ms']>=72*H:result=(b.c,'max_hold_72h')
                    if t+H in CUTS or t+H==end:
                        if result is None:result=(b.c,'calendar_boundary')
                if result:close(t if result[1] in ('stop_gap','data_gap_first_quote') else t+H,*result);changed=True
        # Canonical signals are generated only after this bar has CLOSED.
        for sym,a in sorted(assets.items()):
            i=a.by_time.get(t)
            if i is None:continue
            snap=a.snaps[i];p=pending.get(sym);candidate=snap.get('candidate')
            if p and not p.get('market_at') and (not snap['integrity_ok'] or not policy.startswith('E') and (not candidate or candidate['setup_id']!=p['setup_id'])):
                log(t+H,sym,p,'pending_closed_state_cancel');pending.pop(sym)
            if sym not in pending and snap['integrity_ok'] and candidate and t+H not in CUTS and t+H<end:
                ident=(sym,candidate['setup_id'])
                if ident not in consumed:
                    consumed.add(ident);pending[sym]=dict(candidate)
                    log(t+H,sym,candidate,'canonical_signal_registered')
        if t+H in CUTS or t+H==end:
            for sym,p in pending.items():log(t+H,sym,p,'calendar_pending_cancel')
            pending.clear();consumed.clear();phase_start=t+H
        mark=cash
        if position:
            p=position;raw=last_marks.get(p['symbol'],p['raw_entry']);x=fill(raw,p['side'],False,slip)
            mark+=p['quantity']*x*D('.999') if p['side']=='long' else p['quantity']*(p['entry_fill']-x)-p['quantity']*x*D('.001')
        peak=max(peak,mark);dd=(peak-mark)/peak if peak>0 else D(0);maxdd=max(maxdd,dd)
        if position or was_held or changed or (t+H)%86400000==0 or t+H==end:
            curve.append(dict(time_ms=t+H,time_utc=utc(t+H),equity_usdt=mark,cash_usdt=cash,
                position_symbol=position['symbol'] if position else '',drawdown_pct=dd*100,
                stale_price_mark=bool(position and (t not in assets[position['symbol']].by_time))))
    return dict(trades=trades,curve=[dict(time_ms=start,time_utc=utc(start),equity_usdt=D(1000),cash_usdt=D(1000),position_symbol='',drawdown_pct=D(0)),*curve],
        signals=logs,decision_counts=dict(counts),final_equity=mark,maximum_drawdown_pct=maxdd*100,
        in_market_hours=in_market,maximum_simultaneous_positions=max_positions,
        terminal_position_unresolved=position is not None,unpriced_exposure_hours=unpriced_hours,calendar_start=start,calendar_end=end)
