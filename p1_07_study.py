"""P1-07: archived mechanism, two new daily hypotheses and fixed meta gates."""
import argparse,csv,hashlib,io,itertools,json,os,tempfile
from pathlib import Path
from zipfile import ZipFile
from decimal import Decimal as D,localcontext
import p1_candlestick_research as core
import p1_portfolio as old
import p1_stop_candidates as engine
import p1_stop_study as previous
import p1_loss_study as files
import p1_07_signals as signals
import p1_07_mechanism as mechanism
import p1_07_ai_audit as ai

SOURCE_SHA='2b416875856defb20e714d366d3e4b61a22283cac8432e0f11131fdab00ece00'
PROTOCOL_SHA='931839144a17fef7e793a845d15b380dbfa57e0d591308d6aae91cb0400805f7'
PERIODS=previous.PERIODS
MODELS=previous.MODELS

def read(z,name,policy=None):
    with z.open(name) as f:
        return [r for r in csv.DictReader(io.TextIOWrapper(f,encoding='utf-8-sig')) if policy is None or r.get('policy')==policy]

def archived_plan(row):
    row=dict(row)
    for key in ('registration_ms','entry_time_ms','anchor_ms','signal_time_ms','feature_cutoff_ms'):
        if key in row:row[key]=int(row[key]) if row[key] else None
    for key in ('raw_entry','stop','target','SL_distance_pct','TP_distance_pct','planned_net_RR','entry_delay_hours','entry_drift_pct'):
        if row.get(key):row[key]=D(row[key])
    return row

def archived_paths(rows):
    paths={}
    for r in rows:
        path=paths.setdefault(r['event_id'],dict(path_dependent_outcome=r['path_dependent_outcome']=='True',gap_or_zero_volume_path_unknown=r['gap_or_zero_volume_path_unknown']=='True'))
        path[r['scenario']]=dict(exit_time_ms=int(r['exit_time_ms']),exit_time_utc=r['exit_time_utc'],exit_bar_ms=int(r['exit_bar_ms']),reserved_until_ms=int(r['reserved_until_ms']),raw_exit=D(r['raw_exit']),exit_reason=r['exit_reason'])
    return paths

def portfolio_batch(out,plans,paths,assets,policies,models,baseline):
    accounts=[];breakdowns=[];audits=[]
    for model,(period,(start,end)),scenario in itertools.product(models,PERIODS.items(),engine.SCENARIOS):
        chart={}
        for policy in policies:
            print('NEW_ACCOUNT',model,period,scenario,policy,flush=True)
            r=engine.ledger(plans,paths,policy,model,scenario,start,end,assets);chart[policy]=r
            audits.append(dict(model=model,period=period,scenario=scenario,policy=policy,**previous.audit_account(r)))
            row=dict(model=model,period=period,scenario=scenario,policy=policy,calendar_start_utc=old.utc(start),calendar_end_utc=old.utc(end),
                final_equity_USDT=str(r['final_equity']),maximum_drawdown_pct=str(r['maximum_drawdown_pct']),exposure_percent=100*r['in_market_hours']/((end-start)/engine.H),
                **previous.summary(r['trades'],r['opportunities']))
            base=next(b for b in baseline if (b['model'],b['period'],b['scenario'],b['policy'])==(model,period,scenario,'A'))
            valid=row['trade_count']>=30 and int(base['trade_count'])>0
            row['joint_improvement_vs_archived_A']=bool(valid and row['stop_exit_rate_pct']<float(base['stop_exit_rate_pct']) and row['win_rate_pct']>float(base['win_rate_pct']) and row['average_R']>float(base['average_R']) and row['profit_factor'] is not None and row['profit_factor']>=float(base['profit_factor']) and D(row['maximum_drawdown_pct'])<=D(base['maximum_drawdown_pct']))
            row['positive_expectancy']=bool(row['trade_count'] and row['average_R']>0)
            row['comparison_is_not_causal']=True;accounts.append(row)
            prefix=out/'runs'/f'{model}__{period}__{scenario}__{policy}'
            for name,items in [('trades',r['trades']),('decisions',r['decisions']),('equity',r['curve'])]:files.write_csv(prefix.with_name(prefix.name+'__'+name+'.csv'),items)
            local=[p for p in plans if p['policy']==policy and start<=p['registration_ms']<end]
            categories={'universe':list(files.universes(assets)), 'year':[str(y) for y in range(2020,2027)]}
            def match(p,dim,cat):return p['symbol'] in files.universes(assets)[cat] if dim=='universe' else old.utc(p['registration_ms'])[:4]==cat
            for dim,labels in categories.items():
                for cat in labels:
                    ps=[p for p in local if match(p,dim,cat)];ts=[t for t in r['trades'] if match(t,dim,cat)]
                    breakdowns.append(dict(model=model,period=period,scenario=scenario,policy=policy,dimension=dim,category=cat,**previous.summary(ts,len(ps))))
        previous.draw(out/f'equity_{model}__{period}__{scenario}__{policies[0]}.png',chart,model,period,scenario)
    return accounts,breakdowns,audits

def run(source_zip,out):
    with localcontext() as ctx:
        ctx.prec=50
        return _run(source_zip,out)

def _run(source_zip,out):
    if out.exists() and any(out.iterdir()):raise ValueError('Output must be empty')
    root=Path(__file__).parent
    if files.sha((root/'P1_07_PROTOCOL.json').read_bytes())!=PROTOCOL_SHA:raise ValueError('Fixed P1-07 protocol changed')
    if files.sha(source_zip.read_bytes())!=SOURCE_SHA:raise ValueError('Source archive changed')
    out.mkdir(parents=True,exist_ok=True)
    print('READ_ONLY_FROZEN_MECHANISM',flush=True);mech=mechanism.run(source_zip,out/'mechanism')
    print('AUDIT_THEN_FROZEN_META',flush=True);meta=ai.run(source_zip,out/'ai')
    with ZipFile(source_zip) as z, tempfile.TemporaryDirectory(prefix='p1-07-') as temp:
        source_manifest=json.loads(z.read('results/p1_06b_manifest.json'))
        if source_manifest['current_commit']!='df203cca3adbb37b11118c1725cf36cbc9fffe41':raise ValueError('Wrong source commit')
        for name,digest in source_manifest['code_sha256'].items():
            if files.sha((root/name).read_bytes())!=digest:raise ValueError('P1-06B code changed')
        baseline=read(z,'results/portfolio_comparison.csv')
        original=[archived_plan(p) for p in read(z,'results/candidate_events.csv','A')]
        old_paths=archived_paths(read(z,'results/event_outcomes.csv','A'))
        with ZipFile(io.BytesIO(z.read('inputs/p1-06a-source.zip'))) as p106a:
            with ZipFile(io.BytesIO(p106a.read('inputs/p1-05-source.zip'))) as p105:
                lock=core.read_lock(root/'PROSPECTIVE_LOCK_V1.json');temp=Path(temp);data=temp/'data';tapes=temp/'tapes';data.mkdir();tapes.mkdir()
                with ZipFile(io.BytesIO(p105.read('inputs/frozen-source.zip'))) as raw:
                    for name,info in lock['frozen_data_prefixes'].items():
                        content=raw.read(name)
                        if files.sha(content)!=info['sha256']:raise ValueError('Frozen SHA256 changed')
                        (data/name).write_bytes(content)
                for symbol in lock['symbols']:(tapes/(symbol+'.json.gz')).write_bytes(p105.read('inputs/tapes/'+symbol+'.json.gz'))
        assets={s:old.Asset(s,data,tapes) for s in lock['symbols']}
        plans=[];paths={};available=[]
        for a in assets.values():
            engine.prepare_context(a);print('NEW_20D_SIGNALS',a.symbol,flush=True)
            rows,counts=signals.generate(a);plans.extend(rows);available.append(dict(symbol=a.symbol,**counts))
            view=signals.execution_view(a)
            for p in rows:
                if p['status']=='eligible':paths[p['event_id']+'|'+p['policy']]=engine.outcome_paths(p,view)
        files.write_csv(out/'new_signal_events.csv',plans);files.write_csv(out/'reference_availability.csv',available)
        outcomes=[]
        for p in plans:
            if p['status']=='eligible':
                for scenario in engine.SCENARIOS:outcomes.append(previous.isolated(p,paths[p['event_id']+'|'+p['policy']],scenario))
        files.write_csv(out/'new_isolated_outcomes.csv',outcomes)
        pairs=[]
        for model,(period,(start,end)),scenario in itertools.product(MODELS,PERIODS.items(),engine.SCENARIOS):
            ids={p['event_id'] for p in plans if start<=p['registration_ms']<end}
            group={policy:{p['event_id']:p for p in outcomes if p['policy']==policy and p['scenario']==scenario and start<=p['registration_ms']<end and (p['side']=='long' or model=='hypothetical_1x_long_short')} for policy in signals.POLICIES}
            a,b=(group[k] for k in signals.POLICIES);common=set(a)&set(b)
            pairs.append(dict(model=model,period=period,scenario=scenario,raw_events=len(ids),fail_filled=len(a),cont_filled=len(b),both_filled=len(common),
                delta_FAIL_minus_CONT_USDT_per_opportunity=float(sum((a[k]['net_pnl_usdt'] if k in a else D(0))-(b[k]['net_pnl_usdt'] if k in b else D(0)) for k in ids)/len(ids)) if ids else None,
                common_delta_R=float(sum(a[k]['r_multiple']-b[k]['r_multiple'] for k in common)/len(common)) if common else None,
                causal_effect_identified=False))
        files.write_csv(out/'new_same_event_pairs.csv',pairs)
        accounts,breakdowns,audits=portfolio_batch(out,plans,paths,assets,signals.POLICIES,MODELS,baseline)
        predictions=list(csv.DictReader((out/'ai'/'meta_predictions.csv').open(encoding='utf-8-sig')))
        pred={r['event_id']:r for r in predictions};gateplans=[];gatepaths={}
        for policy,flag in [('ML_GATE','accept_ml'),('FIXED_COST25','accept_fixed')]:
            for original_plan in original:
                p=dict(original_plan,policy=policy)
                if p['status']=='eligible':
                    prediction=pred.get(p['event_id'])
                    if p['side']!='long':p['status']='spot_short_unavailable'
                    elif not ai.accept_for_entry(prediction,flag,p['entry_time_ms']):p['status']='meta_watch_or_rejected'
                    # Future ambiguity flags never participate in this admission gate.
                    if p['status']=='eligible':gatepaths[p['event_id']+'|'+policy]=old_paths[p['event_id']]
                gateplans.append(p)
        files.write_csv(out/'meta_gate_opportunities.csv',gateplans)
        aa,bb,cc=portfolio_batch(out,gateplans,gatepaths,assets,('ML_GATE','FIXED_COST25'),('fractional_spot',),baseline)
        accounts+=aa;breakdowns+=bb;audits+=cc
        files.write_csv(out/'portfolio_comparison.csv',accounts);files.write_csv(out/'portfolio_breakdowns.csv',breakdowns)
        files.write_csv(out/'archived_A_S_ATR1_reference.csv',[r for r in baseline if r['policy'] in ('A','S_ATR1')]);files.write_json(out/'account_audit.json',audits)
        qualified=[]
        for policy in (*signals.POLICIES,'ML_GATE','FIXED_COST25'):
            ok=True
            for period in ('continuous','opened_test'):
                r=next(x for x in accounts if (x['model'],x['period'],x['scenario'],x['policy'])==('fractional_spot',period,'source_stop_first',policy))
                ok=ok and r['joint_improvement_vs_archived_A'] and r['positive_expectancy'] and (r['profit_factor'] or 0)>1 and D(r['final_equity_USDT'])>1000
            for category in ('BTC','other_seven'):
                r=next(x for x in breakdowns if (x['model'],x['period'],x['scenario'],x['policy'],x['dimension'],x['category'])==('fractional_spot','continuous','source_stop_first',policy,'universe',category))
                ok=ok and r['trade_count']>0 and r['average_R']>0
            r=next(x for x in accounts if (x['model'],x['period'],x['scenario'],x['policy'])==('fractional_spot','continuous','source_slip15',policy))
            ok=ok and r['trade_count']>0 and r['average_R']>=0
            if ok:qualified.append(policy)
        chinese_report(out,accounts,breakdowns,baseline,qualified,meta)
        manifest=dict(version='p1-07-signal-quality-v1',current_commit=os.getenv('GITHUB_SHA'),protocol_sha256=PROTOCOL_SHA,source_zip_sha256=SOURCE_SHA,
            frozen_source_sha256=source_manifest['frozen_source_sha256'],frozen_candle_count=source_manifest['frozen_candle_count'],
            new_signal_opportunity_rows=len(plans),new_isolated_outcome_rows=len(outcomes),new_account_runs=len(accounts),old_six_strategy_replays=0,
            qualified_forward_candidates=qualified,meta_trained=meta['model_trained'],new_OOS=False,live_execution=False,
            code_sha256={name:files.sha((root/name).read_bytes()) for name in ('p1_07_signals.py','p1_07_study.py','p1_07_mechanism.py','p1_07_ai_audit.py')},
            output_sha256={str(p.relative_to(out)):files.sha(p.read_bytes()) for p in sorted(out.rglob('*')) if p.is_file()})
        files.write_json(out/'p1_07_manifest.json',manifest);return manifest

def chinese_report(out,accounts,groups,baseline,qualified,meta):
    fmt=lambda v:'—' if v is None else f'{float(v):.6f}'
    lines=['# P1-07｜交易訊號品質與正期望研究完整實測','',
        '先提交361c47c固定規格，再進行新績效。S_ATR1與六個舊方案沒有重播或調參；機制報告在mechanism，AI稽核與模型在ai。所有Test已開啟，結果是歷史探索，沒有新的獨立OOS或實盤成交認證。','',
        '## P1-07A：保留什麼證據','',
        '完整機制分解見mechanism/P1_07A_MECHANISM_ZH.md。共同成交與RR刪失、固定q與重新部位化、價差與成本/R分母分開；不能把持有期延長當成已識別的獨立原因，也不能把孤立事件損益相加當本金。','',
        '## P1-07B/C：全部新增帳戶','',
        '20日必須是真正前20根已收盤1D，不是20小時。兩新假設共用首次突破母體；未收復案例保留。SL只看確認時已知日內極值，不用當日最終日線；兩臂可在同一事件不同時間形成訊號，不宣稱因果效果。','',
        'AI只使用Train201等門檻合格的條件紙上標籤；未知label不得用來事前避開測試交易。Meta母體不是已成交子集，原4583機會與1090合資格全部稽核，525long均可接受或觀望。','']
    for model in MODELS:
        lines += [f'### {model}｜全期主成本情境','', '| 方案 | 機會／成交 | 未交易% | SL率% | 淨虧損率% | 勝率% | 淨R | PF | 最終USDT | MDD% | 在市% |', '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
        for r in accounts:
            if (r['model'],r['period'],r['scenario'])==(model,'continuous','source_stop_first'):
                lines.append(f"| {r['policy']} | {r['opportunities']}／{r['trade_count']} | {fmt(r['untraded_ratio_pct'])} | {fmt(r['stop_exit_rate_pct'])} | {fmt(r['net_loss_rate_pct'])} | {fmt(r['win_rate_pct'])} | {fmt(r['average_R'])} | {fmt(r['profit_factor'])} | {fmt(r['final_equity_USDT'])} | {fmt(r['maximum_drawdown_pct'])} | {fmt(r['exposure_percent'])} |")
        for period in ('train','validation','opened_test'):
            lines += ['',f'### {model}｜{period}：5bps與15bps','', '| 方案 | 情境 | n | SL率% | 勝率% | 淨R | PF | 最終USDT | MDD% |', '|---|---|---:|---:|---:|---:|---:|---:|---:|']
            for r in accounts:
                if r['model']==model and r['period']==period and r['scenario'] in ('source_stop_first','source_slip15'):
                    lines.append(f"| {r['policy']} | {r['scenario']} | {r['trade_count']} | {fmt(r['stop_exit_rate_pct'])} | {fmt(r['win_rate_pct'])} | {fmt(r['average_R'])} | {fmt(r['profit_factor'])} | {fmt(r['final_equity_USDT'])} | {fmt(r['maximum_drawdown_pct'])} |")
        lines += ['',f'### {model}｜BTC與其他七幣分開','', '| 方案 | 範圍 | n | SL率% | 勝率% | 淨R | PF |', '|---|---|---:|---:|---:|---:|---:|']
        for r in groups:
            if r['model']==model and r['period']=='continuous' and r['scenario']=='source_stop_first' and r['dimension']=='universe' and r['category'] in ('BTC','other_seven'):
                lines.append(f"| {r['policy']} | {r['category']} | {r['trade_count']} | {fmt(r['stop_exit_rate_pct'])} | {fmt(r['win_rate_pct'])} | {fmt(r['average_R'])} | {fmt(r['profit_factor'])} |")
    lines += ['', '## 判定與限制','',f'通過事前嚴格正期望與風險／跨幣／Test／15bps門檻候選：{", ".join(qualified) or "0個"}。即使歷史通過也需新前瞻資料，不能宣稱可交易優勢。',
        'SL降低、分類accuracy較高或保留1000本金不能單獨算成功。ML若拒絕所有機會，勝率與淨R未定義；不降低0.5閾值補交易。固定成本篩選仍需與原A的完整帳戶而非不同結束日餘額比較。',
        'Wyckoff完整range/phase/secondary test尚無凍結標籤，第一輪沒有第三新策略；Turtle Soup採20日價位之1H收盤改編，固定2R/72h、沒有原版ticks與trailing/reentry，不冒稱原版績效。R3文件未取得，未假稱其協議。',
        '因果修正：原固定規格下一根零量拒絕子句不可在開盤知道，先提交99c693b失敗測試後移除未來成交量入場篩選；開盤後零量僅標記路徑不確定。原protocol逐字保留，修正說明見P1_07_ACCEPTANCE.md。',
        '有限OHLC可行悲觀／樂觀是每事件條件情境，完整帳戶不是全局數學上下界。原料缺口、零量與歷史交易規則／深度／借幣未知保留，2%正常模型風險不保證實損上限；無新OOS、iid顯著性或真實執行認證。',
        '全部原始候選／拒絕、共同事件孤立配對、96組新帳戶、完整CSV、曲線PNG、程式／來源／輸出SHA256與封存來源ZIP均保留；沒有重跑六個舊方案。','']
    (out/'P1_07_RESULT_ZH.md').write_text('\n'.join(lines),encoding='utf-8')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source-zip',type=Path,required=True);p.add_argument('--out-dir',type=Path,required=True)
    a=p.parse_args();run(a.source_zip,a.out_dir)
