"""Read-only paired evaluation/resource views. No historical repricing or judgments."""
import math
from .pricing import format_cost
from .human_evaluation import valid_score


def number(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value >= 0


def cost_view(run):
    cost=run.get('estimated_api_cost')
    if number(cost):
        return {'cost_display':format_cost(cost,run.get('currency')),'cost_available':True,
                'cost_status':run.get('cost_estimation_method') or '历史费用原值，元数据未记录'}
    reason=run.get('cost_unavailable_reason')
    explanations={
        'legacy_price_metadata_missing':'历史未保存可用费用；不使用当前价格补算',
        'request_date_outside_confirmed_calendar':'请求日期超出已确认计价日历',
        'holiday_calendar_unconfirmed':'计价日历未确认',
        'cache_usage_missing':'缺少缓存用量明细',
        'model_price_missing':'缺少对应模型价格',
        'price_metadata_missing':'缺少价格来源、核对日期或币种',
        'price_null_or_invalid':'价格未确认或无效',
        'invalid_or_missing_token_usage':'缺少有效计费用量',
    }
    return {'cost_display':'Unavailable','cost_available':False,
            'cost_status':explanations.get(reason, '费用不可用：'+reason if reason else '历史未记录可用费用；不是零费用')}


def performance(run):
    human=run.get('human_evaluation')
    if human:
        return human['completion_status'],human['quality_score']
    history=run.get('completion_evaluation_history') or []
    if history:
        status={'completed':'Completed','incomplete':'历史 incomplete（未细分）','pending':'待评价'}.get(history[-1]['status'],'未知')
    else:
        status='待人工评价'
    score=(run.get('quality') or {}).get('score')
    return status,score if valid_score(score) else None


def baseline_pairs(runs):
    """One row per non-baseline run; missing/duplicate baseline never silently picked."""
    groups={}
    for run in runs:
        key=('experiment',run.get('experiment_id')) if run.get('experiment_id') else ('run',run['run_id'])
        groups.setdefault(key,[]).append(run)
    rows=[]
    for group in groups.values():
        references=[r for r in group if r.get('mode')=='baseline']
        baseline=references[0] if len(references)==1 else None
        for run in group:
            if run.get('mode')=='baseline':
                continue
            status,score=performance(run)
            b_status,b_score=performance(baseline) if baseline else (None,None)
            human=run.get('human_evaluation') or {}
            cost=cost_view(run)
            matched=None
            if baseline and 'original_prompt' in baseline and 'original_prompt' in run:
                matched=baseline['original_prompt']==run['original_prompt'] and baseline.get('original_context','')==run.get('original_context','')
            def reduction(field):
                a=baseline.get(field) if baseline else None
                b=run.get(field)
                return (a-b)/a if matched is True and number(a) and number(b) and a > 0 else None
            same_currency=bool(baseline and baseline.get('currency') and baseline.get('currency')==run.get('currency'))
            cr=reduction('estimated_api_cost') if same_currency else None
            relative=human.get('baseline_comparison')
            if baseline is None:
                evidence='缺少或存在多条 Baseline，无法建立唯一对照'
            elif matched is not True:
                evidence='输入不一致或输入证据缺失，不能直接对照'
            elif status not in ('Completed','待人工评价','待评价') or b_status not in ('Completed','待人工评价','待评价'):
                evidence='任务未完全完成或不适用，不认定有效优化'
            elif status!='Completed' or b_status!='Completed':
                evidence='完成评价尚未齐全；仅描述资源差异'
            elif score is None or b_score is None:
                evidence='缺少适用的质量评价'
            elif relative is None:
                evidence='相对质量尚未判断；同分不自动等于 Similar'
            elif relative=='Cannot compare':
                evidence='人工标记无法比较相对质量'
            elif relative=='Worse':
                evidence='人工报告质量下降；需按预设质量门槛判断'
            elif cr is None:
                evidence='费用对照不可用；可看 Token / Latency，综合收益待判断'
            else:
                evidence='完成与相对质量证据齐全；收益仍需结合预设资源目标'
            cross=bool(baseline and (baseline.get('selected_model')!=run.get('selected_model') or
                baseline.get('response_model') and run.get('response_model') and baseline['response_model']!=run['response_model']))
            rows.append({'experiment_id':run.get('experiment_id'),'baseline_run_id':baseline.get('run_id') if baseline else None,
                         'run_id':run['run_id'],'mode':run.get('mode'),'model':run.get('selected_model'),
                         'input_matched':matched,'cross_model_comparison':cross,
                         'completion_status':status,'baseline_completion_status':b_status,
                         'quality_score':score,'baseline_quality_score':b_score,
                         'quality_difference':score-b_score if matched is True and score is not None and b_score is not None else None,
                         'baseline_comparison':relative,'manual_optimization_effect':human.get('optimization_effect'),
                         'cost':run.get('estimated_api_cost'),'currency':run.get('currency'),
                         'cost_display':cost['cost_display'],'cost_status':cost['cost_status'],
                         'cost_reduction_ratio':cr,'token_reduction_ratio':reduction('total_tokens'),
                         'latency_reduction_ratio':reduction('latency_seconds'),
                         'evidence_status':evidence})
    return rows


def paired_csv(runs):
    import csv,io
    rows=baseline_pairs(runs)
    stream=io.StringIO(newline='')
    writer=csv.DictWriter(stream,fieldnames=list(rows[0]) if rows else ['experiment_id','baseline_run_id','run_id'])
    writer.writeheader();writer.writerows(rows)
    return stream.getvalue()
