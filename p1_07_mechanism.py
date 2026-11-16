"""P1-07A: arithmetic mechanism audit of frozen P1-06B output, no strategy replay."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import itertools
import json
from collections import Counter
from decimal import Decimal as D, localcontext
from pathlib import Path
from zipfile import ZipFile


SOURCE_SHA='2b416875856defb20e714d366d3e4b61a22283cac8432e0f11131fdab00ece00'
SOURCE_COMMIT='df203cca3adbb37b11118c1725cf36cbc9fffe41'
H=3600000
MODELS=('fractional_spot','hypothetical_1x_long_short')
SCENARIOS=('source_stop_first','feasible_lower','feasible_upper','source_slip15')
PERIODS={'continuous':(1602720000000,1794787200000),'train':(1602720000000,1672531200000),
         'validation':(1672531200000,1735689600000),'opened_test':(1735689600000,1794787200000)}
SYMBOLS=('BTCUSDT','ETHUSDT','SOLUSDT','ZECUSDT','XRPUSDT','BNBUSDT','NEARUSDT','DOGEUSDT')
TOL=D('1e-18')


def sha(raw):return hashlib.sha256(raw).hexdigest()
def dec(value):return value if isinstance(value,D) else D(str(value))
def mean(values):return sum(values)/len(values) if values else None
def truth(value):return value is True or value=='True'


def write_csv(path,rows):
    fields=list(dict.fromkeys(k for r in rows for k in r)) or ['no_rows']
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)


def write_json(path,data):path.write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')


def cost_share(row):
    normal=dec(row['planned_risk']);q=dec(row['quantity'])
    if min(normal,q)<=0:raise ValueError('Nonpositive risk or quantity')
    return (normal-q*abs(dec(row['raw_entry'])-dec(row['stop'])))/normal


def outcome_values(row):
    q=dec(row['quantity']);risk=dec(row['planned_risk']);sign=1 if row['side']=='long' else -1
    gross=q*(dec(row['raw_exit'])-dec(row['raw_entry']))*sign
    cost=dec(row['fees_usdt'])+dec(row['slippage_usdt']);net=dec(row['net_pnl_usdt'])
    if abs(gross-cost-net)>TOL:raise ValueError('Archived cost identity mismatch')
    if min(q,risk)<=0:raise ValueError('Nonpositive archived risk or quantity')
    return dict(quantity=q,net=net,gross=gross,cost=cost,net_R=net/risk,gross_R=gross/risk,cost_R=cost/risk,
        normal_risk_cost_share=cost_share(row),hold_hours=D(int(row['exit_time_ms'])-int(row['entry_time_ms']))/H,
        stop_distance=abs(dec(row['raw_entry'])-dec(row['stop'])),
        path_uncertain=truth(row['path_dependent_outcome']),data_path_unknown=truth(row['gap_or_zero_volume_path_unknown']))


def common_effect(a,b):
    for key in ('event_id','symbol','side','entry_time_ms'):
        if str(a[key])!=str(b[key]):raise ValueError('Pair entry or target identity changed: '+key)
    for key in ('raw_entry','target'):
        if dec(a[key])!=dec(b[key]):raise ValueError('Pair entry or target changed: '+key)
    av,bv=outcome_values(a),outcome_values(b)
    ratio=av['quantity']/bv['quantity']
    gross_delta=bv['gross']*ratio-av['gross']
    cost_increase=bv['cost']*ratio-av['cost']
    fixed=av['quantity']*(bv['net']/bv['quantity'])-av['net']
    resize=(bv['quantity']-av['quantity'])*(bv['net']/bv['quantity'])
    delta=bv['net']-av['net']
    if abs(fixed+resize-delta)>TOL or abs(gross_delta-cost_increase-fixed)>TOL:
        raise ValueError('Common quantity decomposition identity mismatch')
    return dict(delta_net_USDT=delta,fixed_qA_gross_price_effect_USDT=gross_delta,
        fixed_qA_cost_increase_USDT=cost_increase,fixed_qA_outcome_effect_USDT=fixed,resize_effect_USDT=resize,
        quantity_ratio_S_to_A=bv['quantity']/av['quantity'],quantity_shrunk=bv['quantity']<av['quantity'],
        holding_hours_change=bv['hold_hours']-av['hold_hours'],holding_extended=bv['hold_hours']>av['hold_hours'],
        stop_widened=bv['stop_distance']>av['stop_distance'],A_hold_hours=av['hold_hours'],S_hold_hours=bv['hold_hours'],
        A_cost_share_of_normal_risk=av['normal_risk_cost_share'],S_cost_share_of_normal_risk=bv['normal_risk_cost_share'],
        A_net_R=av['net_R'],S_net_R=bv['net_R'],A_gross_R=av['gross_R'],S_gross_R=bv['gross_R'],
        A_cost_R=av['cost_R'],S_cost_R=bv['cost_R'])


def population_details(aplans,bplans,ares,bres,model,scenario):
    if set(aplans)!=set(bplans):raise ValueError('Source opportunity sets do not match')
    if not set(ares).issubset(aplans) or not set(bres).issubset(bplans):raise ValueError('Outcome absent from opportunity universe')
    rows=[]
    for key,p in aplans.items():
        bp=bplans[key]
        if any(p[x]!=bp[x] for x in ('symbol','side','registration_ms')):raise ValueError('Opportunity identity changed')
        supported=p['side']=='long' or model=='hypothetical_1x_long_short'
        a=ares.get(key) if supported else None;b=bres.get(key) if supported else None
        cls='both_filled' if a and b else 'A_only' if a else 'S_only' if b else 'neither_filled' if supported else 'unsupported_direction'
        av=outcome_values(a) if a else None;bv=outcome_values(b) if b else None
        an=av['net'] if av else D(0);bn=bv['net'] if bv else D(0)
        row=dict(model=model,scenario=scenario,event_id=key,symbol=p['symbol'],side=p['side'],
            registration_ms=int(p['registration_ms']),A_status=p['status'],S_status=bp['status'],fill_class=cls,
            direction_supported=supported,A_net_USDT=an,S_net_USDT=bn,delta_net_USDT=bn-an,
            common_fill_delta_USDT=D(0),exclusion_effect_USDT=-an if cls=='A_only' else D(0),
            new_fill_effect_USDT=bn if cls=='S_only' else D(0),
            fixed_qA_gross_price_effect_USDT=D(0),fixed_qA_cost_increase_USDT=D(0),
            fixed_qA_outcome_effect_USDT=D(0),resize_effect_USDT=D(0))
        for name,v,source in (('A',av,a),('S',bv,b)):
            row[name+'_filled']=v is not None
            for field in ('net_R','gross_R','cost_R','normal_risk_cost_share','hold_hours','quantity'):
                row[name+'_'+field]=v[field] if v else None
            row[name+'_stop_exit']=bool(source and source['exit_reason'] in ('stop','stop_gap'))
            row[name+'_path_uncertain']=v['path_uncertain'] if v else False
            row[name+'_data_path_unknown']=v['data_path_unknown'] if v else False
        if a and b:
            row.update(common_effect(a,b));row['common_fill_delta_USDT']=bn-an
        if abs(row['common_fill_delta_USDT']+row['exclusion_effect_USDT']+row['new_fill_effect_USDT']-row['delta_net_USDT'])>TOL:
            raise ValueError('Opportunity attribution identity mismatch')
        rows.append(row)
    return rows


def summarize(rows):
    common=[r for r in rows if r['fill_class']=='both_filled'];aonly=[r for r in rows if r['fill_class']=='A_only']
    def total(field):return sum((r[field] for r in rows),D(0))
    n=len(rows)
    result=dict(opportunities=n,direction_supported_opportunities=sum(r['direction_supported'] for r in rows),
        both_filled=len(common),A_only=len(aonly),S_only=sum(r['fill_class']=='S_only' for r in rows),
        neither_filled=sum(r['fill_class']=='neither_filled' for r in rows),unsupported_direction=sum(r['fill_class']=='unsupported_direction' for r in rows),
        A_only_RR_rejections=sum(r['S_status']=='net_RR_below_1.5' for r in aonly),
        total_delta_USDT=total('delta_net_USDT'),common_fill_delta_USDT=total('common_fill_delta_USDT'),
        exclusion_effect_USDT=total('exclusion_effect_USDT'),new_fill_effect_USDT=total('new_fill_effect_USDT'),
        fixed_qA_gross_price_effect_USDT=total('fixed_qA_gross_price_effect_USDT'),
        fixed_qA_cost_increase_USDT=total('fixed_qA_cost_increase_USDT'),
        fixed_qA_outcome_effect_USDT=total('fixed_qA_outcome_effect_USDT'),resize_effect_USDT=total('resize_effect_USDT'),
        delta_USDT_per_opportunity=total('delta_net_USDT')/n if n else None,
        common_quantity_shrunk=sum(r['quantity_shrunk'] for r in common),
        common_holding_extended=sum(r['holding_extended'] for r in common),common_stop_widened=sum(r['stop_widened'] for r in common),
        common_mean_quantity_ratio=mean([r['quantity_ratio_S_to_A'] for r in common]),
        common_mean_holding_hours_change=mean([r['holding_hours_change'] for r in common]))
    for prefix in ('A','S'):
        filled=[r for r in rows if r[prefix+'_filled']]
        result[prefix+'_filled']=len(filled)
        result[prefix+'_wins']=sum(r[prefix+'_net_USDT']>0 for r in filled)
        result[prefix+'_losses']=sum(r[prefix+'_net_USDT']<0 for r in filled)
        result[prefix+'_win_rate_pct']=D(result[prefix+'_wins'])*100/len(filled) if filled else None
        result[prefix+'_stop_exits']=sum(r[prefix+'_stop_exit'] for r in filled)
        result[prefix+'_stop_rate_pct']=D(result[prefix+'_stop_exits'])*100/len(filled) if filled else None
        result[prefix+'_net_total_USDT']=sum((r[prefix+'_net_USDT'] for r in filled),D(0))
        for metric in ('net_R','gross_R','cost_R','normal_risk_cost_share','hold_hours'):
            result[prefix+'_mean_'+metric]=mean([r[prefix+'_'+metric] for r in filled])
            result['common_'+prefix+'_mean_'+metric]=mean([r[prefix+'_'+metric] for r in common])
        result[prefix+'_path_uncertain']=sum(r[prefix+'_path_uncertain'] for r in filled)
        result[prefix+'_data_path_unknown']=sum(r[prefix+'_data_path_unknown'] for r in filled)
    if abs(result['total_delta_USDT']-result['common_fill_delta_USDT']-result['exclusion_effect_USDT']-result['new_fill_effect_USDT'])>TOL:
        raise ValueError('Aggregate attribution identity mismatch')
    return result


def csv_read(z,name,hashes,policies=True):
    raw=z.read(name);hashes[name]=sha(raw)
    rows=csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
    return [r for r in rows if not policies or r['policy'] in ('A','S_ATR1')]


def universe_members():
    return dict(all_eight=set(SYMBOLS),BTC={'BTCUSDT'},other_seven=set(SYMBOLS)-{'BTCUSDT'},**{s:{s} for s in SYMBOLS})


def portfolio_overlaps(z,hashes):
    results=[]
    for model,period,scenario in itertools.product(MODELS,PERIODS,SCENARIOS):
        data={}
        for policy in ('A','S_ATR1'):
            name=f'results/runs/{model}__{period}__{scenario}__{policy}__trades.csv'
            rows=csv_read(z,name,hashes)
            data[policy]={r['event_id']:r for r in rows}
        for universe,members in universe_members().items():
            a={k:r for k,r in data['A'].items() if r['symbol'] in members};b={k:r for k,r in data['S_ATR1'].items() if r['symbol'] in members}
            common=set(a)&set(b)
            results.append(dict(model=model,period=period,scenario=scenario,universe=universe,
                A_actual_trades=len(a),S_actual_trades=len(b),common_selected_events=len(common),
                A_only_actual_selection=len(set(a)-set(b)),S_only_actual_selection=len(set(b)-set(a)),
                A_actual_pnl_USDT=sum((dec(r['net_pnl_usdt']) for r in a.values()),D(0)),
                S_actual_pnl_USDT=sum((dec(r['net_pnl_usdt']) for r in b.values()),D(0)),
                A_mean_quantity=mean([dec(r['quantity']) for r in a.values()]),S_mean_quantity=mean([dec(r['quantity']) for r in b.values()]),
                common_actual_quantity_ratio=mean([dec(b[k]['quantity'])/dec(a[k]['quantity']) for k in common]),
                interpretation='不同已選交易及動態本金，不可把孤立事件分解套成實際帳戶因果歸因'))
    return results


def chinese_report(out,summaries,accounts):
    fmt=lambda x:'—' if x is None else f'{float(x):.6f}'
    lines=['# P1-07A｜S_ATR1凍結改善機制：封存算術分析','',
        '**沒有重跑A或P1-06B六個候選，沒有新增停損、退出、參數或交易。所有結果直接讀取固定Artifact。**','',
        '## 方法與分母','',
        '母體為原A的4583個登記事件，含未觸及、拒絕及現貨不可用空頭。下列全部機會平均含未交易0。兩模型、四時段、5bps／15bps成本及三個原路徑情境完整保留；連續與分段會重複歷史，不是獨立樣本。',
        '總零含入∆USDT = 共同成交效果 + 僅A成交之排除效果 + 僅S成交效果。共同成交效果再拆為固定qA的退出價差／成本變化，以及(qS−qA)乘S每單位淨PnL之重新部位化項。每列及彙總都核對恆等式。',
        '**固定qA分解是事後算術，可能超過新SL的風險預算，不是可執行資金模型。持倉時間延長與退出價改變同屬SL干預，不能把兩者識別為獨立因果效果。**','',
        '| 模型／範圍 | 機會 | 共同成交 | RR等排除 | 總∆USDT | 共同成交∆ | 排除效果 | 固定qA效果 | 重新q項 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in summaries:
        if r['period']=='continuous' and r['scenario']=='source_stop_first' and r['registration_year']=='all' and r['universe'] in ('all_eight','BTC','other_seven'):
            lines.append(f"| {r['model']}／{r['universe']} | {r['opportunities']} | {r['both_filled']} | {r['A_only']} | {fmt(r['total_delta_USDT'])} | {fmt(r['common_fill_delta_USDT'])} | {fmt(r['exclusion_effect_USDT'])} | {fmt(r['fixed_qA_outcome_effect_USDT'])} | {fmt(r['resize_effect_USDT'])} |")
    def primary(universe):
        return next(r for r in summaries if (r['model'],r['period'],r['scenario'],r['universe'],r['registration_year'])==('fractional_spot','continuous','source_stop_first',universe,'all'))
    main,other,btc=primary('all_eight'),primary('other_seven'),primary('BTC')
    share=main['exclusion_effect_USDT']/main['total_delta_USDT']*100 if main['total_delta_USDT'] else None
    lines+=['',f"純多全期孤立A可成交{main['A_filled']}個、S為{main['S_filled']}個；{main['A_only_RR_rejections']}個A-only事件被凍結的淨RR門檻排除。排除效果占總零含入USDT改善{fmt(share)}%，共同成交效果為{fmt(main['common_fill_delta_USDT'])} USDT，不能只將全部改善歸給SL位置。",
        f"更重要的是異質性：其他七幣共同{other['both_filled']}個事件的總效果為{fmt(other['common_fill_delta_USDT'])} USDT，排除{other['A_only']}個的效果為{fmt(other['exclusion_effect_USDT'])}；BTC共同{btc['both_filled']}個效果為{fmt(btc['common_fill_delta_USDT'])}，排除{btc['A_only']}個效果為{fmt(btc['exclusion_effect_USDT'])}。若只報全期平均，會隱藏其他七幣共同成交子集未改善的事實。"]
    lines+=['','## 成本、部位與持有時間（共同成交子集）','',
        '| 模型／範圍 | n | q縮小 | 持有延長 | A→S平均h | A→S淨R | A→S原價差/R | A→S成本/R | A→S正常風險成本比% |',
        '|---|---:|---:|---:|---|---|---|---|---|']
    for r in summaries:
        if r['period']=='continuous' and r['scenario']=='source_stop_first' and r['registration_year']=='all' and r['universe'] in ('all_eight','BTC','other_seven'):
            fields=[]
            for metric in ('hold_hours','net_R','gross_R','cost_R'):
                fields.append(fmt(r['common_A_mean_'+metric])+'→'+fmt(r['common_S_mean_'+metric]))
            a=r['common_A_mean_normal_risk_cost_share'];b=r['common_S_mean_normal_risk_cost_share']
            fields.append(fmt(a*100 if a is not None else None)+'→'+fmt(b*100 if b is not None else None))
            lines.append(f"| {r['model']}／{r['universe']} | {r['both_filled']} | {r['common_quantity_shrunk']} | {r['common_holding_extended']} | "+' | '.join(fields)+' |')
    lines+=['','## 如何解讀凍結改善','',
        '- SL位置、排除事件、重新部位化與時間佔用同時改變。排除效果可直接核算哪些原事件被RR門檻刪掉；不是AI選股或獨立市場預測能力。共同成交子集避免把全部改善都歸於少交易，但仍是固定OHLC成交假設的反事實。',
        '- 淨R的分母是每筆正常含成本風險。SL加寬後，成本占分母比重降低可使淨R改善，即使原價差/R沒有改善；不代表真實手續費消失。',
        '- 其他七幣與BTC必須分開。若其他七幣共同成交效果為負、改善主要靠排除負報酬事件，不能把全期結果當成SL位置普遍有效。',
        '- 原始已選帳戶的部位還受先前PnL、單倉及資金限制影響。portfolio_overlap.csv列出不同選樣，沒有將孤立事件算術偽裝成實際帳戶因果效果。','',
        '## 凍結帳戶結果（直接複製來源，不重播）','',
        '| 模型 | 時段 | 成本／路徑 | 政策 | n | SL% | 勝率% | 淨R | PF | 終值 | MDD% |',
        '|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for r in accounts:
        lines.append('| '+' | '.join(str(r[k]) for k in ('model','period','scenario','policy','trade_count','stop_exit_rate_pct','win_rate_pct','average_R','profit_factor','final_equity_USDT','maximum_drawdown_pct'))+' |')
    test={r['policy']:r for r in accounts if (r['model'],r['period'],r['scenario'])==('fractional_spot','opened_test','source_stop_first')}
    lines+=['',f"已開啟Test的S_ATR1為{test['S_ATR1']['trade_count']}筆、平均淨R {test['S_ATR1']['average_R']}、PF {test['S_ATR1']['profit_factor']}；MDD從A的{fmt(test['A']['maximum_drawdown_pct'])}%上升到{fmt(test['S_ATR1']['maximum_drawdown_pct'])}%。全期相對改善並未通過跨時段風險與正期望驗收。"]
    lines+=['','## 限制與保留的證據','',
        'S_ATR1仍未被證明有正期望。Test、其他七幣及15bps壓力的失敗保留；1H棒內路徑／深度／實際最小委託與借幣資料仍缺。所有Test已開啟，沒有新OOS；不選最佳ATR倍數或篩選最佳幣種。',
        'paired_details.csv保留全部機會及每事件分解；mechanism_summary.csv按兩模型、四時段、四既有成本路徑情境、BTC／其他七幣／各幣、登記年度完整分層；exclusion_reasons.csv列出刪失來源；frozen_portfolio_accounts.csv及portfolio_overlap.csv保留原帳戶選樣。manifest記錄來源、程式與輸出SHA256及算術核對。','']
    (out/'P1_07A_MECHANISM_ZH.md').write_text('\n'.join(lines),encoding='utf-8')


def run(source_zip:Path,out:Path):
    source_zip,out=Path(source_zip),Path(out)
    if out.exists() and any(out.iterdir()):raise ValueError('Output directory must be empty')
    if sha(source_zip.read_bytes())!=SOURCE_SHA:raise ValueError('Pinned P1-06B ZIP SHA256 mismatch')
    out.mkdir(parents=True,exist_ok=True)
    with localcontext() as ctx:
        ctx.prec=60
        hashes={}
        with ZipFile(source_zip) as z:
            prior=json.loads(z.read('results/p1_06b_manifest.json'))
            if prior['current_commit']!=SOURCE_COMMIT:raise ValueError('Pinned P1-06B source commit mismatch')
            plans=csv_read(z,'results/candidate_events.csv',hashes)
            bypolicy={p:{} for p in ('A','S_ATR1')}
            for r in plans:
                if r['event_id'] in bypolicy[r['policy']]:raise ValueError('Duplicate source opportunity')
                bypolicy[r['policy']][r['event_id']]=r
            outcomes=csv_read(z,'results/event_outcomes.csv',hashes)
            byout={(p,s):{} for p,s in itertools.product(('A','S_ATR1'),SCENARIOS)}
            for r in outcomes:
                index=byout[(r['policy'],r['scenario'])]
                if r['event_id'] in index:raise ValueError('Duplicate source outcome')
                index[r['event_id']]=r
            details=[];summaries=[];exclusions=[]
            for model,scenario in itertools.product(MODELS,SCENARIOS):
                rows=population_details(bypolicy['A'],bypolicy['S_ATR1'],byout[('A',scenario)],byout[('S_ATR1',scenario)],model,scenario)
                details.extend(rows)
                for period,(start,end) in PERIODS.items():
                    part=[r for r in rows if start<=r['registration_ms']<end]
                    for universe,members in universe_members().items():
                        selected=[r for r in part if r['symbol'] in members]
                        for year in ('all',*map(str,range(2020,2027))):
                            from datetime import datetime,timezone
                            sub=selected if year=='all' else [r for r in selected if str(datetime.fromtimestamp(r['registration_ms']/1000,timezone.utc).year)==year]
                            summaries.append(dict(model=model,period=period,scenario=scenario,universe=universe,registration_year=year,**summarize(sub)))
                        only=[r for r in selected if r['fill_class']=='A_only']
                        for reason in sorted({p['status'] for p in bypolicy['S_ATR1'].values()}):
                            subset=[r for r in only if r['S_status']==reason]
                            exclusions.append(dict(model=model,period=period,scenario=scenario,universe=universe,reason=reason,
                                excluded_events=len(subset),A_pnl_USDT=sum((r['A_net_USDT'] for r in subset),D(0)),
                                exclusion_effect_USDT=-sum((r['A_net_USDT'] for r in subset),D(0)),
                                A_wins=sum(r['A_net_USDT']>0 for r in subset),A_stops=sum(r['A_stop_exit'] for r in subset)))
            accounts=csv_read(z,'results/portfolio_comparison.csv',hashes)
            overlaps=portfolio_overlaps(z,hashes)
        for name,rows in (('paired_details.csv',details),('mechanism_summary.csv',summaries),('exclusion_reasons.csv',exclusions),
                          ('frozen_portfolio_accounts.csv',accounts),('portfolio_overlap.csv',overlaps)):
            write_csv(out/name,rows)
        chinese_report(out,summaries,accounts)
        main=next(r for r in summaries if (r['model'],r['period'],r['scenario'],r['universe'],r['registration_year'])==('fractional_spot','continuous','source_stop_first','all_eight','all'))
        manifest=dict(version='p1-07a-frozen-mechanism-v1',source_zip_sha256=SOURCE_SHA,source_commit=SOURCE_COMMIT,
            no_strategy_replay=True,no_parameter_change=True,new_OOS=False,live_execution=False,
            archived_registration_episodes=len(bypolicy['A']),paired_detail_rows=len(details),summary_rows=len(summaries),
            identity_tolerance_USDT=str(TOL),all_arithmetic_identities_pass=True,primary_summary=main,
            source_file_sha256=hashes,code_sha256=sha(Path(__file__).read_bytes()),
            output_sha256={p.name:sha(p.read_bytes()) for p in sorted(out.iterdir()) if p.is_file()})
        write_json(out/'manifest.json',manifest)
        return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source-zip',type=Path,required=True);parser.add_argument('--out-dir',type=Path,required=True)
    args=parser.parse_args();run(args.source_zip,args.out_dir)
