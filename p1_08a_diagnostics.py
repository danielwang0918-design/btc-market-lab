"""Closed-candle state annotations and archived-result diagnostics, never signals."""
from __future__ import annotations
import bisect, math, random
from collections import defaultdict,deque
from decimal import Decimal as D,localcontext
import p1_candlestick_research as core
import p1_market_regime as regime
import p1_volume_rvol as volume
import p1_loss_diagnostics as old_diag
import p1_portfolio as money

H=3600000
START=money.START
BLOCK=28*24*H
STATE_CLASSES={
    'MA':('bullish','bearish','transition_or_range','insufficient_prior_ma','reference_data_gap_or_stale'),
    'ER_state':('range_proxy','mixed','efficient_move_proxy','unknown'),
    'ATR_level':('low','normal','high','insufficient_prior_atr_distribution'),
    'volatility_change':('expansion','contraction','unchanged','unknown'),
    'break20':('up_break','down_break','upper_sweep_reclaim','lower_sweep_reclaim','both_sides_unknown','boundary_close','inside_range','unknown'),
    'rvol_bin':volume.RVOL_BINS,
    'turnover_bin':('low','normal','high','unknown'),
}


class FrameState:
    def __init__(self,bars,frame):
        if frame not in core.FRAMES:raise ValueError('Only closed 1h/4h/1d permitted')
        self.bars=bars;self.frame=frame;self.step=core.FRAMES[frame]
        self.volume=volume.VolumeSource(bars,frame)
        self.starts=self.volume.starts;self.times=[b.open_ms for b in bars]
        self.reg=regime.RegimeSource([regime.Candle(b.open_ms,float(b.o),float(b.h),float(b.l),float(b.c)) for b in bars],frame)
        self.index=old_diag.RegimeIndex(bars,frame,self.reg)
        self.turnovers=[b.c*b.v for b in bars]
        # Same14 true ranges as P1-03. Only the new change diagnostic uses
        # exact decimal sums; the /14 factors cancel. Comparing sums avoids
        # falsely labeling equal ranges as expansion from legacy float noise.
        self.exact_tr_sums=[None]*len(bars)
        with localcontext() as ctx:
            ctx.prec=max(ctx.prec,50);window=deque();total=D(0)
            for i,b in enumerate(bars):
                st=self.starts[i]
                if i==st:window.clear();total=D(0)
                tr=max(b.h-b.l,abs(b.h-bars[i-1].c),abs(b.l-bars[i-1].c)) if i>st else b.h-b.l
                window.append(tr);total+=tr
                if len(window)>14:total-=window.popleft()
                if i-st>=14:self.exact_tr_sums[i]=total
        self.zeros=[0]
        for b in bars:self.zeros.append(self.zeros[-1]+int(b.v==0))

    def at(self,t):
        p=bisect.bisect_right(self.times,t-self.step)-1
        out=dict(status='reference_gap_or_stale',reference_close_ms=None,history_start_ms=None,
            MA='reference_data_gap_or_stale',ER20=None,ER_state='unknown',ATR14=None,
            ATR_level='insufficient_prior_atr_distribution',ATR_change_ratio=None,volatility_change='unknown',
            break20='unknown',rvol=None,rvol_bin='insufficient_history',turnover_proxy=None,
            turnover_percentile=None,turnover_bin='unknown',illiquidity_proxy=None,known_zero_volume_count=None)
        if p<0 or self.times[p]+self.step!=t//self.step*self.step:return out
        st=self.starts[p];b=self.bars[p];ref=self.times[p]+self.step
        known=self.index.at(t);vf=self.volume.feature_at(p)
        out.update(status='closed_reference',reference_close_ms=ref,history_start_ms=self.times[max(st,p-219)],
            MA=known['direction'],ATR14=known['atr14'],ATR_level=known['volatility'],
            rvol=vf['rvol'],rvol_bin=vf['rvol_bin'],turnover_proxy=self.turnovers[p],
            known_zero_volume_count=self.zeros[p+1]-self.zeros[max(st,p-219)])
        if p-st>=20:
            changes=sum(abs(self.bars[j].c-self.bars[j-1].c) for j in range(p-19,p+1))
            er=abs(b.c-self.bars[p-20].c)/changes if changes else D(0)
            out.update(ER20=float(er),ER_state='range_proxy' if er<=D('.2') else 'efficient_move_proxy' if er>=D('.4') else 'mixed')
            old=self.exact_tr_sums[p-20];cur=self.exact_tr_sums[p]
            if old is not None and old>0 and cur is not None:
                ratio=cur/old
                out.update(ATR_change_ratio=float(ratio),volatility_change='expansion' if cur>old else 'contraction' if cur<old else 'unchanged')
            hi=max(x.h for x in self.bars[p-20:p]);lo=min(x.l for x in self.bars[p-20:p])
            up=b.h>hi;down=b.l<lo
            state=('both_sides_unknown' if up and down else
                'boundary_close' if (up and b.c==hi) or (down and b.c==lo) else
                'up_break' if up and b.c>hi else 'down_break' if down and b.c<lo else
                'upper_sweep_reclaim' if up else 'lower_sweep_reclaim' if down else 'inside_range')
            out['break20']=state
        if p-st>=200:
            prior=self.turnovers[p-200:p];cur=self.turnovers[p]
            rank=(sum(v<cur for v in prior)+.5*sum(v==cur for v in prior))/200
            out.update(turnover_percentile=rank,turnover_bin='low' if rank<=.25 else 'high' if rank>=.75 else 'normal')
        if p>st and self.turnovers[p]>0:
            out['illiquidity_proxy']=abs(math.log(float(b.c/self.bars[p-1].c)))/float(self.turnovers[p])
        if ref>t:raise ValueError('Future feature reference')
        return out


class FeatureBook:
    def __init__(self,frames):
        self.frames={frame:FrameState(bars,frame) for frame,bars in frames.items()};self.cache={}

    def at(self,t):
        if t not in self.cache:
            out={'feature_cutoff_ms':t}
            for frame,source in self.frames.items():
                out.update({key+'_'+frame:value for key,value in source.at(t).items()})
            self.cache[t]=out
        return self.cache[t]


def enrich_money(trade):
    row=dict(trade);row.update(old_diag.cost_decomposition(row))
    if abs(row['net_R']-D(row['r_multiple']))>D('1e-12'):raise ValueError('Archived R identity mismatch')
    e,s=D(row['raw_entry']),D(row['stop']);slip=D(15) if row['scenario']=='source_slip15' else D(5)
    unit=-money.net_per_unit(e,s,row['side'],slip)
    if unit<=0:raise ValueError('Invalid known normal-stop risk')
    row['expected_stop_cost_share']=(unit-abs(e-s))/unit
    row['cost_share_bin']='<=.25' if row['expected_stop_cost_share']<=D('.25') else '>.25'
    row['holding_hours']=D(int(row['exit_time_ms'])-int(row['entry_time_ms']))/H
    row['holding_bucket']=old_diag.holding_bucket(row['holding_hours'])
    row['entry_session_UTC']=('00-08','08-16','16-24')[(int(row['entry_time_ms'])//H%24)//8]
    row['entry_year']=money.utc(int(row['entry_time_ms']))[:4]
    row['SL_distance_pct']=abs(e-s)/e*100
    row['TP_distance_pct']=abs(D(row['target'])-e)/e*100
    atr=row.get('ATR14_1h')
    ratio=abs(e-s)/D(str(atr)) if atr is not None and atr>0 else None
    row['SL_over_ATR']=ratio
    row['SL_ATR_bin']=old_diag.bucket(ratio,('.5','1','2')) if ratio is not None else 'unknown'
    return row


def summarize(rows):
    n=len(rows);nets=[D(str(r['net_R'])) for r in rows];pnl=[D(str(r['net_USDT'])) for r in rows]
    gross=sum((D(str(r['raw_gross_R'])) for r in rows),D(0));cost=sum((D(str(r['cost_drag_R'])) for r in rows),D(0))
    wins=sum(x>0 for x in pnl);losses=sum(x<0 for x in pnl);stops=sum(r['exit_reason'] in ('stop','stop_gap') for r in rows)
    pos=sum((x for x in pnl if x>0),D(0));neg=-sum((x for x in pnl if x<0),D(0))
    def rate(count):return 100*count/n if n else None
    def mean(values):return float(sum(values)/n) if n else None
    def truth(key):return sum(r.get(key) in (True,'True') for r in rows)
    return dict(n=n,stops=stops,wins=wins,losses=losses,flat=n-wins-losses,
        stop_rate_pct=rate(stops),net_loss_rate_pct=rate(losses),win_rate_pct=rate(wins),
        mean_net_R=mean(nets),mean_raw_R=float(gross/n) if n else None,mean_cost_R=float(cost/n) if n else None,
        profit_factor=float(pos/neg) if neg else None,profit_factor_status='defined' if neg else 'no_losses' if pos else 'no_trades_or_no_pnl',
        net_pnl_USDT=float(sum(pnl)),fees_USDT=float(sum((D(str(r['fees_USDT'])) for r in rows),D(0))),
        slippage_USDT=float(sum((D(str(r['slippage_USDT'])) for r in rows),D(0))),
        cost_flipped_trades=truth('cost_flipped_raw_gain_to_loss'),raw_direction_loss_trades=truth('raw_direction_loss'),
        path_unknown_trades=truth('path_dependent_outcome'),gap_or_zero_trades=truth('gap_or_zero_volume_path_unknown'),
        small_sample=n<30,mean_SL_distance_pct=mean([D(str(r['SL_distance_pct'])) for r in rows]),
        mean_TP_distance_pct=mean([D(str(r['TP_distance_pct'])) for r in rows]),
        mean_holding_hours=mean([D(str(r['holding_hours'])) for r in rows]))


def overlap_summary(rows):
    active=[];affected=set();pairs=maximum=0
    for row in sorted(rows,key=lambda r:(int(r['entry_time_ms']),r['event_id'])):
        t=int(row['entry_time_ms']);active=[(end,key) for end,key in active if end>t]
        if active:affected.add(row['event_id']);affected.update(key for _,key in active)
        pairs+=len(active);active.append((int(row['reserved_until_ms']),row['event_id']));maximum=max(maximum,len(active))
    return dict(pair_overlaps=pairs,events_with_overlap=len(affected),maximum_concurrent=maximum)


def percentile(values,q):
    if not values:return None
    values=sorted(values);index=(len(values)-1)*q;lo=int(index);hi=min(lo+1,len(values)-1)
    return values[lo]+(values[hi]-values[lo])*(index-lo)


def block_difference(a,b,start,end,reps=1000):
    if end<=start:raise ValueError('Invalid calendar')
    blocks=list(range((start-START)//BLOCK,(end-1-START)//BLOCK+1))
    def bucket(rows):
        grouped=defaultdict(lambda:[0.0,0])
        for r in rows:
            t=int(r['entry_time_ms'])
            if not start<=t<end:raise ValueError('Observation outside calendar')
            group=grouped[(t-START)//BLOCK];group[0]+=float(r['net_R']);group[1]+=1
        return grouped
    x,y=bucket(a),bucket(b);rng=random.Random(808);sa=[];sb=[];d=[]
    for _ in range(reps):
        counts=defaultdict(int)
        for _ in blocks:counts[rng.choice(blocks)]+=1
        def estimate(group):
            sums=sum(count*group[k][0] for k,count in counts.items() if k in group)
            n=sum(count*group[k][1] for k,count in counts.items() if k in group)
            return sums/n if n else None
        ra,rb=estimate(x),estimate(y)
        if ra is not None:sa.append(ra)
        if rb is not None:sb.append(rb)
        if ra is not None and rb is not None:d.append(ra-rb)
    def mean(rows):return sum(float(r['net_R']) for r in rows)/len(rows) if rows else None
    ma,mb=mean(a),mean(b)
    return dict(group_a_n=len(a),group_b_n=len(b),group_a_mean_R=ma,group_b_mean_R=mb,
        difference_R=ma-mb if ma is not None and mb is not None else None,
        group_a_low95=percentile(sa,.025),group_a_high95=percentile(sa,.975),
        group_b_low95=percentile(sb,.025),group_b_high95=percentile(sb,.975),
        difference_low95=percentile(d,.025),difference_high95=percentile(d,.975),
        calendar_blocks=len(blocks),group_a_nonempty_blocks=len(x),group_b_nonempty_blocks=len(y),
        undefined_resamples=reps-len(d),replications=reps,seed=808)


def hypothesis_eligible(rows):
    def find(period,universe='all_eight'):
        return next((r for r in rows if (r['period'],r['universe'],r['scenario'])==(period,universe,'source_stop_first')),None)
    for period in ('train','validation'):
        r=find(period)
        if not r or min(r['group_a_n'],r['group_b_n'])<30 or r['difference_R'] is None or r['difference_R']<=0:return False
    pooled=find('continuous')
    if not pooled or pooled['difference_low95'] is None or pooled['difference_low95']<=0:return False
    for period,universe in [('continuous','BTC'),('continuous','other_seven'),('opened_test','all_eight')]:
        r=find(period,universe)
        if not r or min(r['group_a_n'],r['group_b_n'])<30:return False
        if r['difference_R'] is None or r['difference_R']<=0:return False
    return True
