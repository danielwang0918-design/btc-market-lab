"""Reproduce fixed baselines, then compare preregistered offline candidates."""
from __future__ import annotations
import argparse,csv,hashlib,io,itertools,json,os,tempfile
from collections import Counter
from decimal import Decimal as D,localcontext
from pathlib import Path
from zipfile import ZipFile
import p1_candlestick_research as core
import p1_portfolio as old
import p1_portfolio_study as old_report
import p1_loss_diagnostics as prior
import p1_loss_study as util
import p1_stop_candidates as m

P106_SHA='67fa9e644825bb849354cf315d6b9b69a6dd00e1578522a126668d4ead905723'
PERIODS={'continuous':(old.START,old.END),'train':(old.START,old.CUTS[0]),'validation':(old.CUTS[0],old.CUTS[1]),'opened_test':(old.CUTS[1],old.END)}
MODELS=('fractional_spot','hypothetical_1x_long_short')

def summary(trades,opportunities):
    stats=old_report.metrics(trades)
    stops=sum(t['exit_reason'] in ('stop','stop_gap') for t in trades)
    def avg(field):return float(sum(D(str(t[field])) for t in trades)/len(trades)) if trades else None
    return dict(**stats,opportunities=opportunities,untraded_ratio_pct=100*(1-len(trades)/opportunities) if opportunities else None,
        stop_exits=stops,stop_exit_rate_pct=100*stops/len(trades) if trades else None,
        net_loss_rate_pct=100*stats['losses']/len(trades) if trades else None,
        mean_SL_distance_pct=avg('SL_distance_pct'),mean_TP_distance_pct=avg('TP_distance_pct'),
        mean_net_target_RR=avg('cost_scenario_target_RR'),mean_entry_delay_hours=avg('entry_delay_hours'),
        mean_budget_utilization_pct=float(sum(t['planned_risk']/t['risk_budget']*100 for t in trades)/len(trades)) if trades else None,
        path_dependent_trades=sum(t['path_dependent_outcome'] for t in trades),
        unobserved_gap_or_zero_volume_trades=sum(t['gap_or_zero_volume_path_unknown'] for t in trades),
        same_entry_bar_stop=sum(t['exit_reason']=='stop' and t['exit_bar_ms']==t['entry_time_ms'] for t in trades))

def plan_fields(p):
    if p['status']!='eligible':return
    e=p['raw_entry'];p['SL_distance_pct']=abs(e-p['stop'])/e*100;p['TP_distance_pct']=abs(p['target']-e)/e*100
    p['entry_delay_hours']=D(p['entry_time_ms']-p['registration_ms'])/m.H
    p['entry_drift_pct']=(e-D(p.get('entry_reference',e)))*(1 if p['side']=='long' else -1)/e*100

def isolated(p,path,scenario):
    slip=D(15) if scenario=='source_slip15' else D(5);x=path[scenario];sized=m.size_cash(D(1000),p['raw_entry'],p['stop'],p['side'],slip)
    if sized is None:raise ValueError('Isolated funding failed')
    q=sized['quantity'];e=sized['entry_fill'];xf=old.fill(x['raw_exit'],p['side'],False,slip)
    pnl=q*m.net_unit(p['raw_entry'],x['raw_exit'],p['side'],slip)
    fee=q*(e+xf)*D('.001');gross=q*(x['raw_exit']-p['raw_entry'])*(1 if p['side']=='long' else -1)
    return dict(p,**x,**sized,scenario=scenario,net_pnl_usdt=pnl,r_multiple=pnl/sized['planned_risk'],
        cost_scenario_target_RR=m.net_unit(p['raw_entry'],p['target'],p['side'],slip)/-m.net_unit(p['raw_entry'],p['stop'],p['side'],slip),
        fees_usdt=fee,slippage_usdt=gross-pnl-fee,loss_exceeds_budget=-pnl>sized['risk_budget']+D('1e-9'),
        path_dependent_outcome=path['path_dependent_outcome'],gap_or_zero_volume_path_unknown=path['gap_or_zero_volume_path_unknown'])

def verify_baseline(z,plans,paths,assets):
    results=[];audits=[]
    for model,(period,(start,end)) in itertools.product(MODELS,PERIODS.items()):
        got=m.ledger(plans,paths,'A',model,'source_stop_first',start,end,assets)
        name=f'results/runs/{model}__{period}__A__trades.csv'
        expected=list(csv.DictReader(io.StringIO(z.read(name).decode('utf-8-sig'))))
        if len(got['trades'])!=len(expected):raise ValueError(f'Baseline count changed {model} {period} {len(got["trades"])} vs {len(expected)}')
        for a,b in zip(got['trades'],expected):
            for field in ('symbol','setup_id','side','entry_time_ms','exit_time_ms','exit_reason'):
                if str(a[field])!=b[field]:raise ValueError('Baseline identity changed '+field)
            for field in ('raw_entry','stop','target','raw_exit','quantity','planned_risk','net_pnl_usdt','r_multiple','equity_after'):
                if abs(D(str(a[field]))-D(b[field]))>D('1e-12'):raise ValueError('Baseline money changed '+field)
        source=list(csv.DictReader(io.StringIO(z.read('results/strategy_comparison.csv').decode('utf-8-sig'))))
        expected_summary=next(r for r in source if (r['model'],r['period'],r['policy'])==(model,period,'A'))
        if abs(got['maximum_drawdown_pct']-D(expected_summary['maximum_drawdown_pct']))>D('1e-12'):raise ValueError('Baseline MTM drawdown changed')
        results.append(dict(model=model,period=period,trades=len(expected),final_equity=str(got['final_equity']),
            maximum_drawdown_pct=str(got['maximum_drawdown_pct']),all_core_fields_match_1e_12=True))
        if period=='continuous':
            for t in got['trades']:
                if t['exit_reason']=='stop' and t['exit_bar_ms']==t['entry_time_ms']:
                    a=assets[t['symbol']];bar=a.bars[a.by_time[t['entry_time_ms']]]
                    states=m.feasible_bar(bar,t['raw_entry'],t['stop'],t['target'],t['side'],False)
                    path=paths[t['event_id']+'|A'];risk=-m.net_unit(t['raw_entry'],t['stop'],t['side'])
                    audits.append(dict(model=model,event_id=t['event_id'],symbol=t['symbol'],side=t['side'],
                        entry_time_utc=t['entry_time_utc'],O=bar.o,H=bar.h,L=bar.l,C=bar.c,entry=t['raw_entry'],SL=t['stop'],TP=t['target'],
                        feasible_entry_bar_states='|'.join(sorted(states)),same_bar_stop_forced_in_continuous_reference_model=states=={'stop'},
                        resting_limit_open_improvement_interpretation=bar.o<t['raw_entry'] if t['side']=='long' else bar.o>t['raw_entry'],
                        conditional_event_lower_R=m.net_unit(t['raw_entry'],path['feasible_lower']['raw_exit'],t['side'])/risk,
                        conditional_event_upper_R=m.net_unit(t['raw_entry'],path['feasible_upper']['raw_exit'],t['side'])/risk))
        print('BASELINE_REPRODUCED',model,period,len(expected),flush=True)
    return results,audits

def add_flags(rows):
    for r in rows:
        a=next(b for b in rows if (b['model'],b['period'],b['scenario'],b['policy'])==(r['model'],r['period'],r['scenario'],'A'))
        n=r['trade_count'];valid=n>0 and a['trade_count']>0
        r['fewer_trades_than_A']=n<a['trade_count']
        r['stop_rate_change_pp']=r['stop_exit_rate_pct']-a['stop_exit_rate_pct'] if valid else None
        r['mean_R_change']=r['average_R']-a['average_R'] if valid else None
        r['win_rate_change_pp']=r['win_rate_pct']-a['win_rate_pct'] if valid else None
        r['historical_joint_improvement']=bool(valid and n>=30 and r['stop_exit_rate_pct']<a['stop_exit_rate_pct'] and
            r['win_rate_pct']>a['win_rate_pct'] and r['average_R']>a['average_R'] and r['profit_factor'] is not None and a['profit_factor'] is not None and
            r['profit_factor']>=a['profit_factor'] and D(r['maximum_drawdown_pct'])<=D(a['maximum_drawdown_pct']))
        r['positive_net_expectancy']=bool(n and r['average_R']>0)
        r['fewer_trades_not_positive_edge']=r['fewer_trades_than_A'] and not r['positive_net_expectancy']
        r['independent_signal_not_causal_control']=r['policy']=='I_SWEEP20'

def report(out,accounts,events,paired,audit,qualified,breakdowns,plans):
    fmt=lambda v:'—' if v is None else f'{float(v):.4f}'
    lines=['# P1-06B｜高止損率改善方案實測','',
        '純離線歷史探索。六個候選先於績效固定，沒有改策略參數網格、正式策略或舊PR。所有成本為固定模型假設，非真實成交認證；Test已開啟，不是新OOS。','',
        '## 基準可信度與同棒路徑','',
        'P1-05 A兩模型四時段逐筆核心價格、時刻、數量、風險、成本後PnL、R、終值與MTM回撤在1e-12容差內重現。原105筆純多入場同棒SL全部保留，不把它們直接改成獲利。','',
        '| 模型 | 同棒SL數 | 連續參考價觸及假設下必然同棒SL | 可有其他棒內結果 | 開盤優於參考價的另一限價解讀 |',
        '|---|---:|---:|---:|---:|']
    for model in MODELS:
        rows=[a for a in audit if a['model']==model];forced=sum(a['same_bar_stop_forced_in_continuous_reference_model'] for a in rows)
        lines.append(f"| {model} | {len(rows)} | {forced} | {len(rows)-forced} | {sum(a['resting_limit_open_improvement_interpretation'] for a in rows)} |")
    lines += ['', '有限狀態圖列舉所有連續OHLC可行barrier結果，不捏造精確順序。中性為原stop-first慣例，不是機率中位；lower/upper是每事件事後可行終值範圍，帳戶情境不是全局數學上下界。缺口、深度及棒內跳價不在可識別範圍，不能宣稱真實止損一定可避免或不可避免。','',
        '## 先做共同事件控制比較','',
        '共同母體為原A全部登記事件，包含未觸及／取消／拒絕；共同首次合資格觸及沿用原執行防護。孤立事件各自以1000 USDT預算2%與現金上限，不共享本金，重疊事件不能當實際同時持倉或獨立試驗。配對表同列全部機會的零含入報酬差與雙方均成交的子集。','',
        '| 模型 | 政策 | 相同機會 | 雙方均成交 | 僅A／僅候選 | 每共同機會淨USDT差 | 同成交平均淨R差 |',
        '|---|---|---:|---:|---:|---:|---:|']
    for r in paired:
        if r['period']=='continuous' and r['scenario']=='source_stop_first':
            lines.append(f"| {r['model']} | {r['policy']} | {r['opportunities']} | {r['both_filled']} | {r['only_A']}／{r['only_candidate']} | {fmt(r['delta_USDT_per_shared_opportunity'])} | {fmt(r['delta_R_common_filled'])} |")
    for model in MODELS:
        lines += ['',f'## 完整資金帳戶｜{model}','',
            '起始1000 USDT、正常模型風險≤進場前淨值2%、全球單倉、至多1000次建倉、固定相同日曆截止。以下主情境使用10bps/side手續費及5bps/side滑價。空頭僅假想1x抵押，不含借幣資格／真實成本；fractional Spot亦缺歷史最小委託規則。','',
            '| 政策 | 機會／成交 | 未交易% | SL率% | 淨虧損率% | 勝率% | 平均淨R | PF | 淨PnL USDT | 最終USDT | MDD% | 改善旗標 |',
            '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|']
        for r in accounts:
            if r['model']==model and r['period']=='continuous' and r['scenario']=='source_stop_first':
                lines.append(f"| {r['policy']} | {r['opportunities']}／{r['trade_count']} | {fmt(r['untraded_ratio_pct'])} | {fmt(r['stop_exit_rate_pct'])} | {fmt(r['net_loss_rate_pct'])} | {fmt(r['win_rate_pct'])} | {fmt(r['average_R'])} | {fmt(r['profit_factor'])} | {fmt(r['net_pnl_USDT'])} | {fmt(r['final_equity_USDT'])} | {fmt(r['maximum_drawdown_pct'])} | {r['historical_joint_improvement']} |")
        lines += ['', '| 政策 | SL實際距離%均值 | TP實際距離%均值 | 入場淨RR均值 | 登記到入場h | 在市% | 路徑依賴成交 | 未觀察路徑成交 | cap拒絕 |', '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
        for r in accounts:
            if r['model']==model and r['period']=='continuous' and r['scenario']=='source_stop_first':
                lines.append(f"| {r['policy']} | {fmt(r['mean_SL_distance_pct'])} | {fmt(r['mean_TP_distance_pct'])} | {fmt(r['mean_net_target_RR'])} | {fmt(r['mean_entry_delay_hours'])} | {fmt(r['exposure_percent'])} | {r['path_dependent_trades']} | {r['unobserved_gap_or_zero_volume_trades']} | {r['cap_rejections']} |")
        lines += ['', '| 政策 | Train n／SL%／淨R | Validation n／SL%／淨R | 已開啟Test n／SL%／淨R |', '|---|---|---|---|']
        for policy in m.POLICIES:
            cells=[]
            for period in ('train','validation','opened_test'):
                r=next(x for x in accounts if (x['model'],x['period'],x['scenario'],x['policy'])==(model,period,'source_stop_first',policy))
                cells.append(f"{r['trade_count']}／{fmt(r['stop_exit_rate_pct'])}／{fmt(r['average_R'])}")
            lines.append('| '+policy+' | '+' | '.join(cells)+' |')
        lines += ['', '| 政策 | 範圍 | n | SL率% | 勝率% | 淨R | PF |', '|---|---|---:|---:|---:|---:|---:|']
        for policy,universe in itertools.product(m.POLICIES,('BTC','other_seven')):
            r=next(x for x in breakdowns if (x['model'],x['period'],x['scenario'],x['policy'],x['dimension'],x['category'])==(model,'continuous','source_stop_first',policy,'universe',universe))
            lines.append(f"| {policy} | {universe} | {r['trade_count']} | {fmt(r['stop_exit_rate_pct'])} | {fmt(r['win_rate_pct'])} | {fmt(r['average_R'])} | {fmt(r['profit_factor'])} |")
        lines += ['', '| 政策 | 情境 | n | SL率% | 勝率% | 淨R | PF | 最終USDT | MDD% |', '|---|---|---:|---:|---:|---:|---:|---:|---:|']
        for r in accounts:
            if r['model']==model and r['period']=='continuous':
                lines.append(f"| {r['policy']} | {r['scenario']} | {r['trade_count']} | {fmt(r['stop_exit_rate_pct'])} | {fmt(r['win_rate_pct'])} | {fmt(r['average_R'])} | {fmt(r['profit_factor'])} | {fmt(r['final_equity_USDT'])} | {fmt(r['maximum_drawdown_pct'])} |")
    lines += ['', '## 每個候選的判定與未交易原因','', '| 模型 | 政策 | 判定 |', '|---|---|---|']
    for model,policy in itertools.product(MODELS,m.POLICIES[1:]):
        r=next(x for x in accounts if (x['model'],x['period'],x['scenario'],x['policy'])==(model,'continuous','source_stop_first',policy))
        state='樣本不足／不可判斷' if r['trade_count']<30 else '相對指標同時改善，但仍負期望' if r['historical_joint_improvement'] and not r['positive_net_expectancy'] else '歷史相對改善；須跨時段與成本核對' if r['historical_joint_improvement'] else '降低SL卻未改善整體經濟性' if r['stop_rate_change_pp'] is not None and r['stop_rate_change_pp']<0 else '未達事前改善標準'
        lines.append(f'| {model} | {policy} | {state} |')
    lines += ['', '| 政策 | 全部機會 | 原共同觸及資格後 | 候選合資格 | 主要拒絕計數（全部保留） |', '|---|---:|---:|---:|---|']
    for policy in m.POLICIES:
        selected=[p for p in plans if p['policy']==policy];counts=Counter(p['status'] for p in selected)
        lines.append(f"| {policy} | {len(selected)} | {sum(p['anchor_ms'] is not None for p in selected)} | {counts['eligible']} | "+'; '.join(f'{k}:{v}' for k,v in sorted(counts.items()) if k!='eligible')+' |')
    breakrows=[p for p in plans if p['policy']=='E_BREAK_RETEST' and 'confirmation_reference_pivot' in p]
    lines += ['', f"突破回踩方案中，有已知事前pivot的觸及機會{len(breakrows)}個，其中{sum(p['pivot_at_or_beyond_original_target'] for p in breakrows)}個pivot已在原TP1或更遠。原TP不變且觸TP先失效，可能使結構確認與原目標互相衝突；零成交不能推論所有MSS/CHOCH無效。本次不搬移TP或延長確認窗口補交易。", '',
        '## 成敗、代價及前瞻候選','',
        'historical_joint_improvement需n≥30、SL率降低、勝率與淨R提高、PF不降及MDD不升。只降低SL率、少交易保本或縮短TP不能成功。所有受控候選原TP1未縮短；SL擴大可能降低RR並造成RR門檻拒絕，配對表明列因此改變的樣本。I_SWEEP20的2淨R及72h是獨立模型，不能與A做單一因素因果比較。',
        f'事前嚴格正期望／跨幣／已開啟Test／成本壓力門檻通過候選：{", ".join(qualified) or "0個"}。這不是新OOS認證。',
        '若0個達標，不推薦任何已證明獲利候選。S_ATR1若通過事前「相對指標同時改善」旗標，只能作下一階段機制驗證的線索，不能改標為嚴格正期望候選。原規格另外允許E_CLOSE、E_BREAK_RETEST、I_SWEEP20作訊號可觀測性蒐集假設，並不代表三者已因績效獲推薦；失敗或不可判斷結果完整保留，不調參翻案。','',
        '## 限制與完整輸出','',
        '- 模型假設包含OHLC參考價觸及即成交、整棒target/zone防護及1H收盤估值，不是真實成交證明。source_stop_first不是統計上中性，lower/upper不得按未來盈利挑訊號。每事件極端情境的不同退出使後續資金佔用不同，不能把帳戶曲線當真正全局上下界。',
        '- 所有歷史Test已被查看。現存八幣有選幣／存活者偏差；重疊事件、同一事件多情境／多時段不是獨立交易。SL率差不是實證因果效果。',
        '- 15bps壓力按15bps重新換算正常風險，仍不能保證跳空損失≤2%。原資料沒有歷史LOT_SIZE／MIN_NOTIONAL、深度與借幣資格，沒有任何真實Spot或空頭可成交認證。',
        '- baseline_reconciliation.json／baseline_same_bar_audit.csv：原八個A帳戶與同棒可行性。candidate_events.csv／event_outcomes.csv：全部候選、失敗與四個路徑情境，孤立事件可按方向套用模型。',
        '- isolated_comparison.csv／paired_control_comparison.csv：先事件後帳戶的控制比較、未交易零含入與共同成交子集。portfolio_comparison.csv／portfolio_breakdowns.csv：224帳戶及全部幣、年度、波動／方向分組與零組。',
        '- runs/*trades.csv／*decisions.csv／*equity.csv、equity_*.png：完整逐筆風險、價格、成本、接受／拒絕、資金與共同日曆曲線。全部來源ZIP、程式／輸出SHA256與測試日誌一併保存。',
        '- 方法、每個候選規則與外部來源詳見P1_06B_METHODS.md；市場理論與外匯文獻是動機，不是Binance策略有效性證據。','']
    (out/'P1_06B_REPORT_ZH.md').write_text('\n'.join(lines),encoding='utf-8')

def run(source_zip,out):
    with localcontext() as ctx:
        ctx.prec=50
        return _run(source_zip,out)

def _run(source_zip,out):
    if out.exists() and any(out.iterdir()):
        raise ValueError('Output directory must be empty; use a new folder to prevent stale evidence')
    root=Path(__file__).parent;out.mkdir(parents=True,exist_ok=True)
    if util.sha((root/'P1_06B_PROTOCOL.json').read_bytes())!=m.PROTOCOL_SHA:raise ValueError('Pre-performance protocol changed')
    if util.sha(source_zip.read_bytes())!=P106_SHA:raise ValueError('P1-06A source ZIP SHA256 mismatch')
    with ZipFile(source_zip) as outer, tempfile.TemporaryDirectory(prefix='p1-06b-') as tmp:
        p106=json.loads(outer.read('results/p1_06a_manifest.json'))
        for name,digest in p106['code_sha256'].items():
            if util.sha((root/name).read_bytes())!=digest:raise ValueError('P1-06A source code changed')
        raw=outer.read('inputs/p1-05-source.zip')
        if util.sha(raw)!=prior.SOURCE_ZIP_SHA:raise ValueError('P1-05 nested archive changed')
        with ZipFile(io.BytesIO(raw)) as z:
            source=json.loads(z.read('results/p1_05_manifest.json'));prior.validate_manifest(source)
            for name,digest in source['code_sha256'].items():
                if util.sha((root/name).read_bytes())!=digest:raise ValueError('Protected code changed')
            lock=core.read_lock(root/'PROSPECTIVE_LOCK_V1.json');folder=Path(tmp);data=folder/'data';tapes=folder/'tapes';data.mkdir();tapes.mkdir()
            with ZipFile(io.BytesIO(z.read('inputs/frozen-source.zip'))) as frozen:
                for name,meta in lock['frozen_data_prefixes'].items():
                    b=frozen.read(name)
                    if util.sha(b)!=meta['sha256']:raise ValueError('Frozen SHA256 changed')
                    (data/name).write_bytes(b)
            for symbol in lock['symbols']:
                b=z.read(f'inputs/tapes/{symbol}.json.gz')
                if util.sha(b)!=source['source_sha256']['canonical_tapes'][symbol+'.json.gz']:raise ValueError('Canonical tape changed')
                (tapes/(symbol+'.json.gz')).write_bytes(b)
            assets={s:old.Asset(s,data,tapes) for s in lock['symbols']}
            for a in assets.values():m.prepare_context(a)
            signals=list(csv.DictReader(io.StringIO(z.read('results/runs/fractional_spot__continuous__A__signals.csv').decode('utf-8-sig'))))
            original_events=m.episodes(signals,assets);plans=[m.controlled_plan(e,'A',assets[e['symbol']]) for e in original_events]
            paths={}
            for p in plans:
                plan_fields(p)
                if p['status']=='eligible':paths[p['event_id']+'|A']=m.outcome_paths(p,assets[p['symbol']])
            baseline,audit=verify_baseline(z,plans,paths,assets)
            util.write_json(out/'baseline_reconciliation.json',baseline);util.write_csv(out/'baseline_same_bar_audit.csv',audit)
            print('BASELINE_VALIDATED_BEFORE_CANDIDATES',flush=True)
            for policy in m.POLICIES[1:-1]:
                print('BUILD_CANDIDATE',policy,flush=True)
                plans.extend(m.controlled_plan(e,policy,assets[e['symbol']]) for e in original_events)
            for a in assets.values():
                print('BUILD_INDEPENDENT',a.symbol,flush=True);plans.extend(m.independent_plans(a))
            for p in plans:
                plan_fields(p)
                key=p['event_id']+'|'+p['policy']
                if p['status']=='eligible' and key not in paths:paths[key]=m.outcome_paths(p,assets[p['symbol']])
            util.write_csv(out/'candidate_events.csv',plans)
            outcomes=[]
            for p in plans:
                if p['status']=='eligible':
                    for scenario in m.SCENARIOS:outcomes.append(isolated(p,paths[p['event_id']+'|'+p['policy']],scenario))
            util.write_csv(out/'event_outcomes.csv',outcomes)
            event_stats=[];paired=[]
            for model,(period,(start,end)),scenario in itertools.product(MODELS,PERIODS.items(),m.SCENARIOS):
                eligible=lambda p:p['side']=='long' or model=='hypothetical_1x_long_short'
                per={}
                for policy in m.POLICIES:
                    subset=[p for p in plans if p['policy']==policy and start<=p['registration_ms']<end]
                    ts=[r for r in outcomes if r['policy']==policy and r['scenario']==scenario and start<=r['registration_ms']<end and eligible(r)]
                    supported=sum(eligible(p) for p in subset)
                    event_stats.append(dict(model=model,period=period,scenario=scenario,policy=policy,**summary(ts,len(subset)),
                        direction_supported_opportunities=supported,untraded_supported_ratio_pct=100*(1-len(ts)/supported) if supported else None,
                        maximum_drawdown_pct=None,study_type='isolated_overlapping_events_not_account'))
                    per[policy]={r['event_id']:r for r in ts}
                for policy in m.POLICIES[1:-1]:
                    ids=[e['event_id'] for e in original_events if start<=e['registration_ms']<end];a=per['A'];b=per[policy];both=set(a)&set(b)
                    paired.append(dict(model=model,period=period,scenario=scenario,policy=policy,opportunities=len(ids),both_filled=len(both),
                        only_A=len(set(a)-set(b)),only_candidate=len(set(b)-set(a)),
                        delta_USDT_per_shared_opportunity=float(sum((b[k]['net_pnl_usdt'] if k in b else D(0))-(a[k]['net_pnl_usdt'] if k in a else D(0)) for k in ids)/len(ids)) if ids else None,
                        delta_R_common_filled=float(sum(b[k]['r_multiple']-a[k]['r_multiple'] for k in both)/len(both)) if both else None))
            util.write_csv(out/'isolated_comparison.csv',event_stats);util.write_csv(out/'paired_control_comparison.csv',paired)
            print('CONTROLLED_EVENT_COMPARISON_FINISHED_BEFORE_ACCOUNTS',flush=True)
            accounts=[];breakdowns=[];cash_audits=[]
            for model,(period,(start,end)),scenario in itertools.product(MODELS,PERIODS.items(),m.SCENARIOS):
                chart={}
                for policy in m.POLICIES:
                    print('ACCOUNT',model,period,scenario,policy,flush=True)
                    r=m.ledger(plans,paths,policy,model,scenario,start,end,assets);chart[policy]=r
                    localplans=[p for p in plans if p['policy']==policy and start<=p['registration_ms']<end]
                    supported=sum(p['side']=='long' or model=='hypothetical_1x_long_short' for p in localplans)
                    cash_audits.append(audit_account(r))
                    row=dict(model=model,period=period,scenario=scenario,policy=policy,calendar_start_utc=old.utc(start),calendar_end_utc=old.utc(end),
                        initial_equity_USDT=1000,final_equity_USDT=str(r['final_equity']),maximum_drawdown_pct=str(r['maximum_drawdown_pct']),
                        exposure_percent=100*r['in_market_hours']/((end-start)/m.H),cap_rejections=sum(x['status']=='first_1000_cap' for x in r['decisions']),
                        direction_supported_opportunities=supported,untraded_supported_ratio_pct=100*(1-len(r['trades'])/supported) if supported else None,
                        **summary(r['trades'],r['opportunities']))
                    accounts.append(row)
                    prefix=out/'runs'/f'{model}__{period}__{scenario}__{policy}'
                    util.write_csv(prefix.with_name(prefix.name+'__trades.csv'),r['trades']);util.write_csv(prefix.with_name(prefix.name+'__decisions.csv'),r['decisions']);util.write_csv(prefix.with_name(prefix.name+'__equity.csv'),r['curve'])
                    categories={'universe':list(util.universes(lock['symbols'])),'registration_year':[str(y) for y in range(2020,2027)],
                        'volatility':['low','normal','high','insufficient_prior_atr_distribution']}
                    for frame in ('1h','4h','1d'):categories['MA_'+frame]=['bullish','bearish','transition_or_range','insufficient_prior_ma','reference_data_gap_or_stale']
                    def match(p,dim,cat):
                        if dim=='universe':return p['symbol'] in util.universes(lock['symbols'])[cat]
                        if dim=='registration_year':return old.utc(p['registration_ms'])[:4]==cat
                        return p.get(dim,'unclassified')==cat
                    for dim,labels in categories.items():
                        if dim not in ('universe','registration_year'):labels=list(dict.fromkeys([*labels,*(p.get(dim,'unclassified') for p in localplans)]))
                        for cat in labels:
                            ps=[p for p in localplans if match(p,dim,cat)];ts=[t for t in r['trades'] if match(t,dim,cat)]
                            breakdowns.append(dict(model=model,period=period,scenario=scenario,policy=policy,dimension=dim,category=cat,**summary(ts,len(ps))))
                draw(out/f'equity_{model}__{period}__{scenario}.png',chart,model,period,scenario)
            add_flags(accounts);util.write_csv(out/'portfolio_comparison.csv',accounts);util.write_csv(out/'portfolio_breakdowns.csv',breakdowns)
            util.write_json(out/'independent_cash_audit.json',cash_audits)
            qualified=[]
            for policy in m.POLICIES[1:]:
                ok=True
                for period in ('continuous','opened_test'):
                    r=next(x for x in accounts if (x['model'],x['period'],x['scenario'],x['policy'])==('fractional_spot',period,'source_stop_first',policy))
                    ok=ok and r['historical_joint_improvement'] and r['positive_net_expectancy'] and (r['profit_factor'] or 0)>1 and D(r['final_equity_USDT'])>1000
                for universe in ('BTC','other_seven'):
                    r=next(x for x in breakdowns if (x['model'],x['period'],x['scenario'],x['policy'],x['dimension'],x['category'])==('fractional_spot','continuous','source_stop_first',policy,'universe',universe))
                    ok=ok and r['trade_count']>0 and r['average_R']>0
                stress=next(x for x in accounts if (x['model'],x['period'],x['scenario'],x['policy'])==('fractional_spot','continuous','source_slip15',policy))
                ok=ok and stress['trade_count']>0 and stress['average_R']>=0
                if ok:qualified.append(policy)
            report(out,accounts,event_stats,paired,audit,qualified,breakdowns,plans)
            manifest=dict(version='p1-06b-fixed-candidates-v1',current_commit=os.getenv('GITHUB_SHA'),protocol_sha256=m.PROTOCOL_SHA,
                P106A_archive_sha256=P106_SHA,P105_archive_sha256=prior.SOURCE_ZIP_SHA,
                frozen_source_sha256=source['source_sha256']['frozen_files'],frozen_candle_count=sum(len(b) for a in assets.values() for b in a.frames.values()),
                baseline_accounts_reconciled=len(baseline),raw_controlled_registration_episodes=len(original_events),first_eligible_anchors=sum(e['anchor_ms'] is not None for e in original_events),
                candidate_event_rows=len(plans),event_outcome_rows=len(outcomes),account_runs=len(accounts),qualified_forward_candidates=qualified,
                code_sha256={n:util.sha((root/n).read_bytes()) for n in ('p1_stop_candidates.py','p1_stop_study.py')},
                output_sha256={str(p.relative_to(out)):util.sha(p.read_bytes()) for p in sorted(out.rglob('*')) if p.is_file()},
                source_code_unchanged=True,new_OOS=False,live_execution=False,
                assumptions='conditional_continuous_OHLC_reference_fill_no_intrabar_gaps_or_depth_no_certified_exchange_fills')
            util.write_json(out/'p1_06b_manifest.json',manifest)
            return manifest

def audit_account(result):
    cash=D(1000);busy=-1
    for t in result['trades']:
        if t['entry_time_ms']<busy or t['feature_cutoff_ms']>t['entry_time_ms']:raise ValueError('Overlap or future features')
        if t['cash_required']>cash+D('1e-18') or t['planned_risk']>cash*D('.02')+D('1e-18'):raise ValueError('Excess cash or budget')
        q=t['quantity'];fee=q*(t['entry_fill']+t['exit_fill'])*D('.001')
        gross=q*(t['exit_fill']-t['entry_fill'])*(1 if t['side']=='long' else -1)
        if abs(gross-fee-t['net_pnl_usdt'])>D('1e-12'):raise ValueError('Cash flow mismatch')
        cash+=gross-fee
        if abs(cash-t['equity_after'])>D('1e-12'):raise ValueError('Equity mismatch')
        busy=t['reserved_until_ms']
    if abs(cash-result['final_equity'])>D('1e-12'):raise ValueError('Final cash mismatch')
    peak=D(1000);dd=D(0)
    for point in result['curve']:
        if point['cash_usdt']<0:raise ValueError('Negative free cash')
        peak=max(peak,point['equity_usdt']);dd=max(dd,(peak-point['equity_usdt'])/peak*100)
    if abs(dd-result['maximum_drawdown_pct'])>D('1e-12'):raise ValueError('Drawdown mismatch')
    return dict(trades_checked=len(result['trades']),cash_risk_costs_single_position_and_MTM_verified=True)

def draw(path,chart,model,period,scenario):
    os.environ.setdefault('MPLCONFIGDIR','/tmp/p1-06b-mpl')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from datetime import datetime,timezone
    fig,ax=plt.subplots(figsize=(12,5))
    for policy,r in chart.items():
        ax.plot([datetime.fromtimestamp(x['time_ms']/1000,timezone.utc) for x in r['curve']],[float(x['equity_usdt']) for x in r['curve']],label=policy,linewidth=1)
    ax.axhline(1000,color='gray',linestyle='--',linewidth=.6);ax.legend();ax.grid(alpha=.2)
    ax.set(title=f'Offline candidate scenarios | {model} | {period} | {scenario}',ylabel='Liquidation mark USDT',xlabel='Shared calendar UTC')
    fig.tight_layout();fig.savefig(path,dpi=130);plt.close(fig)

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source-zip',type=Path,required=True);parser.add_argument('--out-dir',type=Path,required=True)
    args=parser.parse_args();run(args.source_zip,args.out_dir)
if __name__=='__main__':main()
