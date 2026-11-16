"""Bounded posthoc paths and separately clocked features; never trading signals."""
from __future__ import annotations
import bisect
from decimal import Decimal as D
import p1_portfolio as money
import p1_loss_diagnostics as legacy
import p1_08a_diagnostics as states
import p1_07_signals as daily

H=money.H
HORIZONS=(6,12,24,48,72)
OPEN_EXITS=legacy.OPEN_EXITS
INTRABAR_EXITS=legacy.INTRABAR_EXITS


def flag(value):
    if value not in (True,False,'True','False'):raise ValueError('Unknown execution type')
    return value in (True,'True')


def units(row):
    e,s=D(row['raw_entry']),D(row['stop']);r0=abs(e-s);q=D(row['quantity'])
    slip=D(15) if row['scenario']=='source_slip15' else D(5)
    risk=-money.net_per_unit(e,s,row['side'],slip)
    if r0<=0 or q<=0 or risk<=0 or not (s<e if row['side']=='long' else s>e):raise ValueError('Invalid original risk')
    if abs(D(row['planned_risk'])/q-risk)>D('1e-12')*max(D(1),risk):raise ValueError('Original unit risk differs from archived costs')
    return e,s,r0,risk,slip


def break_even(e,side,slip):
    v=slip/D(10000);fee=D('.001')
    return e*(1+v)*(1+fee)/((1-v)*(1-fee)) if side=='long' else e*(1-v)*(1-fee)/((1+v)*(1+fee))


def _arrival(name,threshold,adverse,observed,known,start,complete):
    hit=lambda p:p[0]<=threshold if adverse else p[0]>=threshold
    possible=sorted((p for p in observed if hit(p)),key=lambda p:(p[1],p[2]))
    confirmed=sorted((p for p in known if hit(p)),key=lambda p:(p[2],p[1]))
    return dict(level=name,status='data_unknown' if not complete else 'confirmed' if confirmed else 'possible_only' if possible else 'not_reached',
        possible_from_hours=D((possible[0][1] if complete else start)-start)/H if possible else None,
        confirmed_by_hours=D(confirmed[0][2]-start)/H if confirmed and complete else None,
        first_observed_bar_ms=possible[0][3] if possible else None)


def analyze(row,bars,horizon_hours=None,tape=False,cutoff=money.END):
    if horizon_hours is not None and horizon_hours not in HORIZONS:raise ValueError('Only fixed diagnostic horizons')
    if tape and horizon_hours is None:raise ValueError('Tape requires fixed horizon')
    index=bars if isinstance(bars,legacy.BarIndex) else legacy.BarIndex(bars)
    start=int(row['entry_time_ms']);exit_time=int(row['exit_time_ms']);exit_bar=int(row['exit_bar_ms'])
    if start%H or exit_time%H or exit_time<start:raise ValueError('Invalid archived path clock')
    market=flag(row['market_at_open']);e,s,r0,risk,slip=units(row);sign=1 if row['side']=='long' else -1
    requested=start+horizon_hours*H if horizon_hours is not None else exit_time
    end=requested if tape else min(requested,exit_time)
    censored=end>cutoff;end=min(end,cutoff)
    if end<start:raise ValueError('Cutoff before entry')
    chosen=index.window(start,end)
    missing=(end-start)//H-len(chosen);zero=sum(b.v==0 for b in chosen)
    if missing<0:raise ValueError('Bad candle spacing')
    complete=not censored and not missing and not zero
    move=lambda p:(p-e)*sign
    known=[(D(0),start,start,start)];observed=list(known);closes=[]
    exit_uncertain=not tape and end==exit_time and row['exit_reason'] in INTRABAR_EXITS
    for b in chosen:
        if b.v==0:continue
        lo,hi=b.open_ms,b.open_ms+H
        entry_amb=not market and lo==start and not tape
        exit_amb=exit_uncertain and lo==exit_bar
        extremes=[(move(p),lo,hi,lo) for p in (b.h,b.l)]
        observed.extend(extremes)
        if not entry_amb and not exit_amb:known.extend(extremes)
        if not entry_amb:known.append((move(b.o),lo,lo,lo))
        if not exit_amb:
            known.append((move(b.c),hi,hi,lo));closes.append(b.c)
    endpoint=None
    if not tape and end==exit_time:
        endpoint=D(row['raw_exit']);lo=exit_bar if exit_uncertain else exit_time
        point=(move(endpoint),lo,exit_time,exit_bar);known.append(point);observed.append(point)
    elif chosen and chosen[-1].open_ms+H==end and chosen[-1].v>0:
        endpoint=chosen[-1].c
    observed+=known
    mfe_lo=max(p[0] for p in known);mae_lo=-min(p[0] for p in known)
    envelope_f=max(p[0] for p in observed);envelope_a=-min(p[0] for p in observed)
    mfe_hi=envelope_f if complete else None;mae_hi=envelope_a if complete else None
    endpoint_move=move(endpoint) if endpoint is not None else None
    terminal=[] if endpoint is None else [endpoint]
    sequence=[e]+closes+terminal
    variation=sum(abs(b-a) for a,b in zip(sequence,sequence[1:]))
    er=abs(sequence[-1]-e)/variation if variation and complete else D(0) if complete else None
    near=sum(abs(p-e)<=r0/2 for p in closes)/len(closes) if closes and complete else None
    levels={f'favorable_{x}R0':(D(x)*r0,False) for x in ('0.5','1','2')}
    be=break_even(e,row['side'],slip)
    levels.update(cost_break_even=(move(be),False),TP=(move(D(row['target'])),False),SL=(move(s),True))
    arrivals=[_arrival(name,value,adverse,observed,known,start,complete) for name,(value,adverse) in levels.items()]
    first={r['level']:r['first_observed_bar_ms'] for r in arrivals}
    if not complete:order='data_unknown'
    elif not market and first['TP']==start or not market and first['SL']==start:order='entry_bar_unknown'
    elif first['TP'] is not None and first['SL'] is not None:
        order='same_bar_unknown' if first['TP']==first['SL'] else 'TP_first_distinct_bars' if first['TP']<first['SL'] else 'SL_first_distinct_bars'
    elif first['TP'] is not None:order='only_TP_observed'
    elif first['SL'] is not None:order='only_SL_observed'
    else:order='neither_observed'
    peak=next(p for p in observed if p[0]==envelope_f);trough=next(p for p in observed if p[0]==-envelope_a)
    if not complete:peak_order='data_unknown'
    elif not market or exit_uncertain:peak_order='entry_or_exit_bar_unknown'
    elif not envelope_f or not envelope_a:peak_order='not_both_moved'
    elif peak[3]==trough[3]:peak_order='same_bar_unknown'
    else:peak_order='favorable_peak_first' if peak[3]<trough[3] else 'adverse_peak_first'
    def potential(amount):
        if amount is None:return None,None
        net=money.net_per_unit(e,e+sign*amount,row['side'],slip)
        return net/risk,(amount-net)/amount if amount else None
    net_lo,cost_lo=potential(mfe_lo);net_hi,cost_hi=potential(mfe_hi)
    peak_end=D(peak[2]-start)/H if envelope_f else None
    segment=next((label for limit,label in ((6,'0-6'),(12,'6-12'),(24,'12-24'),(48,'24-48'),(72,'48-72')) if peak_end is not None and peak_end<=limit),'after72_or_no_favorable')
    return dict(path_scope='counterfactual_tape' if tape else 'full_original_hold' if horizon_hours is None else 'held_prefix',
        requested_horizon_hours=horizon_hours,observed_end_ms=end,observed_hours=D(end-start)/H,
        complete=complete,right_censored=censored,missing_bars=missing,zero_volume_bars=zero,
        early_exit_capped=not tape and horizon_hours is not None and exit_time<requested,
        contains_post_exit_prices=tape and (end>exit_time or row['exit_reason'] in INTRABAR_EXITS and end>exit_bar),
        envelope_may_include_unheld_extremes=not market or exit_uncertain or tape and (end>exit_time or row['exit_reason'] in INTRABAR_EXITS and end>exit_bar),
        conditional_on_archived_fill_assumptions=True,
        entry_bar_uncertain=not market,exit_bar_uncertain=exit_uncertain,entry_clock_proxy=not market,
        R0_price=r0,R_model_per_unit=risk,mfe_R0_lower=mfe_lo/r0,mfe_R0_upper=mfe_hi/r0 if mfe_hi is not None else None,
        mae_R0_lower=mae_lo/r0,mae_R0_upper=mae_hi/r0 if mae_hi is not None else None,
        mfe_R_model_lower=mfe_lo/risk,mfe_R_model_upper=mfe_hi/risk if mfe_hi is not None else None,
        mae_R_model_lower=mae_lo/risk,mae_R_model_upper=mae_hi/risk if mae_hi is not None else None,
        mfe_price_lower=mfe_lo,mfe_price_upper=mfe_hi,mfe_price_observed_envelope=envelope_f,
        endpoint_gross_R0=endpoint_move/r0 if endpoint_move is not None else None,
        giveback_R0_lower=(mfe_lo-endpoint_move)/r0 if endpoint_move is not None else None,
        giveback_R0_upper=(mfe_hi-endpoint_move)/r0 if mfe_hi is not None and endpoint_move is not None else None,
        net_quote_potential_R_model_lower=net_lo,net_quote_potential_R_model_upper=net_hi,
        cost_over_mfe_lower=cost_lo,cost_over_mfe_upper=cost_hi,break_even_move_R0=move(be)/r0,
        close_efficiency_proxy=er,observed_closes_near_entry_fraction=near,observed_close_count=len(closes),
        TP_SL_order=order,peak_order=peak_order,peak_first_bar_end_hours=peak_end,peak_segment=segment,arrivals=arrivals)


def timeout_class(label):
    if not label['complete'] or label['entry_bar_uncertain'] or label['exit_bar_uncertain']:return 'unknown_data_or_order'
    if label['net_quote_potential_R_model_upper']<=0:return 'insufficient_even_cost_cover'
    if label['mfe_R0_upper']<D('.5'):return 'weak_MFE_below0.5R0'
    if label['mfe_R0_lower']>=1 and label['endpoint_gross_R0']<=label['mfe_R0_lower']/2:return 'substantial_giveback'
    if label['close_efficiency_proxy']<=D('.2'):return 'low_close_direction_efficiency'
    return 'mixed_or_partial_progress'


class BreakFeatures:
    def __init__(self,frames):
        self.frames=frames;self.hour=states.FrameState(frames['1h'],'1h');self.refs={};self.cache={}

    def at(self,row):
        out=dict(feature_status='not_defined_for_signal_family',break_open_ms=None,break_available_ms=None,prebreak_available_ms=None,
            body_share=None,upper_wick_share=None,lower_wick_share=None,body_bin='unknown',ATR_pre=None,
            compression_ratio=None,compression_bin='unknown',ER_pre=None,ER_pre_bin='unknown',break_rvol=None,break_rvol_bin='unknown',
            prebreak_ATR_pct=None,flat_cost5_over_ATR_pre=None,flat_cost15_over_ATR_pre=None,
            close_extension_ATR=None,extreme_extension_ATR=None,extension_bin='unknown',reference_high=None,reference_low=None,
            reference_closed_ms=None,preclose_location_20D=None,breakclose_location_20D=None,range_position_bin='unknown')
        if row['policy'] not in daily.POLICIES:return out
        reg=int(row['registration_ms']);key=(reg,row['edge'],str(row['level']))
        if key in self.cache:return self.cache[key]
        t=reg-H;j=bisect.bisect_left(self.hour.times,t)
        if j==len(self.hour.times) or self.hour.times[j]!=t:return dict(out,feature_status='missing_breach_candle')
        b=self.hour.bars[j];day=t//daily.DAY*daily.DAY
        if day not in self.refs:self.refs[day]=daily.daily_reference(self.frames['1d'],t)
        ref=self.refs[day]
        if ref is None:return dict(out,feature_status='missing_closed20D_reference')
        level=ref['high'] if row['edge']=='upper' else ref['low']
        if level!=D(row['level']):raise ValueError('Frozen daily level mismatch')
        sign=1 if row['edge']=='upper' else -1
        f=self.hour.at(t);p=j-1;st=self.hour.starts[j]
        out.update(feature_status='available_or_partial',break_open_ms=t,break_available_ms=reg,
            prebreak_available_ms=f['reference_close_ms'],reference_high=ref['high'],reference_low=ref['low'],reference_closed_ms=ref['reference_closed_ms'])
        width=b.h-b.l
        if width:
            body=abs(b.c-b.o)/width
            out.update(body_share=body,upper_wick_share=(b.h-max(b.o,b.c))/width,lower_wick_share=(min(b.o,b.c)-b.l)/width,
                body_bin='>=.5' if body>=D('.5') else '<.5')
        vol=self.hour.volume.feature_at(j);out.update(break_rvol=vol['rvol'],break_rvol_bin=vol['rvol_bin'])
        if p>=st and f['reference_close_ms']==t:
            total=self.hour.exact_tr_sums[p]
            atr=total/14 if total is not None else None
            out.update(ATR_pre=atr,ER_pre=f['ER20'],ER_pre_bin=f['ER_state'])
            if atr is not None and atr>0:
                ext=(b.c-level)*sign/atr
                preclose=self.hour.bars[p].c
                out.update(prebreak_ATR_pct=100*atr/preclose,
                    flat_cost5_over_ATR_pre=-money.net_per_unit(preclose,preclose,row['side'],D(5))/atr,
                    flat_cost15_over_ATR_pre=-money.net_per_unit(preclose,preclose,row['side'],D(15))/atr)
                out.update(close_extension_ATR=ext,extreme_extension_ATR=((b.h if sign==1 else b.l)-level)*sign/atr,
                    extension_bin='<.5' if ext<D('.5') else '.5-1' if ext<1 else '>=1')
            if p-20>=st and total is not None:
                previous=self.hour.exact_tr_sums[p-20:p]
                if all(v is not None for v in previous) and sum(previous)>0:
                    comp=total*20/sum(previous)
                    out.update(compression_ratio=comp,compression_bin='<1' if comp<1 else '>=1')
        span=ref['high']-ref['low']
        if span>0:
            loc=(b.c-ref['low'])/span
            out.update(breakclose_location_20D=loc,preclose_location_20D=(self.hour.bars[p].c-ref['low'])/span if p>=st else None,
                range_position_bin='below_range' if loc<0 else 'above_range' if loc>1 else 'inside_range')
        self.cache[key]=out;return out
