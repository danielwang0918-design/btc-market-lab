"""Two preregistered 20-DAY hypotheses, exclusively using closed candles."""
import bisect
from collections import Counter
from decimal import Decimal as D
from types import SimpleNamespace
import p1_stop_candidates as money
import p1_portfolio as old
import p1_stop_study as helpers

DAY=86400000
POLICIES=('T20_FAIL','T20_CONT')
def daily_reference(bars,t):
    day=t//DAY*DAY
    j=bisect.bisect_right([b.open_ms for b in bars],day-DAY)
    hist=bars[max(0,j-20):j]
    if len(hist)!=20 or hist[-1].open_ms+DAY!=day:return None
    if any(y.open_ms-x.open_ms!=DAY for x,y in zip(hist,hist[1:])) or any(b.v==0 for b in hist):return None
    hi=max(b.h for b in hist);lo=min(b.l for b in hist)
    return dict(high=hi,low=lo,high_age=(day-max(b.open_ms for b in hist if b.h==hi))//DAY,
        low_age=(day-max(b.open_ms for b in hist if b.l==lo))//DAY,reference_closed_ms=day)

def intraday_prefix(bars,i):
    day=bars[i].open_ms//DAY*DAY
    start=i-(bars[i].open_ms-day)//money.H
    if start<0:return None
    hist=bars[start:i+1]
    if hist[0].open_ms!=day or any(y.open_ms-x.open_ms!=money.H for x,y in zip(hist,hist[1:])) or any(b.v==0 for b in hist):return None
    return hist

def arm_side(policy,edge):
    return ('long' if edge=='upper' else 'short') if policy=='T20_CONT' else ('short' if edge=='upper' else 'long')

def entry_location(side,price,level):return price>level if side=='long' else price<level

def execution_view(a):
    return SimpleNamespace(bars=a.bars,times=a.times,by_time=a.by_time,snaps=[{'integrity_ok':True} for _ in a.bars])

def generate(a):
    plans=[];availability=Counter();day_seen={};references={}
    for i,b in enumerate(a.bars):
        if b.open_ms<old.START or b.open_ms>=old.END:continue
        day=b.open_ms//DAY*DAY
        if day not in references:references[day]=daily_reference(a.frames['1d'],b.open_ms)
        ref=references[day]
        if ref is None:availability['missing_20D_reference_hours']+=1;continue
        availability['available_20D_reference_hours']+=1
        edges=[]
        if b.h>ref['high']:edges.append('upper')
        if b.l<ref['low']:edges.append('lower')
        seen=day_seen.setdefault(day,set())
        for edge in edges:
            if edge in seen:continue
            seen.add(edge);level=ref['high' if edge=='upper' else 'low'];age=ref['high_age' if edge=='upper' else 'low_age']
            eid=f'{a.symbol}|20D|{day}|{edge}'
            for policy in POLICIES:
                p=dict(event_id=eid,setup_id=eid,policy=policy,symbol=a.symbol,edge=edge,level=level,reference_age_days=age,
                    reference_closed_ms=ref['reference_closed_ms'],registration_ms=b.open_ms+money.H,anchor_ms=b.open_ms+money.H,
                    signal_time_ms=b.open_ms+money.H,side=arm_side(policy,edge),entry_time_ms=None,status='no_same_day_reclaim',action='觀望',
                    universe='first_20D_daily_level_breach',basis=policy+'_1H_closed_adaptation',TP2='',TP2_status='not_defined',
                    invalidation='same_day_expiry_gap_zero_volume_stop_or72h',confirmation_required='已收盤1H符合指定延續或收復，下一根連續開盤')
                def reject(reason):plans.append(dict(p,status=reason))
                if age<4:reject('reference_age_below4');continue
                if len(edges)==2:reject('both_sides_same_bar');continue
                found=None
                for j in range(i,len(a.bars)):
                    bar=a.bars[j]
                    if bar.open_ms//DAY*DAY!=day:break
                    if intraday_prefix(a.bars,j) is None:found='intraday_prefix_gap_or_zero';break
                    outside=bar.c>level if edge=='upper' else bar.c<level
                    inside=bar.c<level if edge=='upper' else bar.c>level
                    if policy=='T20_CONT':
                        found=j if outside else 'first_close_not_continuation';break
                    if inside:found=j;break
                if not isinstance(found,int):reject(found or 'no_same_day_reclaim');continue
                j=found;confirm=a.bars[j];t=confirm.open_ms+money.H
                if t>=day+DAY or t in old.CUTS or t>=old.END:reject('day_or_split_expired');continue
                k=a.by_time.get(t)
                # The next open quote is available at entry, its final hourly
                # volume is not. outcome_paths flags zero-volume uncertainty
                # after entry; never select opportunities using that future bar.
                if k is None:reject('next_open_gap');continue
                e=a.bars[k].o
                valid_location=entry_location(p['side'],e,level)
                # Both long arms must stay above their respective level; short mirror.
                if not valid_location:reject('next_open_lost_confirmation');continue
                if not j or a.bars[j-1].open_ms+money.H!=confirm.open_ms:
                    reject('preconfirmation_ATR_gap');continue
                atr=a.reg.atr[j-1]
                if atr is None or atr<=0:reject('unknown_preconfirmation_ATR');continue
                prefix=intraday_prefix(a.bars,j);s=min(v.l for v in prefix)-D('.1')*D(str(atr)) if p['side']=='long' else max(v.h for v in prefix)+D('.1')*D(str(atr))
                if not (0<s<e if p['side']=='long' else 0<e<s):reject('invalid_SL');continue
                tp=money.target_for_R(e,s,p['side'],D(2))
                if tp<=0:reject('invalid_TP');continue
                row=dict(p,status='eligible',entry_time_ms=t,entry_time_utc=old.utc(t),signal_time_ms=t,
                    confirmation_open_ms=confirm.open_ms,raw_entry=e,stop=s,target=tp,SL=s,TP1=tp,
                    planned_net_RR=money.net_rr(e,s,tp,p['side']),market_at_open=True,maximum_hold_hours=72,
                    known_day_low=min(v.l for v in prefix),known_day_high=max(v.h for v in prefix),action='僅離線候選',**money.asof(a,t))
                helpers.plan_fields(row);plans.append(row)
    return plans,dict(availability)
