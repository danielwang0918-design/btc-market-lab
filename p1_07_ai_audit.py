"""P1-07C: read-only opportunity/label/as-of audit; no estimator or training."""
from __future__ import annotations
import argparse,csv,hashlib,io,json,math
from collections import Counter
from decimal import Decimal as D,InvalidOperation
from pathlib import Path
from zipfile import ZipFile

SOURCE_SHA='2b416875856defb20e714d366d3e4b61a22283cac8432e0f11131fdab00ece00'
VALIDATION=1672531200000
TEST=1735689600000
END=1794787200000
NUMERIC=('planned_net_RR','SL_distance_pct','TP_distance_pct','entry_delay_hours','atr_fraction','normal_stop_cost_fraction')
CATEGORICAL=('side','volatility','MA_1h','MA_4h','MA_1d')
LR_NUMERIC=('SL_distance_over_preentry_ATR','net_RR_at_entry','normal_stop_cost_share','log1p_known_registration_delay')
PROHIBITED=('status','anchor_status','exit_time_ms','exit_bar_ms','exit_reason','raw_exit','net_pnl_usdt','r_multiple',
    'fees_usdt','slippage_usdt','loss_exceeds_budget','path_dependent_outcome','gap_or_zero_volume_path_unknown',
    'quantity','equity_before','equity_after','event_id','setup_id','target_event_id','signal_index','reserved_until_ms')

def split(t):
    return 'train' if t<VALIDATION else 'validation' if t<TEST else 'opened_test'

def number(value):
    if value in ('',None):return None
    try:
        result=D(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation,ValueError):return None

def features(p):
    values={key:number(p.get(key)) for key in NUMERIC[:4]}
    e,s,atr=number(p.get('raw_entry')),number(p.get('stop')),number(p.get('atr'))
    values['atr_fraction']=atr/e if atr is not None and e is not None and e>0 else None
    values['normal_stop_cost_fraction']=None
    if e is not None and s is not None and e>0 and s>0:
        sign=D(1) if p['side']=='long' else D(-1)
        ef=e*(1+sign*D('.0005'));sf=s*(1-sign*D('.0005'))
        risk=(ef-sf)*sign+(ef+sf)*D('.001')
        if risk>0:values['normal_stop_cost_fraction']=(risk-abs(e-s))/risk
    values.update({key:p.get(key) or 'unknown' for key in CATEGORICAL})
    return values

def audit_rows(plans,outcomes):
    plans=[p for p in plans if p.get('policy')=='A']
    selected=[r for r in outcomes if r.get('policy')=='A' and r.get('scenario')=='source_stop_first']
    labels={}
    for row in selected:
        if row['event_id'] in labels:raise ValueError('duplicate source label')
        labels[row['event_id']]=row
    if len({p['event_id'] for p in plans})!=len(plans):raise ValueError('duplicate opportunity')
    output=[]
    for p in plans:
        reg=int(p['registration_ms']);sample_split=split(reg)
        row=dict(event_id=p['event_id'],symbol=p['symbol'],side=p['side'],registration_ms=reg,
            split=sample_split,source_status=p['status'],entry_time_ms=p.get('entry_time_ms',''),
            feature_cutoff_ms=p.get('feature_cutoff_ms',''),exit_time_ms='',label_net_positive='',label_stop='',
            audit_status='no_execution_label',complete_case_train_ready=False,missing_numeric_features='')
        if p['status']!='eligible':
            if p['event_id'] in labels:raise ValueError('noneligible opportunity has source label')
            output.append(row);continue
        if p['event_id'] not in labels:raise ValueError('eligible opportunity missing source label')
        label=labels[p['event_id']];entry=int(p['entry_time_ms']);end=int(label['exit_time_ms'])
        row.update(entry_time_ms=entry,exit_time_ms=end,**features(p))
        missing=[f for f in NUMERIC if row[f] is None]
        row['missing_numeric_features']='|'.join(missing)
        cutoff=int(p.get('feature_cutoff_ms') or entry+1)
        if cutoff>entry or reg>entry or int(p.get('signal_time_ms') or reg)>reg or int(p.get('setup_confirm_ms') or reg)>=reg:
            status='future_feature'
        elif end<entry:status='invalid_label_interval'
        elif split(entry)!=sample_split or end>{'train':VALIDATION,'validation':TEST,'opened_test':END}[sample_split]:
            status='label_crosses_split'
        elif any(label.get(key) not in (True,False,'True','False') for key in ('path_dependent_outcome','gap_or_zero_volume_path_unknown')):
            status='unknown_label_quality'
        elif label.get('path_dependent_outcome') in (True,'True'):status='path_dependent_label'
        elif label.get('gap_or_zero_volume_path_unknown') in (True,'True'):status='gap_or_zero_volume_label'
        else:status='known_conditional_paper_label'
        row['audit_status']=status
        if status=='known_conditional_paper_label':
            row['label_net_positive']=int(D(label['net_pnl_usdt'])>0)
            row['label_stop']=int(label['exit_reason'] in ('stop','stop_gap'))
            row['complete_case_train_ready']=not missing
        output.append(row)
    if set(labels)-{p['event_id'] for p in plans}:raise ValueError('source label outside opportunity universe')
    return output

def overlap(rows):
    active=[];pairs=0;affected=set();maximum=0
    for row in sorted(rows,key=lambda r:(int(r['entry_time_ms']),r['event_id'])):
        t=int(row['entry_time_ms']);active=[(end,key) for end,key in active if end>t]
        if active:affected.add(row['event_id']);affected.update(key for _,key in active)
        pairs+=len(active);active.append((int(row['exit_time_ms']),row['event_id']));maximum=max(maximum,len(active))
    return dict(pair_overlaps=pairs,events_with_overlap=len(affected),maximum_concurrent_labels=maximum)

def summarize(rows):
    results=[]
    for model in ('fractional_spot','hypothetical_1x_long_short'):
        for period in ('all','train','validation','opened_test'):
            selected=[r for r in rows if (model!='fractional_spot' or r['side']=='long') and (period=='all' or r['split']==period)]
            eligible=[r for r in selected if r['entry_time_ms']!='']
            known=[r for r in selected if r['audit_status']=='known_conditional_paper_label']
            complete=[r for r in known if r['complete_case_train_ready']]
            results.append(dict(model=model,split=period,direction_supported_opportunities=len(selected),
                source_eligible=len(eligible),known_paper_labels=len(known),known_positive=sum(r['label_net_positive'] for r in known),
                known_negative=sum(r['label_net_positive']==0 for r in known),complete_case_rows=len(complete),
                complete_case_positive=sum(r['label_net_positive'] for r in complete),
                complete_case_negative=sum(r['label_net_positive']==0 for r in complete),
                **Counter(r['audit_status'] for r in selected),**overlap(known)))
    return results

def write_csv(path,rows):
    fields=list(dict.fromkeys(k for row in rows for k in row)) or ['no_rows']
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(rows)

def meta_features(row):
    pct=number(row.get('SL_distance_pct'));atr_fraction=number(row.get('atr_fraction'))
    delay=number(row.get('entry_delay_hours'))
    return dict(SL_distance_over_preentry_ATR=float(pct/100/atr_fraction) if pct is not None and atr_fraction is not None and atr_fraction>0 else None,
        net_RR_at_entry=float(row['planned_net_RR']) if number(row.get('planned_net_RR')) is not None else None,
        normal_stop_cost_share=float(row['normal_stop_cost_fraction']) if number(row.get('normal_stop_cost_fraction')) is not None else None,
        log1p_known_registration_delay=math.log1p(float(delay)) if delay is not None and delay>=0 else None)

def fit_meta(rows):
    """One fixed train-only fit. Future label quality never enters prediction admission."""
    pool=[r for r in rows if r['side']=='long' and r['source_status']=='eligible']
    if any(int(r['feature_cutoff_ms'])>int(r['entry_time_ms']) or int(r['registration_ms'])>int(r['entry_time_ms']) for r in pool):
        raise ValueError('future feature in prediction population')
    train=[r for r in pool if r['split']=='train' and r['audit_status']=='known_conditional_paper_label' and int(r['exit_time_ms'])<VALIDATION]
    positive=sum(r['label_net_positive'] for r in train);negative=len(train)-positive
    state=dict(model_trained=False,train_status='sample_or_class_gate_failed',training_rows=len(train),
        training_positive=positive,training_negative=negative,features=LR_NUMERIC,
        C=1.0,solver='liblinear',class_weight=None,random_state=0,max_iter=1000,threshold=0.5,
        training_label_end_exclusive_ms=VALIDATION,train_only_imputation_and_scaling=True,
        future_label_quality_not_used_for_prediction_admission=True,large_neural_network_trained=False,new_OOS=False)
    model=None
    if len(train)>=200 and positive>=30 and negative>=30:
        import numpy as np
        import sklearn,scipy
        from sklearn.impute import SimpleImputer
        from sklearn.preprocessing import StandardScaler
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        X=np.array([[meta_features(r)[key] if meta_features(r)[key] is not None else np.nan for key in LR_NUMERIC] for r in train],dtype=float)
        if np.any(np.all(np.isnan(X),axis=0)):
            state['train_status']='training_feature_entirely_unavailable'
        else:
            model=Pipeline([('imputer',SimpleImputer(strategy='median')),('scaler',StandardScaler()),
                ('classifier',LogisticRegression(C=1.0,solver='liblinear',class_weight=None,random_state=0,max_iter=1000))])
            model.fit(X,np.array([r['label_net_positive'] for r in train],dtype=int))
            state.update(model_trained=True,train_status='trained_once_frozen_spec',
                imputer_statistics=model.named_steps['imputer'].statistics_.tolist(),
                scaler_mean=model.named_steps['scaler'].mean_.tolist(),scaler_scale=model.named_steps['scaler'].scale_.tolist(),
                scaler_variance=model.named_steps['scaler'].var_.tolist(),
                coefficients=model.named_steps['classifier'].coef_[0].tolist(),
                intercept=model.named_steps['classifier'].intercept_.tolist(),classes=model.named_steps['classifier'].classes_.tolist(),
                n_iter=model.named_steps['classifier'].n_iter_.tolist(),
                package_versions=dict(numpy=np.__version__,scipy=scipy.__version__,sklearn=sklearn.__version__))
    predictions=[]
    for r in pool:
        f=meta_features(r);share=f['normal_stop_cost_share'];probability=''
        if model is not None:
            import numpy as np
            X=np.array([[f[key] if f[key] is not None else np.nan for key in LR_NUMERIC]],dtype=float)
            probability=float(model.predict_proba(X)[0,1])
        predictions.append(dict(event_id=r['event_id'],symbol=r['symbol'],entry_time_ms=r['entry_time_ms'],
            registration_ms=r['registration_ms'],period=r['split'],probability=probability,
            accept_ml=probability!='' and probability>=.5,
            accept_fixed=share is not None and share<=.25,**f))
    lookup={r['event_id']:r for r in pool};scores=[]
    for period in ('train','validation','opened_test'):
        trusted=[p for p in predictions if p['period']==period and lookup[p['event_id']]['audit_status']=='known_conditional_paper_label']
        unknown=sum(p['period']==period for p in predictions)-len(trusted)
        for gate in ('accept_all','fixed_cost_share','meta_LR'):
            if gate=='meta_LR' and model is None:
                scores.append(dict(period=period,gate=gate,status='not_trained',trusted_labels=len(trusted),unknown_labels_excluded=unknown));continue
            y=[lookup[p['event_id']]['label_net_positive'] for p in trusted]
            pred=[1 if gate=='accept_all' else int(p['accept_fixed'] if gate=='fixed_cost_share' else p['accept_ml']) for p in trusted]
            tp=sum(a==1 and b==1 for a,b in zip(y,pred));tn=sum(a==0 and b==0 for a,b in zip(y,pred))
            fp=sum(a==0 and b==1 for a,b in zip(y,pred));fn=sum(a==1 and b==0 for a,b in zip(y,pred))
            s=dict(period=period,gate=gate,status='trusted_conditional_paper_labels_only',trusted_labels=len(y),unknown_labels_excluded=unknown,
                predicted_accepts=sum(pred),true_positive=tp,true_negative=tn,false_positive=fp,false_negative=fn,
                accuracy=(tp+tn)/len(y) if y else None,precision=tp/(tp+fp) if tp+fp else None,
                recall=tp/(tp+fn) if tp+fn else None)
            if gate=='meta_LR' and y:
                from sklearn.metrics import log_loss,roc_auc_score,brier_score_loss
                probabilities=[p['probability'] for p in trusted]
                s.update(log_loss=float(log_loss(y,probabilities,labels=[0,1])),brier_score=float(brier_score_loss(y,probabilities)),
                    AUC=float(roc_auc_score(y,probabilities)) if len(set(y))==2 else None)
            scores.append(s)
    return state,predictions,scores

def run(source_zip:Path,out:Path):
    if out.exists() and any(out.iterdir()):raise ValueError('Output directory must be empty')
    if hashlib.sha256(source_zip.read_bytes()).hexdigest()!=SOURCE_SHA:raise ValueError('Pinned source SHA256 changed')
    out.mkdir(parents=True,exist_ok=True)
    with ZipFile(source_zip) as archive:
        def records(name):
            with archive.open(name) as raw:
                return [r for r in csv.DictReader(io.TextIOWrapper(raw,encoding='utf-8-sig')) if r.get('policy')=='A']
        plans=records('results/candidate_events.csv');outcomes=records('results/event_outcomes.csv')
    rows=audit_rows(plans,outcomes);counts=summarize(rows)
    ready=[]
    for r in rows:
        if not r['complete_case_train_ready']:continue
        for model in ('fractional_spot','hypothetical_1x_long_short'):
            if model=='fractional_spot' and r['side']!='long':continue
            ready.append(dict(model=model,event_id=r['event_id'],symbol=r['symbol'],split=r['split'],
                entry_time_ms=r['entry_time_ms'],label_end_ms=r['exit_time_ms'],
                label_net_positive=r['label_net_positive'],label_stop=r['label_stop'],
                **{key:r[key] for key in NUMERIC+CATEGORICAL}))
    write_csv(out/'ai_opportunity_audit.csv',rows);write_csv(out/'ai_split_census.csv',counts);write_csv(out/'ai_complete_case_features.csv',ready)
    train_state,predictions,classification=fit_meta(rows)
    write_csv(out/'meta_predictions.csv',predictions);write_csv(out/'meta_classification.csv',classification)
    (out/'meta_model.json').write_text(json.dumps(train_state,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
    manifest=dict(version='p1-07c-data-feasibility-v1',source_sha256=SOURCE_SHA,raw_opportunities=len(rows),
        raw_eligible=sum(r['entry_time_ms']!='' for r in rows),numeric_feature_whitelist=NUMERIC,
        categorical_feature_whitelist=CATEGORICAL,prohibited_feature_fields=PROHIBITED,
        audit_status_counts=dict(Counter(r['audit_status'] for r in rows)),
        complete_registration_feature_table_available=False,credible_real_execution_labels_available=False,
        conditional_paper_logistic_fit_possible=all(next(r for r in counts if r['model']==model and r['split']=='train')['complete_case_positive']>0 and
            next(r for r in counts if r['model']==model and r['split']=='train')['complete_case_negative']>0 for model in ('fractional_spot','hypothetical_1x_long_short')),
        model_trained=train_state['model_trained'],train_status=train_state['train_status'],training_rows=train_state['training_rows'],
        prediction_file='meta_predictions.csv',prediction_rows=len(predictions),classification_file='meta_classification.csv',
        trained_numeric_features=LR_NUMERIC,large_neural_network_trained=False,new_OOS=False,live_execution=False,
        code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    report(out,counts,manifest,train_state,classification)
    manifest['output_sha256']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.iterdir()) if p.is_file()}
    (out/'ai_audit_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
    return manifest

def report(out,counts,manifest,train_state,classification):
    lines=['# P1-07C｜AI／Meta-Labeling資料可行性實測','',
        '先稽核資料；事前門檻通過後只以Train單次擬合固定四特徵Logistic Regression，不重跑P1-06B策略；不能把條件式紙上標籤當成真實成交或全新OOS。','',
        f"完整原A母體 {manifest['raw_opportunities']} 個，其中 {manifest['raw_eligible']} 個有單一source_stop_first孤立事件結果；其餘未成交機會保留未知，不標成虧損。",'',
        '| 模型 | 時段 | 可用方向機會 | 有結果 | 可信條件紙上標籤 | 正／負 | 數值完整列 | 數值完整正／負 | 重疊事件／配對 | 最大同時標籤 |',
        '|---|---|---:|---:|---:|---|---:|---|---|---:|']
    for r in counts:
        lines.append(f"| {r['model']} | {r['split']} | {r['direction_supported_opportunities']} | {r['source_eligible']} | {r['known_paper_labels']} | {r['known_positive']}／{r['known_negative']} | {r['complete_case_rows']} | {r['complete_case_positive']}／{r['complete_case_negative']} | {r['events_with_overlap']}／{r['pair_overlaps']} | {r['maximum_concurrent_labels']} |")
    lines += ['', '## 固定Meta-Labeling實驗','',
        f"訓練狀態：{train_state['train_status']}；訓練{train_state['training_rows']}列、正{train_state['training_positive']}／負{train_state['training_negative']}，全部label終點嚴格早於2023 cutoff。預測包含全部{manifest['prediction_rows']}個long eligible機會，未因未來gap／path品質遮罩拒絕。",'',
        '| 時段 | 比較 | 可信標籤 | 未知排除 | 預測接受 | accuracy | precision | recall | AUC |','|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for r in classification:
        fmt=lambda value:'—' if value is None else f'{float(value):.6f}'
        lines.append(f"| {r['period']} | {r['gate']} | {r['trusted_labels']} | {r['unknown_labels_excluded']} | {r.get('predicted_accepts','—')} | {fmt(r.get('accuracy'))} | {fmt(r.get('precision'))} | {fmt(r.get('recall'))} | {fmt(r.get('AUC'))} |")
    lines += ['', '## 可行性與限制','',
        '- 訓練期有兩類標籤，數學上可估計小型Logistic Regression，不能籠統說完全沒有資料。可研究的是原A已到達入場階段的conditional paper meta-label，不是完整4583登記事件的普遍獲利分類器。',
        '- 未達入場階段事件缺少統一登記時點的完整MA／ATR特徵，也沒有同時明確的可執行入場／退出標籤；source status與最終拒絕原因需要未來路徑，禁止作登記時點特徵或將其當虧損標籤。',
        '- 同棒路徑依賴與缺口標籤留空。此品質遮罩只用於訓練／標籤稽核，不是進場時可知條件，不能在回測中預先避開未來會有缺口／歧義的交易。驗證須揭露未評估樣本比例。',
        '- numeric/categorical whitelist僅使用已知價格幾何、入場等待時間、事前ATR與原MA分類。所有退出、實現費用／PnL、路徑旗標、成交／拒絕狀態、倉位、淨值與ID禁止入模。未知MA類別保留，不用全歷史填補數值。',
        '- 標籤終點不越過Train／Validation／已開啟Test；仍有同段重疊與跨幣市場依賴。不得隨機切分或以iid計顯著性；任何交叉驗證須purge標籤區間重疊並考慮共同市場時間。',
        '- 本次固定LR：四numeric為SL/事前ATR、入場netRR、normal stop成本占比、log1p登記等待時間；SimpleImputer median與StandardScaler只fit Train；C1/liblinear/random0/max_iter1000、class_weight=None、門檻0.5，不調參。固定規則為事前成本占風險≤0.25，與accept-all同母體比較。分類不等同交易驗收，Test只作已開啟歷史探索。準確率高但拒絕所有交易、或少交易保本，不算提高期望。',
        '- 歷史最小委託、深度、排隊、借幣與真實成交結果欠缺；可擬合不等於可信實盤訓練資料。現階段不得訓練大型神經網路或宣稱AI找到可交易優勢。','']
    (out/'AI_FEASIBILITY_RESULT_ZH.md').write_text('\n'.join(lines),encoding='utf-8')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source-zip',required=True,type=Path);parser.add_argument('--out-dir',required=True,type=Path)
    args=parser.parse_args();run(args.source_zip,args.out_dir)
