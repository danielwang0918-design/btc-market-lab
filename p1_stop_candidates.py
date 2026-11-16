"""P1-06B: isolated historical candidates, finite cash and OHLC reachability."""
from __future__ import annotations
import bisect
from collections import deque
from decimal import Decimal as D, localcontext, ROUND_FLOOR
import p1_portfolio as old
import p1_loss_diagnostics as context

H=3600000
POLICIES=('A','E_CLOSE','E_BREAK_RETEST','S_SWING','S_ATR1','F_4H','I_SWEEP20')
SCENARIOS=('source_stop_first','feasible_lower','feasible_upper','source_slip15')
PROTOCOL_SHA='78c4d4b6ba4ef909880700619825781d6f5050f11abd18849dcee45d4f22c173'

def net_unit(e,x,side,slip=D(5)):
    return old.net_per_unit(e,x,side,slip)

def net_rr(e,s,tp,side):
    return net_unit(e,tp,side)/-net_unit(e,s,side)

def target_for_R(e,s,side,R):
    risk=-net_unit(e,s,side);ef=old.fill(e,side,True)
    return (ef*D('1.001')+R*risk)/(D('.9995')*D('.999')) if side=='long' else (ef*D('.999')-R*risk)/(D('1.0005')*D('1.001'))

def size_cash(equity,e,s,side,slip=D(5)):
    if min(equity,e,s)<=0 or not (s<e if side=='long' else s>e):return None
    risk=-net_unit(e,s,side,slip);ef=old.fill(e,side,True,slip)
    q=min(equity*D('.02')/risk,equity/(ef*D('1.001')))
    q=q.quantize(D('1e-18'),rounding=ROUND_FLOOR)
    if q<=0:return None
    return dict(quantity=q,planned_risk=q*risk,risk_budget=equity*D('.02'),
        cash_required=q*ef*D('1.001'),entry_fill=ef,cash_capped=q*risk<equity*D('.02')-D('1e-12'))

def feasible_bar(b,e,s,tp,side,holding):
    """All continuous barrier paths: state graph, not two invented extremum paths."""
    if not (0<b.l<=min(b.o,b.c)<=max(b.o,b.c)<=b.h):raise ValueError('Invalid OHLC')
    points=sorted({b.o,b.h,b.l,b.c,*[v for v in (e,s,tp) if b.l<=v<=b.h]})
    def advance(state,p):
        if state=='unfilled' and p==e:state='hold'
        if state=='hold':
            if (p<=s if side=='long' else p>=s):return 'stop'
            if (p>=tp if side=='long' else p<=tp):return 'target'
        return state
    i=points.index(b.o);start=(i,b.o==b.h,b.o==b.l,advance('hold' if holding else 'unfilled',b.o))
    seen={start};queue=deque([start]);out=set()
    while queue:
        i,high,low,state=queue.popleft()
        if points[i]==b.c and high and low:out.add(state)
        for j in (i-1,i+1):
            if 0<=j<len(points):
                p=points[j];n=(j,high or p==b.h,low or p==b.l,advance(state,p))
                if n not in seen:seen.add(n);queue.append(n)
    return out

def latest_pivot(rows,times,t):
    eligible=[(p,v) for p,c,v in rows if times[c]+H<=t and times[p]>=t-200*H]
    return D(str(eligible[-1][1])) if eligible else None

def sweep_side(bars,i):
    if i<20:return None
    hist=bars[i-20:i+1]
    if any(b.open_ms-a.open_ms!=H for a,b in zip(hist,hist[1:])):return None
    b=bars[i];lo=min(x.l for x in hist[:-1]);hi=max(x.h for x in hist[:-1])
    down=b.l<lo;up=b.h>hi
    if down and up or b.v==0:return None
    if down and b.c>lo and b.c>b.o:return 'long'
    if up and b.c<hi and b.c<b.o:return 'short'
    return None

def prepare_context(a):
    a.regimes={f:context.RegimeIndex(b,f,source=a.reg if f=='1h' else None) for f,b in a.frames.items()}
    a.stop_context_cache={}

def asof(a,t):
    if t in a.stop_context_cache:return a.stop_context_cache[t]
    values={f:r.at(t) for f,r in a.regimes.items()}
    out=dict(feature_cutoff_ms=t,atr=values['1h']['atr14'],volatility=values['1h']['volatility'],
        **{'MA_'+f:v['direction'] for f,v in values.items()})
    a.stop_context_cache[t]=out
    return out

def episodes(signals,assets):
    records=[];active={}
    offer={'entered','global_position_busy','spot_short_unavailable','zero_volume_entry_unavailable','first_1000_cap'}
    cancel={'pending_closed_state_cancel','invalid_before_entry','pending_data_gap_cancel',
        'target_zone_order_unknown_cancel','calendar_pending_cancel','expired','reference_outside_bar'}
    for r in signals:
        ident=(r['symbol'],r['setup_id']);t=int(r['time_ms']);a=assets[r['symbol']]
        if r['event']=='canonical_signal_registered':
            c=a.snaps[a.by_time[t-H]]['candidate']
            if c is None or c['setup_id']!=r['setup_id']:raise ValueError('Registration/tape mismatch')
            event=dict(c,symbol=r['symbol'],registration_ms=t,event_id='|'.join((*ident,str(t))),anchor_ms=None,
                anchor_status='not_touched',universe='controlled_A_registrations')
            records.append(event);active[ident]=event
        elif ident in active and r['event'] in offer|cancel:
            event=active.pop(ident);event['anchor_status']=r['event']
            if r['event'] in offer:event['anchor_ms']=t
    return records

def controlled_plan(event,policy,a):
    out=dict(event,policy=policy,status=event['anchor_status'],entry_time_ms=None,action='觀望',
        basis=policy,original_SL=event['stop'],original_TP1=event['target'],
        confirmation_required='原候選失效前滿足事前固定規則',invalidation='original_SL_TP_gap_age_boundary_or_confirmation_failed')
    t=event['anchor_ms']
    if t is None:return out
    side=event['side'];s=D(event['stop']);tp=D(event['target']);e=D(event['entry_reference'])
    cutoff=next(c for c in old.CUTS if t<c);market=False;basis=policy
    if policy.startswith('E_'):
        level=latest_pivot(a.pivot_highs if side=='long' else a.pivot_lows,a.times,t)
        if policy=='E_BREAK_RETEST' and level is None:return dict(out,status='unknown_confirmed_pivot')
        if policy=='E_BREAK_RETEST':
            out.update(confirmation_reference_pivot=level,pivot_at_or_beyond_original_target=level>=tp if side=='long' else level<=tp)
        phase='break';deadline=t+3*H;found=None;prev=a.bars[a.by_time[t]-1].c
        clock=t
        while clock<deadline and clock+H<cutoff:
            i=a.by_time.get(clock)
            if i is None:return dict(out,status='confirmation_gap')
            b=a.bars[i]
            if b.v==0:return dict(out,status='confirmation_zero_volume')
            if b.l<=s or b.h>=tp if side=='long' else b.h>=s or b.l<=tp:
                return dict(out,status='original_SL_TP_invalidated_while_waiting')
            good=b.c>b.o if side=='long' else b.c<b.o
            if policy=='E_CLOSE':
                good=good and (b.c>D(event['zone_high']) if side=='long' else b.c<D(event['zone_low']))
            elif phase=='break':
                good=good and (prev<=level<b.c if side=='long' else prev>=level>b.c)
                if good:phase='retest';deadline=clock+4*H;good=False
            else:good=good and b.l<=level<=b.h and (b.c>level if side=='long' else b.c<level)
            if good:found=clock+H;break
            prev=b.c;clock+=H
        if found is None:return dict(out,status=('break_not_confirmed' if phase=='break' else 'retest_not_confirmed') if policy=='E_BREAK_RETEST' else 'confirmation_expired')
        if found not in a.by_time or found-t>168*H:return dict(out,status='next_open_gap')
        t=found;e=a.bars[a.by_time[t]].o;market=True
    f=asof(a,t);atr=D(str(f['atr'])) if f['atr'] is not None and f['atr']>0 else None
    out.update(f)
    if policy in ('S_ATR1','S_SWING'):
        if atr is None:return dict(out,status='unknown_prior_ATR')
        if policy=='S_ATR1':s=min(s,e-atr) if side=='long' else max(s,e+atr)
        else:
            pivot=latest_pivot(a.pivot_lows if side=='long' else a.pivot_highs,a.times,t)
            if pivot is None:return dict(out,status='unknown_confirmed_pivot')
            s=min(s,pivot-D('.1')*atr) if side=='long' else max(s,pivot+D('.1')*atr)
    if policy=='F_4H' and f['MA_4h']!=('bullish' if side=='long' else 'bearish'):
        return dict(out,status='4H_MA_filter_rejected')
    if not (0<s<e<tp if side=='long' else 0<tp<e<s):return dict(out,status='invalid_entry_SL_TP')
    rr=net_rr(e,s,tp,side)
    out.update(proposed_entry=e,proposed_SL=s,proposed_TP1=tp,proposed_net_RR=rr)
    if policy not in ('A','F_4H') and rr<D('1.5'):return dict(out,status='net_RR_below_1.5')
    if a.bars[a.by_time[t]].v==0:return dict(out,status='zero_volume_entry')
    if t-event['setup_confirm_ms']>168*H:return dict(out,status='setup_age_expired')
    return dict(out,status='eligible',entry_time_ms=t,entry_time_utc=old.utc(t),raw_entry=e,
        stop=s,target=tp,planned_net_RR=rr,market_at_open=market,maximum_hold_hours=None,
        basis=basis,SL=s,TP1=tp,TP2='',TP2_status='not_defined',action='僅離線候選',
        invalidation='original_SL_TP_gap_age_boundary_or_confirmation_failed',**f)

def independent_plans(a):
    out=[]
    for i,b in enumerate(a.bars):
        t=b.open_ms+H
        if t<old.START or t>=old.END or t in old.CUTS:continue
        side=sweep_side(a.bars,i)
        if side is None:continue
        event=dict(policy='I_SWEEP20',symbol=a.symbol,side=side,registration_ms=t,anchor_ms=t,
            universe='independent_sweep20',event_id=f'{a.symbol}|sweep20|{t}',setup_id=f'{a.symbol}:sweep20:{t}',
            signal_time_ms=t,status='unknown_prior_ATR',entry_time_ms=None,action='觀望',
            confirmation_required='20棒單側掃蕩同向收復後下一根連續開盤',invalidation='gap_stop_boundary_or_72h')
        f=asof(a,b.open_ms);atr=f['atr']
        if atr is None or atr<=0:out.append(event);continue
        s=b.l-D('.1')*D(str(atr)) if side=='long' else b.h+D('.1')*D(str(atr))
        j=a.by_time.get(t)
        if j is None:out.append(dict(event,status='next_open_gap'));continue
        e=a.bars[j].o
        if a.bars[j].v==0 or not (s<e if side=='long' else s>e):out.append(dict(event,status='invalid_next_open'));continue
        tp=target_for_R(e,s,side,D(2))
        if min(s,tp)<=0:out.append(dict(event,status='invalid_target'));continue
        out.append(dict(event,status='eligible',entry_time_ms=t,entry_time_utc=old.utc(t),raw_entry=e,stop=s,target=tp,
            planned_net_RR=net_rr(e,s,tp,side),market_at_open=True,maximum_hold_hours=72,
            SL=s,TP1=tp,TP2='',TP2_status='not_defined',action='僅離線候選',
            basis='prior20_sweep_reclaim_proxy_not_full_Wyckoff_or_Turtle_Soup',
            invalidation='gap_stop_boundary_or_72h',**asof(a,t)))
    return out

def outcome_paths(plan,a):
    e,s,tp=plan['raw_entry'],plan['stop'],plan['target'];side=plan['side'];start=plan['entry_time_ms']
    end=next(c for c in old.CUTS if start<c);source=None;choices=[];continuing=True;uncertain=False
    def exit_(b,raw,reason):
        op=reason in ('stop_gap','data_gap_first_quote');stamp=b.open_ms if op else b.open_ms+H
        return dict(exit_time_ms=stamp,exit_time_utc=old.utc(stamp),exit_bar_ms=b.open_ms,
            reserved_until_ms=b.open_ms+H,raw_exit=raw,exit_reason=reason)
    for i in range(a.by_time[start],len(a.bars)):
        b=a.bars[i]
        if b.open_ms>=end:break
        if b.v==0:uncertain=True;continue
        gap=i>0 and a.times[i]-a.times[i-1]!=H
        if gap:uncertain=True
        opened=(side=='long' and b.o<=s) or (side=='short' and b.o>=s)
        forced=None
        if gap:forced=(b.o,'data_gap_first_quote')
        elif opened:forced=(b.o,'stop_gap')
        close_reason=None
        if not a.snaps[i]['integrity_ok']:close_reason='integrity_review_exit'
        if close_reason is None and plan['maximum_hold_hours'] and b.open_ms+H-start>=plan['maximum_hold_hours']*H:close_reason='max_hold_72h'
        if close_reason is None and b.open_ms+H==end:close_reason='calendar_boundary'
        if source is None:
            price=forced or old.exit_price(side,b.o,b.h,b.l,s,tp)
            if price is None and close_reason:price=(b.c,close_reason)
            if price is not None:source=exit_(b,*price)
        if continuing:
            if forced:
                choices.append(exit_(b,*forced));continuing=False
            else:
                if b.open_ms==start and not plan['market_at_open']:
                    states=feasible_bar(b,e,s,tp,side,False)
                    if 'unfilled' in states:raise ValueError('Eligible reference never filled')
                else:
                    sl=b.l<=s if side=='long' else b.h>=s;hit=b.h>=tp if side=='long' else b.l<=tp
                    states=({'stop'} if sl else set())|({'target'} if hit else set()) or {'hold'}
                for state in ('stop','target'):
                    if state in states:choices.append(exit_(b,s if state=='stop' else tp,state))
                continuing='hold' in states
                if continuing and close_reason:choices.append(exit_(b,b.c,close_reason));continuing=False
        if source is not None and not continuing:break
    if source is None or continuing:
        raise ValueError('Unresolved terminal holding: cannot fabricate liquidation')
    key=lambda c:net_unit(e,c['raw_exit'],side)
    return dict(source_stop_first=source,source_slip15=source,feasible_lower=min(choices,key=key),feasible_upper=max(choices,key=key),
        feasible_terminal_outcomes=len(choices),path_dependent_outcome=len({(c['raw_exit'],c['exit_time_ms']) for c in choices})>1,
        gap_or_zero_volume_path_unknown=uncertain)

def ledger(plans,paths,policy,model,scenario,start,end,assets):
    slip=D(15) if scenario=='source_slip15' else D(5);cash=D(1000);busy=start;trades=[];decisions=[]
    selected=[p for p in plans if p['policy']==policy and start<=p['registration_ms']<end]
    for p in selected:
        if p['status']!='eligible':decisions.append(dict(event_id=p['event_id'],status=p['status']))
    offers=sorted((p for p in selected if p['status']=='eligible'),key=lambda p:(p['entry_time_ms'],p['symbol'],p['setup_id']))
    for p in offers:
        t=p['entry_time_ms'];reason=None
        if t<busy:reason='global_position_busy'
        elif len(trades)>=1000:reason='first_1000_cap'
        elif p['side']=='short' and model=='fractional_spot':reason='spot_short_unavailable'
        if reason:decisions.append(dict(event_id=p['event_id'],status=reason));continue
        sized=size_cash(cash,p['raw_entry'],p['stop'],p['side'],slip)
        if sized is None:decisions.append(dict(event_id=p['event_id'],status='insufficient_cash_or_quantity'));continue
        path=paths[p['event_id']+'|'+policy];outcome=path[scenario];q=sized['quantity'];before=cash
        e,x=old.fill(p['raw_entry'],p['side'],True,slip),old.fill(outcome['raw_exit'],p['side'],False,slip)
        fees=q*(e+x)*D('.001');net=q*net_unit(p['raw_entry'],outcome['raw_exit'],p['side'],slip)
        rawgross=q*(outcome['raw_exit']-p['raw_entry'])*(1 if p['side']=='long' else -1)
        cash+=net
        trade=dict(p,**outcome,**sized,model=model,scenario=scenario,equity_before=before,equity_after=cash,
            exit_fill=x,fees_usdt=fees,slippage_usdt=rawgross-net-fees,net_pnl_usdt=net,r_multiple=net/sized['planned_risk'],
            cost_scenario_target_RR=net_unit(p['raw_entry'],p['target'],p['side'],slip)/-net_unit(p['raw_entry'],p['stop'],p['side'],slip),
            loss_exceeds_budget=-net>sized['risk_budget']+D('1e-9'),path_dependent_outcome=path['path_dependent_outcome'],
            gap_or_zero_volume_path_unknown=path['gap_or_zero_volume_path_unknown'])
        trades.append(trade);busy=outcome['reserved_until_ms'];decisions.append(dict(event_id=p['event_id'],status='entered'))
    curve=[];at=0;position=None;balance=D(1000);peak=D(1000);dd=D(0);exposure=0;last_price=None
    for t in range(start,end,H):
        if at<len(trades) and trades[at]['entry_time_ms']==t:position=trades[at];at+=1;last_price=position['raw_entry']
        held=position is not None;mark=balance;free=balance;stale=False
        if position:
            exposure+=1;p=position;a=assets[p['symbol']];i=a.by_time.get(t)
            if i is not None:last_price=a.bars[i].c
            stale=i is None or a.bars[i].v==0
            if p['exit_bar_ms']==t:balance=p['equity_after'];mark=balance;position=None;free=balance
            else:
                mark=balance+p['quantity']*net_unit(p['raw_entry'],last_price,p['side'],slip)
                free=balance-p['cash_required']
        peak=max(peak,mark);dd=max(dd,(peak-mark)/peak*100)
        if held or (t+H)%86400000==0 or t+H==end:curve.append(dict(time_ms=t+H,time_utc=old.utc(t+H),equity_usdt=mark,cash_usdt=free,
            stale_price_mark=stale,drawdown_pct=(peak-mark)/peak*100,position_symbol=position['symbol'] if position else ''))
    if position is not None or at!=len(trades) or abs(balance-cash)>D('1e-20'):raise ValueError('Ledger/calendar mismatch')
    return dict(trades=trades,decisions=decisions,curve=[dict(time_ms=start,time_utc=old.utc(start),equity_usdt=D(1000),cash_usdt=D(1000),drawdown_pct=D(0)),*curve],
        final_equity=cash,maximum_drawdown_pct=dd,in_market_hours=exposure,opportunities=len(selected),eligible_offers=len(offers))
