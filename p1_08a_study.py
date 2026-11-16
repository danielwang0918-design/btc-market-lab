"""P1-08A: verify archived evidence, annotate causal states, diagnose; no replay."""
from __future__ import annotations
import argparse,csv,hashlib,io,itertools,json,os,tempfile
from collections import Counter,defaultdict
from decimal import Decimal as D,localcontext
from pathlib import Path
from zipfile import ZipFile
import p1_candlestick_research as core
import p1_portfolio as money
import p1_loss_study as files
import p1_stop_study as prior
import p1_08a_diagnostics as diag

BASE='6865531f2b5b5f5fa8ce7741102836ca73694d64'
SOURCE_SHA='eed65cbfd3ad60c01ef951d2bea5a1414e269e0ef756ae527aa7a27d0402171f'
PROTOCOL_SHA='ccf08be091b2e13770e2bff81b1137118cc790ca6ed770a85a799230f6b303a1'
POLICIES=('A','S_ATR1','T20_FAIL','T20_CONT','ML_GATE','FIXED_COST25')
UNIVERSES=('all_eight','BTC','other_seven')


def read(z,name):
    return list(csv.DictReader(io.StringIO(z.read(name).decode('utf-8-sig'))))


def audit_account(summary,trades,curve,decisions):
    cash=D(1000);busy=-1
    keys=[r['event_id'] for r in decisions]
    if len(set(keys))!=len(keys):raise ValueError('Duplicate archived decision')
    entered={r['event_id'] for r in decisions if r['status']=='entered'}
    if entered!={r['event_id'] for r in trades} or len(trades)!=len(entered):raise ValueError('Decision/trade identity mismatch')
    for r in trades:
        t=int(r['entry_time_ms'])
        if t<busy or int(r['feature_cutoff_ms'])>t or int(r['signal_time_ms'])>t:raise ValueError('Archived overlap or future feature')
        if summary['model']=='fractional_spot' and r['side']!='long':raise ValueError('Spot short mixed with longs')
        if abs(D(r['equity_before'])-cash)>D('1e-12'):raise ValueError('Equity before mismatch')
        if D(r['cash_required'])>cash+D('1e-12') or D(r['planned_risk'])>cash*D('.02')+D('1e-12'):raise ValueError('Excess cash or risk')
        q=D(r['quantity']);ef=D(r['entry_fill']);xf=D(r['exit_fill'])
        expected_fee=q*(ef+xf)*D('.001')
        if abs(expected_fee-D(r['fees_usdt']))>D('1e-12'):raise ValueError('Archived fee mismatch')
        gross=q*(xf-ef)*(1 if r['side']=='long' else -1)
        if abs(gross-expected_fee-D(r['net_pnl_usdt']))>D('1e-12'):raise ValueError('Fill/cash identity mismatch')
        diag.enrich_money(r)
        cash+=gross-expected_fee
        if abs(cash-D(r['equity_after']))>D('1e-12'):raise ValueError('Equity after mismatch')
        busy=int(r['reserved_until_ms'])
    if len(trades)!=int(summary['trade_count']) or abs(cash-D(summary['final_equity_USDT']))>D('1e-12'):raise ValueError('Final equity/count mismatch')
    if sum(D(r['net_pnl_usdt'])>0 for r in trades)!=int(summary['wins']) or sum(D(r['net_pnl_usdt'])<0 for r in trades)!=int(summary['losses']):raise ValueError('Win/loss count mismatch')
    if not curve or int(curve[0]['time_ms'])!=money.ms(summary['calendar_start_utc']) or int(curve[-1]['time_ms'])!=money.ms(summary['calendar_end_utc']):raise ValueError('Calendar endpoints mismatch')
    peak=D(1000);dd=D(0);last=-1
    for point in curve:
        stamp=int(point['time_ms'])
        if stamp<last or D(point['cash_usdt'])<0:raise ValueError('Curve clock or free cash invalid')
        last=stamp;mark=D(point['equity_usdt']);peak=max(peak,mark);dd=max(dd,(peak-mark)/peak*100)
    if abs(dd-D(summary['maximum_drawdown_pct']))>D('1e-12') or abs(D(curve[-1]['equity_usdt'])-cash)>D('1e-12'):raise ValueError('Archived MTM drawdown or final mark mismatch')
    return dict(verified=True,trades=len(trades),decision_rows=len(decisions),curve_points=len(curve),maximum_drawdown_pct=dd,
        note='Audit of archived cash and curve; no new price-path reconstruction or strategy replay')


def annotate(row,features):
    t=int(row['entry_time_ms']);f=features[(row['symbol'],t)]
    for frame in core.FRAMES:
        if row.get('MA_'+frame) and row['MA_'+frame]!=f['MA_'+frame]:raise ValueError('Archived MA category differs from causal reconstruction')
        close=f['reference_close_ms_'+frame]
        if close is not None and close>t:raise ValueError('Future frame reference')
    return diag.enrich_money(dict(row,**f))


def in_universe(row,name):
    return name=='all_eight' or (row['symbol']=='BTCUSDT' if name=='BTC' else row['symbol']!='BTCUSDT')


def dimensions(symbols):
    dims={key+'_'+frame:values for frame in core.FRAMES for key,values in diag.STATE_CLASSES.items()}
    dims.update(symbol=tuple(symbols),entry_year=tuple(str(y) for y in range(2020,2027)),entry_session_UTC=('00-08','08-16','16-24'),
        side=('long','short'),cost_share_bin=('<=.25','>.25'),SL_ATR_bin=('[0,.5)','[.5,1)','[1,2)','[2,+inf)','unknown'),
        exit_reason=('stop','stop_gap','target','max_hold_72h','calendar_boundary','data_gap_first_quote','integrity_review_exit'),
        holding_bucket=('[0,1]','(1,6]','(6,24]','(24,72]','(72,+inf)'))
    return dims


def grouped(rows,identity,symbols):
    output=[]
    for universe in UNIVERSES:
        local=[r for r in rows if in_universe(r,universe)]
        output.append(dict(**identity,universe=universe,dimension='all',category='all',**diag.summarize(local)))
        for field,categories in dimensions(symbols).items():
            buckets=defaultdict(list)
            for r in local:buckets[str(r.get(field,'unknown'))].append(r)
            for category in dict.fromkeys((*categories,*sorted(set(buckets)-set(categories)))):
                output.append(dict(**identity,universe=universe,dimension=field,category=category,**diag.summarize(buckets[category])))
    return output


def verify_archive(z):
    manifest=json.loads(z.read('results/p1_07_manifest.json'))
    if manifest['current_commit']!=BASE or manifest['qualified_forward_candidates'] or manifest['new_OOS'] or manifest['live_execution']:raise ValueError('Wrong frozen P1-07 study')
    for name,digest in manifest['output_sha256'].items():
        if files.sha(z.read('results/'+name))!=digest:raise ValueError('P1-07 output changed: '+name)
    for name,digest in manifest['code_sha256'].items():
        if files.sha(Path(__file__).with_name(name).read_bytes())!=digest:raise ValueError('Inherited P1-07 code changed')
    return manifest


def frame_features(z6,plans,isolated,lock):
    requested=defaultdict(set)
    for row in plans:
        requested[row['symbol']].add(int(row['registration_ms']))
        if row['status']=='eligible':requested[row['symbol']].add(int(row['entry_time_ms']))
    for row in isolated:requested[row['symbol']].add(int(row['entry_time_ms']))
    with ZipFile(io.BytesIO(z6.read('inputs/p1-06a-source.zip'))) as a:
        raw105=a.read('inputs/p1-05-source.zip')
    with ZipFile(io.BytesIO(raw105)) as old, ZipFile(io.BytesIO(old.read('inputs/frozen-source.zip'))) as raw:
        features={};quality=[]
        for symbol in lock['symbols']:
            frames={}
            for frame,step in core.FRAMES.items():
                name=symbol+'_'+frame+'.csv';meta=lock['frozen_data_prefixes'][name];content=raw.read(name)
                if files.sha(content)!=meta['sha256'] or len(content)!=meta['bytes']:raise ValueError('Raw frozen SHA mismatch')
                bars=core.parse_bars(content,symbol,frame,meta['frozen_bars'],closed_through_ms=meta['frozen_last_close_exclusive_ms']);frames[frame]=bars
                quality.append(dict(symbol=symbol,frame=frame,sha256=files.sha(content),bytes=len(content),bars=len(bars),
                    zero_volume=sum(b.v==0 for b in bars),gaps=sum(b.open_ms-a.open_ms>step for a,b in zip(bars,bars[1:])),
                    missing_bars=sum((b.open_ms-a.open_ms)//step-1 for a,b in zip(bars,bars[1:])),first_open_ms=bars[0].open_ms,last_close_exclusive_ms=bars[-1].open_ms+step,
                    duplicate_or_unclosed_or_invalid_bars=0))
            book=diag.FeatureBook(frames)
            for t in sorted(requested[symbol]):features[(symbol,t)]=book.at(t)
            print('CLOSED_STATE_FEATURES',symbol,len(requested[symbol]),flush=True)
    return features,quality


def run(source_zip,out):
    with localcontext() as ctx:
        ctx.prec=50
        return _run(source_zip,out)


def _run(source_zip,out):
    root=Path(__file__).parent
    if out.exists() and any(out.iterdir()):raise ValueError('Output directory must be empty')
    if files.sha(source_zip.read_bytes())!=SOURCE_SHA:raise ValueError('Pinned P1-07 archive SHA changed')
    if files.sha((root/'P1_08A_PROTOCOL.json').read_bytes())!=PROTOCOL_SHA:raise ValueError('Fixed diagnostic protocol changed')
    out.mkdir(parents=True,exist_ok=True)
    with ZipFile(source_zip) as z:
        manifest=verify_archive(z);raw6=z.read('inputs/p1-06b-source.zip')
        if files.sha(raw6)!=manifest['source_zip_sha256']:raise ValueError('Nested P1-06B SHA changed')
        with ZipFile(io.BytesIO(raw6)) as z6:
            lock=core.read_lock(root/'PROSPECTIVE_LOCK_V1.json')
            old_plans=[r for r in read(z6,'results/candidate_events.csv') if r['policy'] in ('A','S_ATR1')]
            plans=old_plans+read(z,'results/new_signal_events.csv')+read(z,'results/meta_gate_opportunities.csv')
            if len({(p['policy'],p['event_id']) for p in plans})!=len(plans):raise ValueError('Duplicate frozen opportunity')
            isolated=[r for r in read(z6,'results/event_outcomes.csv') if r['policy'] in ('A','S_ATR1')]+read(z,'results/new_isolated_outcomes.csv')
            if len({(r['policy'],r['event_id'],r['scenario']) for r in isolated})!=len(isolated):raise ValueError('Duplicate isolated outcome')
            features,quality=frame_features(z6,plans,isolated,lock)
            if sum(r['bars'] for r in quality)!=manifest['frozen_candle_count']:raise ValueError('Candle count mismatch')
            files.write_csv(out/'data_quality.csv',quality)
            snapshots=[];census=Counter()
            for p in plans:
                for stage,t in [('registration',int(p['registration_ms']))]+([('entry',int(p['entry_time_ms']))] if p['status']=='eligible' else []):
                    snapshots.append(dict(event_id=p['event_id'],policy=p['policy'],symbol=p['symbol'],side=p['side'],
                        source_final_status=p['status'],stage=stage,decision_time_ms=t,source_registration_ms=p['registration_ms'],**features[(p['symbol'],t)]))
                    census[(p['policy'],stage,p['status'])]+=1
            files.write_csv(out/'opportunity_snapshots.csv',snapshots)
            files.write_csv(out/'opportunity_census.csv',[dict(policy=k[0],stage=k[1],source_final_status=k[2],rows=n) for k,n in sorted(census.items())])
            availability=Counter()
            for s in snapshots:
                for frame in core.FRAMES:
                    for key in ('status','MA','ER_state','rvol_bin','turnover_bin'):
                        availability[(s['policy'],s['stage'],frame,key,str(s[key+'_'+frame]))]+=1
            files.write_csv(out/'feature_availability.csv',[dict(policy=k[0],stage=k[1],frame=k[2],feature=k[3],value=k[4],n=v) for k,v in sorted(availability.items())])
            files.write_csv(out/'unique_time_feature_audit.csv',[dict(symbol=symbol,decision_time_ms=t,**f) for (symbol,t),f in sorted(features.items())])
            iso=[annotate(r,features) for r in isolated];files.write_csv(out/'isolated_event_diagnostics.csv',iso)
            accounts=read(z,'results/portfolio_comparison.csv')+[r for r in read(z6,'results/portfolio_comparison.csv') if r['policy'] in ('A','S_ATR1')]
            if len(accounts)!=160:raise ValueError('Wrong archived account count')
            audits=[];enriched=[];groups=[];rejections=[];overlaps=[];exit_matrix=[]
            for summary in accounts:
                identity={k:summary[k] for k in ('policy','model','period','scenario')};archive=z6 if summary['policy'] in ('A','S_ATR1') else z
                prefix='results/runs/'+'__'.join(summary[k] for k in ('model','period','scenario','policy'))
                trades=read(archive,prefix+'__trades.csv');curve=read(archive,prefix+'__equity.csv');decisions=read(archive,prefix+'__decisions.csv')
                audits.append(dict(**identity,**audit_account(summary,trades,curve,decisions)))
                rich=[dict(annotate(t,features),**identity) for t in trades];enriched+=rich;groups+=grouped(rich,dict(population='actual_account',**identity),lock['symbols'])
                status=Counter(d['status'] for d in decisions)
                rejections.extend(dict(**identity,decision=key,n=n) for key,n in sorted(status.items()))
                expected={p['event_id'] for p in plans if p['policy']==summary['policy'] and prior.PERIODS[summary['period']][0]<=int(p['registration_ms'])<prior.PERIODS[summary['period']][1]}
                if expected!={d['event_id'] for d in decisions}:raise ValueError('Account does not cover full registered population')
                for universe in UNIVERSES:
                    local=[r for r in rich if in_universe(r,universe)];start,end=prior.PERIODS[summary['period']]
                    ci=diag.block_difference(local,[],start,end)
                    overlaps.append(dict(population='actual_account',**identity,universe=universe,**diag.overlap_summary(local),**ci))
                    for reason in dict.fromkeys((*dimensions(lock['symbols'])['exit_reason'],*sorted({r['exit_reason'] for r in local}))):
                        exit_matrix.append(dict(**identity,universe=universe,exit_reason=reason,**diag.summarize([r for r in local if r['exit_reason']==reason])))
                print('ARCHIVE_ACCOUNT_AUDITED',*identity.values(),len(rich),flush=True)
            files.write_csv(out/'archived_accounts.csv',accounts);files.write_csv(out/'account_audit.csv',audits)
            files.write_csv(out/'actual_trade_diagnostics.csv',enriched);files.write_csv(out/'rejection_census.csv',rejections)
            files.write_csv(out/'exit_vs_net_loss.csv',exit_matrix)
            for policy,scenario,(period,(start,end)),side in itertools.product(('A','S_ATR1','T20_FAIL','T20_CONT'),('source_stop_first','feasible_lower','feasible_upper','source_slip15'),prior.PERIODS.items(),('long','short')):
                local=[r for r in iso if r['policy']==policy and r['scenario']==scenario and r['side']==side and start<=int(r['entry_time_ms'])<end]
                identity=dict(population='isolated_'+side,policy=policy,scenario=scenario,period=period,model='conditional_paper_'+side)
                groups+=grouped(local,identity,lock['symbols'])
                for universe in UNIVERSES:
                    subset=[r for r in local if in_universe(r,universe)]
                    overlaps.append(dict(**identity,universe=universe,**diag.overlap_summary(subset)))
            files.write_csv(out/'state_statistics.csv',groups);files.write_csv(out/'overlap_and_uncertainty.csv',overlaps)
            contrasts=[]
            for hid,policy in [('H_STATE','T20_CONT'),('H_COST','A')]:
                for scenario,(period,(start,end)),universe in itertools.product(('source_stop_first','source_slip15'),prior.PERIODS.items(),UNIVERSES):
                    local=[r for r in iso if r['policy']==policy and r['scenario']==scenario and r['side']=='long' and start<=int(r['entry_time_ms'])<end and in_universe(r,universe)]
                    field='ER_state_1h' if hid=='H_STATE' else 'cost_share_bin'
                    a,b=('efficient_move_proxy','range_proxy') if hid=='H_STATE' else ('<=.25','>.25')
                    ra=[r for r in local if r[field]==a];rb=[r for r in local if r[field]==b]
                    contrasts.append(dict(hypothesis=hid,policy=policy,scenario=scenario,period=period,universe=universe,
                        group_a=a,group_b=b,unclassified_or_middle=len(local)-len(ra)-len(rb),
                        group_a_stop_pct=diag.summarize(ra)['stop_rate_pct'],group_b_stop_pct=diag.summarize(rb)['stop_rate_pct'],
                        **diag.block_difference(ra,rb,start,end)))
            files.write_csv(out/'fixed_hypothesis_contrasts.csv',contrasts)
            eligible=[hid for hid in ('H_STATE','H_COST') if diag.hypothesis_eligible([r for r in contrasts if r['hypothesis']==hid])]
            files.write_json(out/'next_stage_hypotheses.json',dict(recommended_for_independent_mechanism_test=eligible,
                positive_expectancy_strategies=0,no_new_strategies_implemented=True,criteria_fixed_before_analysis=True,new_OOS=False,
                insufficient_or_inconsistent_hypotheses=[h for h in ('H_STATE','H_COST') if h not in eligible]))
            write_report(out,accounts,groups,contrasts,eligible,quality,snapshots,audits)
            draw(out,groups)
            result=dict(version='p1-08a-regime-diagnostics-v1',current_commit=os.getenv('GITHUB_SHA'),base_commit=BASE,
                source_run=43967126133,source_artifact=14276611830,source_zip_sha256=SOURCE_SHA,protocol_sha256=PROTOCOL_SHA,
                frozen_source_sha256={r['symbol']+'_'+r['frame']+'.csv':r['sha256'] for r in quality},frozen_candles=sum(r['bars'] for r in quality),
                source_output_hashes_verified=len(manifest['output_sha256']),account_profiles=len(accounts),all_accounts_verified=all(r['verified'] for r in audits),
                opportunity_rows=len(plans),opportunity_snapshot_rows=len(snapshots),isolated_outcome_rows=len(iso),
                actual_trade_rows=len(enriched),state_statistic_rows=len(groups),unique_feature_cutoffs=len(features),
                fixed_contrast_rows=len(contrasts),recommended_mechanism_hypotheses=eligible,positive_expectancy_strategies=0,
                strategy_replays=0,parameter_optimization=False,new_OOS=False,live_execution=False,
                code_sha256={name:files.sha((root/name).read_bytes()) for name in ('p1_08a_diagnostics.py','p1_08a_study.py')},
                output_sha256={str(p.relative_to(out)):files.sha(p.read_bytes()) for p in sorted(out.iterdir()) if p.is_file()})
            files.write_json(out/'p1_08a_manifest.json',result);return result


def write_report(out,accounts,groups,contrasts,eligible,quality,snapshots,audits):
    fmt=lambda v:'—' if v in (None,'') else f'{float(v):.6f}'
    lines=['# P1-08A｜市場狀態與失敗原因診斷完整實測','',
        '歷史探索，只讀P1-07及其封存A/S_ATR1對照；沒有重播策略、改參數、訓練AI或真實委託。先提交5dd6781凍結方法，再執行新增分析。完整價格結構規格見隨附P1_08A_STRUCTURE_FEASIBILITY.md。','',
        f"24份凍結檔，共{sum(r['bars'] for r in quality)}棒；零量{sum(r['zero_volume'] for r in quality)}、缺口{sum(r['gaps'] for r in quality)}、缺棒{sum(r['missing_bars'] for r in quality)}，均未補造。全部{len(audits)}個帳戶現金、2%正常風險、成本、拒絕、日曆及原曲線回撤對帳；不是重新模擬。",'',
        f'完整機會快照{len(snapshots)}列，registration與eligible-entry分開。最終拒絕理由不入特徵；未成交不標為虧損。只在原資料已明確給出進場時加entry特徵；不推測拒絕事件的假想進場。',
        '全部進場以1H執行代理為基礎；1H/4H/1D是同一事件的不同已知上下文，不是三套獨立交易。','',
        '## 成本與退出機制（純多主成本）','',
        '| 方案 | 時段 | 範圍 | n | SL% | 淨虧損% | 原價差R | 成本R | 淨R | 成本翻轉筆數 |',
        '|---|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for r in groups:
        if r['population']=='actual_account' and r['model']=='fractional_spot' and r['scenario']=='source_stop_first' and r['dimension']=='all':
            lines.append(f"| {r['policy']} | {r['period']} | {r['universe']} | {r['n']} | {fmt(r['stop_rate_pct'])} | {fmt(r['net_loss_rate_pct'])} | {fmt(r['mean_raw_R'])} | {fmt(r['mean_cost_R'])} | {fmt(r['mean_net_R'])} | {r['cost_flipped_trades']} |")
    lines+=['','stop與net loss定義不同；原價差與成本分解是固定已成交事件的帳面恒等式，不能把不付成本後的數字當可交易新策略。完整exit_reason交叉在exit_vs_net_loss.csv，正常SL與stop_gap分開。','',
        '## 已固定市場狀態分組（純多全期主成本）','',
        '| 方案 | 特徵 | 類別 | n | SL% | 勝率% | 淨R | PF |',
        '|---|---|---|---:|---:|---:|---:|---:|']
    for r in groups:
        if r['population']=='actual_account' and r['model']=='fractional_spot' and r['scenario']=='source_stop_first' and r['period']=='continuous' and r['universe']=='all_eight' and r['dimension'] in ('ER_state_1h','MA_4h','volatility_change_1h','rvol_bin_1h','turnover_bin_1h'):
            lines.append(f"| {r['policy']} | {r['dimension']} | {r['category']} | {r['n']} | {fmt(r['stop_rate_pct'])} | {fmt(r['win_rate_pct'])} | {fmt(r['mean_net_R'])} | {fmt(r['profit_factor'])} |")
    lines+=['','完整state_statistics.csv包括零樣本、BTC/其他七幣、各幣、年度、UTC時段、所有frame、四情境及孤立／帳戶母體，不能只引用表內正值。n<30標為small_sample；子群沒有另重播本金／MDD。','',
        '## 預登記機制假說與共同時間區塊不確定性','',
        '| 假說 | 時段 | 範圍 | 成本情境 | nA/nB | A淨R | B淨R | 差A−B | 95%區塊區間 |',
        '|---|---|---|---|---|---:|---:|---:|---|']
    for r in contrasts:
        lines.append(f"| {r['hypothesis']} | {r['period']} | {r['universe']} | {r['scenario']} | {r['group_a_n']}/{r['group_b_n']} | {fmt(r['group_a_mean_R'])} | {fmt(r['group_b_mean_R'])} | {fmt(r['difference_R'])} | [{fmt(r['difference_low95'])}, {fmt(r['difference_high95'])}] |")
    lines+=['',f'通過事前「值得進入P1-08B獨立機制檢驗」門檻：{", ".join(eligible) or "0個"}。正期望策略仍為0；機制關聯、低SL或高勝率不是策略驗證。',
        'H_STATE比較T20_CONT入場前ER高效率與range代理；中間組保留。H_COST比較A預期正常停損成本占比≤.25與>.25。兩者先使用所有原eligible long孤立結果，避免只分析單倉成交造成選擇偏差；仍受原eligible OHLC成交代理選樣，不能聲稱因果。',
        '共同28日區塊一次抽取所有幣，同一假說兩組同步抽；零事件區塊與未知重抽次數保留。重疊與長期regime依賴可能超出28日，區間依賴區塊假設，不是iid顯著性。所有Test早已開啟，不是新OOS。','',
        '## 時間可得性與資料缺口','',
        'unique_time_feature_audit.csv保存每個symbol/cutoff及三週期reference_close、lookback_start、未知狀態；所有reference_close<=decision。缺預期收盤棒不前填，進場棒最終OHLCV不入特徵。ATR水準與擴張不同，MA transition不是盤整真值，break/sweep僅描述當時已收盤行為，不能回填日後真假突破。',
        '原料只有OHLCV：成交額為close*base_volume代理，illiquidity為每USDT絕對close-return；不能重建spread、深度、CVD、吸收或精確實盤成交。歷史最小委託與借幣資格未取得；純多是條件紙上Spot，假想多空另模型，不能混統計。',
        '未提供meta gate獨立孤立結果，故只診斷其原帳戶，不補造新label。原A等未達eligible的事件沒有同等經濟結果；未知保留。帳戶及路徑情境多次出現同事件，禁止合併計有效樣本數。',
        '未重建盤中路徑；gap/path旗標、同棒退出與不確定價格保留。固定成本與原曲線稽核不等於真實可成交認證。帳戶最大回撤保留原全日曆值；分組PnL不可作同時可執行本金。','',
        '## 價格結構與下一輪','',
        '諧波／Fibonacci、艾略特／ABCDE、2B、OB、FVG、掃蕩及K線量能的確認／等待／事後類別與難度，見隨附結構可行性文件；沒有開發七套策略。現有harmonic/012345只是因果幾何子集，ABCDE與FVG尚無凍結detector。',
        '下一輪需先凍結規格與資料生效時間，收集全新前瞻候選（含觀望／拒絕）及可用時間；至少24個月、20個非空28日區塊、每臂200事件／30正30負、每臂30 BTC，固定日曆審查。樣本地板不是功效保證，不因既有Test漂亮提早停止或改條件。若本輪沒有通過假說，先完善量能／流動性可觀測性與前瞻日誌，不硬選候選或開新策略。','']
    (out/'P1_08A_RESULT_ZH.md').write_text('\n'.join(lines),encoding='utf-8')


def draw(out,groups):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    selected=[r for r in groups if r['population']=='actual_account' and r['model']=='fractional_spot' and r['scenario']=='source_stop_first' and r['policy']=='T20_CONT' and r['dimension']=='all' and r['period'] in ('train','validation','opened_test')]
    fig,ax=plt.subplots(figsize=(12,5));x=list(range(len(selected)))
    ax.bar(x,[r['mean_raw_R'] or 0 for r in selected],label='Raw-price mean R',color='#75aadb')
    ax.bar(x,[-(r['mean_cost_R'] or 0) for r in selected],label='Cost mean R',color='#d58888')
    ax.plot(x,[r['mean_net_R'] or 0 for r in selected],color='black',marker='o',label='Net mean R')
    ax.set_xticks(x,[r['period']+'\n'+r['universe']+'\nn='+str(r['n']) for r in selected],fontsize=8)
    ax.axhline(0,color='grey',linewidth=.8);ax.set_ylabel('Mean R at archived modeled risk')
    ax.set_title('T20_CONT: fixed executed events, price/cost identities; exploratory')
    ax.legend();fig.tight_layout();fig.savefig(out/'t20_cont_price_cost_diagnosis.png',dpi=160);plt.close(fig)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source-zip',required=True,type=Path);parser.add_argument('--out-dir',required=True,type=Path)
    args=parser.parse_args();run(args.source_zip,args.out_dir)
