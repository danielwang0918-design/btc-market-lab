"""Offline P1-05 report runner; no exchange account or order APIs."""
from __future__ import annotations
import argparse,csv,gzip,hashlib,json,os
from collections import Counter,defaultdict
from decimal import Decimal as D
from pathlib import Path
from statistics import mean
import p1_portfolio as m
import p1_portfolio_sources as source
import p1_candlestick_research as core

def csv_write(path,rows,default_fields=('no_rows',)):
    fields=list(dict.fromkeys(k for r in rows for k in r)) if rows else list(default_fields)
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)

def metrics(trades):
    pnls=[D(t['net_pnl_usdt']) for t in trades];rs=[D(t['r_multiple']) for t in trades]
    n=len(trades);wins=[p for p in pnls if p>0];losses=[p for p in pnls if p<0]
    streak=maximum=0
    for p in pnls:
        streak=streak+1 if p<0 else 0;maximum=max(maximum,streak)
    return dict(trade_count=n,wins=len(wins),losses=len(losses),flat_trades=n-len(wins)-len(losses),
        win_rate_pct=float(D(len(wins))*100/n) if n else None,
        average_R=float(sum(rs)/n) if n else None,net_expectancy_R=float(sum(rs)/n) if n else None,
        net_expectancy_USDT=float(sum(pnls)/n) if n else None,
        net_pnl_USDT=float(sum(pnls)),profit_factor=float(sum(wins)/-sum(losses)) if losses else None,
        profit_factor_status='defined' if losses else 'no_losses' if wins else 'no_trades_or_all_flat',
        maximum_consecutive_losses=maximum,
        total_fees_USDT=float(sum(D(t['fees_usdt']) for t in trades)),
        total_slippage_USDT=float(sum(D(t['slippage_usdt']) for t in trades)),
        risk_budget_exceeded_trades=sum(t['loss_exceeds_budget'] in (True,'True') for t in trades),
        cash_capped_trades=sum(t['cash_capped'] in (True,'True') for t in trades))

def audit_run(result,model):
    # Independent cash-flow arithmetic and interval checks, without using net_per_unit.
    equity=D(1000);last_exit=-1;fees=D(0);slip=D('.0015') if model.endswith('slip15') else D('.0005')
    for t in result['trades']:
        q=t['quantity'];e=t['entry_fill'];x=t['exit_fill'];side=t['side']
        if t['entry_time_ms']<last_exit or t['feature_cutoff_ms']>t['entry_time_ms']:raise ValueError('Overlap or future feature')
        if q<=0 or t['cash_required']>t['equity_before']+D('1e-20'):raise ValueError('Unfunded quantity')
        if t['planned_risk']>t['equity_before']*D('.02')+D('1e-20'):raise ValueError('Excess planned risk')
        if model!='hypothetical_1x_long_short' and side!='long':raise ValueError('Spot naked short')
        expected_fees=q*(e+x)*D('.001')
        gross=q*(x-e if side=='long' else e-x)
        net=gross-expected_fees
        if abs(net-t['net_pnl_usdt'])>D('1e-16') or abs(expected_fees-t['fees_usdt'])>D('1e-16'):raise ValueError('Cash/fee/PnL mismatch')
        equity+=net
        if abs(equity-t['equity_after'])>D('1e-15'):raise ValueError('Equity does not reconcile')
        last_exit=t['exit_time_ms'];fees+=expected_fees
    curve=result['curve'];peak=D(1000);dd=D(0)
    for row in curve:
        if row['cash_usdt']<0:raise ValueError('Negative free cash')
        peak=max(peak,row['equity_usdt']);dd=max(dd,(peak-row['equity_usdt'])/peak*100)
    if abs(dd-result['maximum_drawdown_pct'])>D('1e-15'):raise ValueError('Drawdown mismatch')
    if curve[0]['time_ms']!=result['calendar_start'] or curve[-1]['time_ms']!=result['calendar_end']:raise ValueError('Non-common endpoint')
    if not result['terminal_position_unresolved'] and abs(equity-result['final_equity'])>D('1e-15'):raise ValueError('Final equity mismatch')
    if len(result['trades'])>1000 or result['maximum_simultaneous_positions']>1:raise ValueError('Cap or overlapping positions')
    return dict(trades_checked=len(result['trades']),same_calendar_end=True,fee_cash_risk_reconciled=True,maximum_positions=result['maximum_simultaneous_positions'])

def plot(path,runs,period,model):
    os.environ.setdefault('MPLCONFIGDIR','/tmp/p1-05-mpl')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from datetime import datetime,timezone
    fig,ax=plt.subplots(figsize=(12,6))
    for policy,r in runs.items():
        ax.plot([datetime.fromtimestamp(x['time_ms']/1000,timezone.utc) for x in r['curve']],
                [float(x['equity_usdt']) for x in r['curve']],label=policy,linewidth=1.25)
    ax.axhline(1000,color='gray',linestyle='--',linewidth=.7)
    ax.set(title=f'Offline research only | {model} | {period}',ylabel='Liquidation-mark equity (USDT)',xlabel='Common calendar (UTC)')
    ax.legend();ax.grid(alpha=.2);fig.tight_layout();fig.savefig(path,dpi=140);plt.close(fig)

def annual_rows(result,policy,model,period):
    from datetime import datetime,timezone
    rows=[];curve=result['curve']
    first=datetime.fromtimestamp(result['calendar_start']/1000,timezone.utc).year
    last=datetime.fromtimestamp((result['calendar_end']-1)/1000,timezone.utc).year
    for year in range(first,last+1):
        lo=max(result['calendar_start'],int(datetime(year,1,1,tzinfo=timezone.utc).timestamp()*1000))
        hi=min(result['calendar_end'],int(datetime(year+1,1,1,tzinfo=timezone.utc).timestamp()*1000))
        before=[r for r in curve if r['time_ms']<=lo][-1];after=[r for r in curve if r['time_ms']<=hi][-1]
        ts=[t for t in result['trades'] if lo<t['exit_time_ms']<=hi]
        rows.append(dict(policy=policy,model=model,period=period,year=year,equity_start_USDT=str(before['equity_usdt']),
            equity_end_USDT=str(after['equity_usdt']),calendar_mark_pnl_USDT=str(after['equity_usdt']-before['equity_usdt']),**metrics(ts)))
    return rows

def run(data,tapes,trade_csv,quality,p104,out):
    out.mkdir(parents=True,exist_ok=True)
    protocol=Path(__file__).with_name('P1_05_PROTOCOL.json')
    if source.digest(protocol)!=source.PROTOCOL_SHA:raise ValueError('Pre-performance protocol changed')
    if source.digest(trade_csv)!=source.TRADE_SHA:raise ValueError('Original R source changed')
    q=json.loads(quality.read_text())
    if source.digest(quality)!='d8d67cf15b6a8611dfbbdb2d21566bde65fa7b4a43de54f3d6d7f300c9b6c5b6':raise ValueError('Quality source changed')
    if q['same_symbol_frame_horizon_any_overlap']!=301787:raise ValueError('Quality overlap baseline changed')
    prior=json.loads(p104.read_text())
    if source.digest(p104)!='e740acccae1b35a115a56b8d2c26167a3fc61efaa791e208cec7daad1e22a0b9':raise ValueError('P1-04 verified manifest SHA256 mismatch')
    if prior['unique_pattern_events']!=106433 or prior['observation_rows']!=319299:raise ValueError('P1-04 source counts changed')
    records=list(csv.DictReader(trade_csv.open(encoding='utf-8-sig')))
    legacy=m.legacy_replay(records);lts=legacy['trades'];values=[D(t['r_multiple']) for t in lts]
    legacy_summary=dict(trades=len(lts),wins=sum(v>0 for v in values),win_rate_pct=100*sum(v>0 for v in values)/len(values),
        mean_net_R=str(sum(values)/len(values)),final_USDT=str(legacy['final_equity']),costs_double_counted=False,
        matches_user_latest_reference=len(lts)==1000 and sum(v>0 for v in values)==210 and abs(legacy['final_equity']-D('4.226477'))<D('.000001'))
    csv_write(out/'legacy_causal_R_ledger.csv',lts)
    (out/'legacy_reproduction.json').write_text(json.dumps(legacy_summary,indent=2)+'\n')
    lock=core.read_lock(Path(__file__).with_name('PROSPECTIVE_LOCK_V1.json'))
    assets={sym:m.Asset(sym,data,tapes) for sym in lock['symbols']}
    summaries=[];symbols=[];years=[];audits=[];decisions=[]
    periods=dict(continuous=(m.START,m.END),train=(m.START,m.CUTS[0]),validation=(m.CUTS[0],m.CUTS[1]),opened_test=(m.CUTS[1],m.END))
    for model in m.MODELS:
        for period,(start,end) in periods.items():
            chart_runs={}
            for policy in m.POLICIES:
                print('RUN',model,period,policy,flush=True)
                r=m.run_account(policy,model,start,end,assets);chart_runs[policy]=r
                audited=audit_run(r,model);audits.append(dict(policy=policy,model=model,period=period,**audited))
                summary=dict(policy=policy,model=model,period=period,calendar_start_utc=m.utc(start),calendar_end_utc=m.utc(end),
                    initial_equity_USDT=1000,final_equity_USDT=str(r['final_equity']),maximum_drawdown_pct=str(r['maximum_drawdown_pct']),
                    in_market_hours=r['in_market_hours'],exposure_percent=100*r['in_market_hours']/((end-start)/m.H),
                    terminal_position_unresolved=r['terminal_position_unresolved'],unpriced_exposure_hours=r['unpriced_exposure_hours'],
                    **metrics(r['trades']))
                summaries.append(summary)
                for sym in lock['symbols']:symbols.append(dict(policy=policy,model=model,period=period,symbol=sym,**metrics([t for t in r['trades'] if t['symbol']==sym])))
                years.extend(annual_rows(r,policy,model,period))
                prefix=out/'runs'/f'{model}__{period}__{policy}'
                csv_write(prefix.with_name(prefix.name+'__trades.csv'),r['trades'],('symbol','setup_id','entry_time_ms','exit_time_ms','quantity','net_pnl_usdt','r_multiple'))
                csv_write(prefix.with_name(prefix.name+'__equity.csv'),r['curve'])
                csv_write(prefix.with_name(prefix.name+'__signals.csv'),r['signals'])
                decisions.extend(dict(policy=policy,model=model,period=period,event=k,count=v) for k,v in sorted(r['decision_counts'].items()))
            plot(out/f'equity_{model}__{period}.png',chart_runs,period,model)
    for row in summaries:
        base=next(r for r in summaries if r['model']==row['model'] and r['period']==row['period'] and r['policy']=='A')
        row['fewer_trades_than_A']=row['trade_count']<base['trade_count']
        row['beats_A_win_rate_and_expectancy']=bool(row['trade_count'] and base['trade_count'] and row['win_rate_pct']>base['win_rate_pct'] and row['net_expectancy_R']>base['net_expectancy_R'])
        row['positive_net_expectancy']=bool(row['trade_count'] and row['net_expectancy_R']>0)
        row['higher_cash_is_not_proof_of_edge']=row['fewer_trades_than_A'] and not row['positive_net_expectancy']
    csv_write(out/'strategy_comparison.csv',summaries);csv_write(out/'by_symbol.csv',symbols);csv_write(out/'by_year.csv',years)
    csv_write(out/'all_decision_counts.csv',decisions)
    (out/'independent_account_audit.json').write_text(json.dumps(audits,indent=2)+'\n')
    manifest=dict(version='p1-05-capital-backtest-v1',base_commit='515590e122880207e23936ae9c65905ad13b988e',protocol_sha256=source.PROTOCOL_SHA,
        current_commit=os.getenv('GITHUB_SHA'),source_sha256={
            'quality_manifest':source.digest(quality),'P1_04_manifest':source.digest(p104),'original_trade_csv':source.TRADE_SHA,
            'frozen_files':{name:meta['sha256'] for name,meta in lock['frozen_data_prefixes'].items()},
            'canonical_tapes':{p.name:source.digest(p) for p in sorted(tapes.glob('*.json.gz'))}},
        code_sha256={name:source.digest(Path(__file__).with_name(name)) for name in ('p1_portfolio.py','p1_portfolio_sources.py','p1_portfolio_study.py','historical_backtest.py','market_research.py','market_structure.py','binance_market_data.py','p1_candlestick_research.py','p1_zone_context.py','p1_market_regime.py','p1_volume_rvol.py')},
        run_count=len(summaries),independently_checked_accounts=len(audits),legacy_reproduction=legacy_summary,
        strict_spot_exchange_filters_available=False,verified_real_spot_trades=0,live_execution_implemented=False,new_OOS=False,
        rule_changes_to_original_modules=False,quality_overlap_fraction=301787/311873,
        limitations=['Historical exchange lot/notional and borrow rules are absent; strict model cannot certify any order.',
            'Fractional OHLC curves are conditional cost/cash simulations, not verified exchange executions.',
            'Hypothetical shorts reserve 1x collateral; no borrowing/funding/margin validation.',
            'Daily flat and hourly held liquidation marks are not tick-level maximum drawdown.',
            '2025-2026 Test opened; current survivor universe and serial/coin dependence preclude new OOS claims.',
            'User H5 source code/attachments unavailable; H5 numbers are not independently certified.'])
    (out/'p1_05_manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n')
    chinese_report(out,summaries,legacy_summary)
    return manifest

def chinese_report(out,rows,legacy):
    fmt=lambda x:'—' if x is None else f'{float(x):.6f}'
    lines=['# P1-05｜歷史訊號與有限本金模擬驗收','',
        '**純離線研究；沒有帳戶授權、委託 API、槓桿操作或自動交易功能。**','',
        '## 來源與獨立基準重現','',
        f"原始封存交易採 entry、symbol、setup_id 因果排序，重播 {legacy['trades']} 筆；勝 {legacy['wins']} 筆，平均 {legacy['mean_net_R']}R，最終 {legacy['final_USDT']} USDT。與使用者最新 B0 參考吻合：{legacy['matches_user_latest_reference']}。既有 R 已含成本，沒有重扣。",'',
        '先提交固定規格，再計算績效。完整 P0 引擎逐時候選訊號重新生成，並以未修改原引擎逐筆對回原始 1,340 筆交易；P1-05 不只篩選封存成交子集。',
        '全部方案使用 2020-10-15～2026-11-16 UTC 共同日曆，連續與 Train／Validation／已開啟 Test 分開，時段帳戶各以 1,000 USDT 開始。每筆按含成本停損距離預算 2%，現金不足縮小數量，全球單倉，同時訊號只依幣種與 setup ID 排序，最多 1,000 次建倉。','',
        '## 嚴格可執行性與條件性結果分開','',
        '凍結來源沒有歷史 LOT_SIZE／MIN_NOTIONAL、借幣資格及成交深度。strict_spot 因資料不足拒絕下單；0 筆、1,000 USDT 留存是觀望，不能稱為獲利優勢。下表 fractional_spot 只是假定連續數量與 OHLC 代理成交的**有現金限制純長倉研究**，不冒充已驗證交易所可成交績效。假想多空、同棒 target-first 及 15 bps 壓力另列。','',
        '## 條件性純長倉比較','',
        '| 時段 | 方案 | 筆數 | 勝率% | 平均淨 R | PF | 最終 USDT | 最大回撤% | 在市% |',
        '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for period in ('continuous','train','validation','opened_test'):
        for policy in m.POLICIES:
            r=next(x for x in rows if x['model']=='fractional_spot' and x['period']==period and x['policy']==policy)
            lines.append(f"| {period} | {policy} | {r['trade_count']} | {fmt(r['win_rate_pct'])} | {fmt(r['average_R'])} | {fmt(r['profit_factor'])} | {fmt(r['final_equity_USDT'])} | {fmt(r['maximum_drawdown_pct'])} | {fmt(r['exposure_percent'])} |")
    lines.extend(['','## 所有主模型與敏感度（共同截止）','',
        '| 模型 | 方案 | 筆數 | 勝率% | 淨 R | PF | 最終 USDT | 最大回撤% |',
        '|---|---|---:|---:|---:|---:|---:|---:|'])
    for model in m.MODELS:
        for p in m.POLICIES:
            r=next(x for x in rows if x['model']==model and x['period']=='continuous' and x['policy']==p)
            lines.append(f"| {model} | {p} | {r['trade_count']} | {fmt(r['win_rate_pct'])} | {fmt(r['average_R'])} | {fmt(r['profit_factor'])} | {fmt(r['final_equity_USDT'])} | {fmt(r['maximum_drawdown_pct'])} |")
    lines.extend(['','## 判讀與限制','',
        '- A/B/C/E 為不同政策，D 僅是全條件對照，不是強制所有指標同時成立的產品規則。訊號清單具時間戳、進場區、SL、原 TP1、RR、依據與失效；原策略未定義 TP2，明列空值，不補造。',
        '- 120 個帳戶是固定政策、時段與敏感度的對照，不能加總為獨立交易；每次帳戶只走有限原始歷史一次，不循環補足 1,000 筆。',
        '- strategy_comparison.csv 同列勝率與成本後 R。`beats_A_win_rate_and_expectancy` 需兩者同時改善；`higher_cash_is_not_proof_of_edge` 明列少交易而仍負期望的本金保留。0 筆或極少交易不能認證優勢。',
        '- 2% 是正常停損費用與 5 bps 滑價的模型預算，不保證實際損失≤2%；跳空與15 bps壓力的超額損失逐筆列出。',
        '- 原 P0 的策略規則未改。共同執行層拒絕無量／參考價不在棒內的代理成交，不使用進場棒未收盤完整性事後否決先前入場；缺口在下一個可見價格處理，不回填過去收盤成交。這些執行差異與現金／單倉限制使資金回測不能直接等同原每單位 R 曲線。',
        '- 最大回撤為含估計退出成本的1H收盤淨值；無報價時沿用已知估值並標記，不能假稱有精確棒內路徑或止損成交保證。',
        '- P1-QUALITY-01 的 96.766% 紙上窗口重疊不是交易筆數。新模型逐筆區間查核無重疊，但市場／時間依賴仍存在，不使用獨立試驗的顯著性推論。',
        '- 2025–2026 Test 已反覆查看，不是新 OOS。未見過的新前瞻資料及歷史交易所規則、點差／深度／借幣成本仍缺少。',
        '- 舊 H5 的程式附件未提供，因此不認證其81筆或431.523101 USDT。新 B/C/D 的固定實際入場特徵也不等同舊 H5；不得為吻合它而改參數。','',
        '## 交付檔案','',
        '- strategy_comparison.csv：120 個模型×時段×方案，含勝負、R／USDT期望、PF、最終本金、回撤、連虧、費用、滑價及在市時間。',
        '- runs/*__trades.csv、*__equity.csv、*__signals.csv：逐筆數量／風險／費用／本金與所有觀察、接受、拒絕訊號。',
        '- by_year.csv、by_symbol.csv：所有年份及固定八幣種，包括零交易組。',
        '- equity_*.png：所有模型／時段共同日曆曲線；不能以不同交易數終點直接比較。',
        '- canonical_reference.json：原引擎與封存1340筆的對帳；legacy_reproduction.json：1000筆因果R重播。',
        '- independent_account_audit.json、p1_05_manifest.json：現金／費用／風險／單倉／端點核對與來源程式SHA256。',''])
    (out/'P1_05_RESULT_ZH.md').write_text('\n'.join(lines),encoding='utf-8')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('data-dir','tapes','trade-csv','quality-manifest','p104-manifest','out-dir'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();run(a.data_dir,a.tapes,a.trade_csv,a.quality_manifest,a.p104_manifest,a.out_dir)
if __name__=='__main__':main()
