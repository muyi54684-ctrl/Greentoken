"""Product report: simulated metrics and measured API metrics stay distinct."""
import math
import copy
from datetime import datetime, time, date, timezone

from ..diagnostics import COST_CAUTION
from ..pricing import calculate_cost
from ..pricing import format_cost


def valid_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def reduction(baseline, green):
    return (baseline - green) / baseline if valid_number(baseline) and baseline > 0 and valid_number(green) else None


def mock_pricing_time(config):
    """Mock metadata date; does not select or expire a Gateway price version."""
    profile = config.get('gateway_pricing') or {}
    if profile:
        return datetime.combine(date.fromisoformat(profile['checked_at']), time(), timezone.utc)
    calendar = (config.get('pricing') or {}).get('calendar') or {}
    try:
        return datetime.combine(date.fromisoformat(calendar['valid_until']), time(), timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None


def resource_row(record, config, mock, fixture_time):
    profile = config.get('gateway_pricing') or {}
    if mock and not profile:
        price = calculate_cost(record.get('raw_usage'), record['selected_model'], config, fixture_time) if fixture_time else {}
        cost = price.get('estimated_api_cost')
        unavailable = price.get('cost_unavailable_reason') or ('mock_pricing_date_missing' if not fixture_time else None)
        latency = record.get('mock_latency_seconds')
        pricing_timestamp = fixture_time.isoformat() if fixture_time else None
    else:
        cost = record.get('estimated_api_cost')
        unavailable = record.get('cost_unavailable_reason')
        latency = record.get('mock_latency_seconds') if mock else record.get('latency_seconds')
        pricing_timestamp = record.get('request_timestamp')
    usage = record.get('raw_usage') or {}
    available_usage = {k: usage[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens',
                       'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens', 'reasoning_tokens')
                       if k in usage and (usage[k] is None or isinstance(usage[k], int) and not isinstance(usage[k], bool))}
    for name, field in [('prompt_tokens_details', 'cached_tokens'), ('completion_tokens_details', 'reasoning_tokens')]:
        nested = usage.get(name)
        if isinstance(nested, dict) and field in nested and (nested[field] is None or isinstance(nested[field], int) and not isinstance(nested[field], bool)):
            available_usage[name] = {field: nested[field]}
    return {'mode': record['mode'], 'model': record['selected_model'],
            'response_model': record.get('response_model'), 'response_id': record.get('response_id'),
            'run_id': record.get('run_id'), 'usage': available_usage,
            'budget': record['requested_output_budget'], 'input_tokens': record.get('input_tokens'),
            'output_tokens': record.get('output_tokens'), 'total_tokens': record.get('total_tokens'),
            'cost': cost if valid_number(cost) else None, 'currency': config.get('currency'),
            'cost_unavailable_reason': unavailable, 'pricing_timestamp': pricing_timestamp,
            'latency_seconds': latency, 'metric_source': 'mock_fixture' if mock else 'api_response',
            'cost_source': 'gateway_reference_on_simulated_usage' if mock and profile else 'gateway_reference_on_api_usage' if profile else 'simulated_usage_at_scenario_date' if mock else 'recorded_usage_at_request_time',
            'pricing_version': profile.get('version'), 'pricing_signature': profile.get('signature'),
            'pricing_policy': profile.get('policy'), 'price_source': profile.get('price_source'),
            'price_checked_at': profile.get('checked_at'),
            'price_snapshot': copy.deepcopy((profile.get('models') or {}).get(record['selected_model'])),
            'cost_estimation_method': record.get('cost_estimation_method'),
            'cost_assumptions': (['Fixed reference rates; time-of-day/holiday discounts not applied'] if profile else []) +
                                (['Cache details missing; input assumed uncached for cost only'] if record.get('cost_estimation_method') == 'assume_uncached_input' else [])}


def change_details(baseline, green, noun, comparable=True):
    if not comparable or not valid_number(baseline) or not valid_number(green):
        return {'change_ratio': None, 'direction': 'unknown', 'change_display': 'Unknown',
                'change_label': 'Unknown', 'change_summary': 'Unknown'}
    direction = 'increase' if green > baseline else 'reduction' if green < baseline else 'unchanged'
    ratio = (green - baseline) / baseline if baseline > 0 else None
    label = f'{noun} Increase' if direction == 'increase' else f'{noun} Reduction' if direction == 'reduction' else 'No Change'
    display = 'Unknown (Baseline = 0)' if ratio is None else '0.0%' if ratio == 0 else f'{ratio:+.1%}'
    if direction == 'unchanged':
        summary = 'No Change'
    elif ratio is None:
        summary = 'Increase (percentage unavailable)'
    else:
        summary = ('Increase' if direction == 'increase' else 'Reduction') + f' {abs(ratio):.1%}'
    return {'change_ratio': ratio, 'direction': direction, 'change_display': display,
            'change_label': label, 'change_summary': summary}


def resource_comparison(resources, comparison_mode='full_green'):
    baseline, green = resources.get('baseline', {}), resources.get(comparison_mode, {})
    metrics = []
    for label, field in [('Total Tokens', 'total_tokens'), ('API Cost', 'cost'), ('Latency (s)', 'latency_seconds')]:
        ratio = reduction(baseline.get(field), green.get(field))
        comparable = True
        reason = None
        if field == 'cost' and (not baseline.get('currency') or baseline.get('currency') != green.get('currency')):
            comparable, reason = False, 'currency_mismatch_or_missing'
        elif field == 'cost' and baseline.get('pricing_signature') != green.get('pricing_signature'):
            comparable, reason = False, 'pricing_version_mismatch'
        if not comparable:
            ratio = None
        noun = {'total_tokens': 'Token', 'cost': 'Cost', 'latency_seconds': 'Latency'}[field]
        metrics.append({'metric': label, 'baseline': baseline.get(field), 'greenai': green.get(field),
                        'reduction_ratio': ratio, 'comparison_unavailable_reason': reason,
                        **change_details(baseline.get(field), green.get(field), noun, comparable)})
    return metrics


def comparison_rows(report, comparison_mode='full_green'):
    resources = {r['mode']: r for r in report['resources']}
    baseline, green = resources.get('baseline', {}), resources.get(comparison_mode, {})
    rows = []
    for metric in resource_comparison(resources, comparison_mode):
        b, g = metric['baseline'], metric['greenai']
        if metric['metric'] == 'API Cost':
            b = format_cost(b, baseline.get('currency')) if b is not None else None
            g = format_cost(g, green.get('currency')) if g is not None else None
        rows.append({'Metric': metric['metric'], 'Baseline': str(b) if b is not None else 'Unknown',
                     'GreenAI': str(g) if g is not None else 'Unknown', 'Change': metric['change_display'],
                     'Label': metric['change_label']})
    return rows


def build_report(decision, records, errors, config, mock, failures=()):
    fixture_time = mock_pricing_time(config) if mock else None
    resources = {mode: resource_row(r, config, mock, fixture_time) for mode, r in records.items()}
    counts = [r.get('api_calls') for r in records.values()] + [f.get('api_calls') for f in failures]
    calls = sum(counts) if all(valid_number(c) for c in counts) else None
    incomplete = any(r.get('answer_present') is False for r in records.values())
    truncated = any(r.get('possible_truncation') for r in records.values())
    if mock:
        evidence = 'Mock 模拟场景，不能证明真实节省、质量保持或任务完成。'
    elif errors or incomplete:
        evidence = '存在调用失败或空答案；资源下降不能认定为有效优化。'
    elif truncated:
        evidence = '存在可能截断的答案，需要人工检查完整性和质量。'
    else:
        evidence = '有可见回答仍不代表任务完成；资源差异需要结合人工完成与质量判断。'
    diagnostic_fields = ('api_returned_successfully', 'answer_present', 'finish_reason', 'possible_truncation',
                         'budget_exhausted', 'reasoning_tokens', 'reasoning_ratio', 'reasoning_usage_available',
                         'reasoning_usage_status', 'content_state', 'response_diagnostic', 'task_completion_status')
    return {'source': 'mock' if mock else 'live', 'decision': decision, 'resources': list(resources.values()),
            'comparison': resource_comparison(resources),
            'comparisons_by_mode': {m: resource_comparison(resources, m) for m in
                                    ('output_only', 'router_only', 'full_green') if m in records or m in errors},
            'answers': {m: r.get('answer') or '' for m, r in records.items()},
            'diagnostics': {m: {k: r.get(k) for k in diagnostic_fields} for m, r in records.items()},
            'errors': errors, 'actual_api_calls': calls, 'baseline_available': 'baseline' in records,
            'greenai_available': 'full_green' in records, 'effective_optimization': False if incomplete or errors else None,
            'evidence': evidence, 'caution': COST_CAUTION,
            'mock_pricing_timestamp': fixture_time.isoformat() if fixture_time else None,
            'quality_evaluation': 'Pending human evaluation',
            'gateway_pricing': {k: (config.get('gateway_pricing') or {}).get(k) for k in
                                ('version', 'signature', 'policy', 'currency', 'price_source', 'checked_at', 'missing_cache_usage')}}


def merge_baseline_report(green, baseline):
    """Keep the original Green response/cost/time; no new Green request or repricing."""
    result = copy.deepcopy(green)
    resources = {r['mode']: r for r in result['resources'] if r['mode'] != 'baseline'}
    resources.update({r['mode']: copy.deepcopy(r) for r in baseline['resources']})
    result['resources'] = [resources[m] for m in ('baseline', 'full_green') if m in resources]
    for field in ('answers', 'diagnostics', 'errors'):
        result[field].pop('baseline', None)
        result[field].update(copy.deepcopy(baseline[field]))
    result['comparison'] = resource_comparison(resources)
    result['comparisons_by_mode'] = {'full_green': result['comparison']}
    result['planned_modes'] = ['full_green', 'baseline']
    result['baseline_available'] = baseline['baseline_available']
    counts = (green['actual_api_calls'], baseline['actual_api_calls'])
    result['actual_api_calls'] = sum(counts) if all(valid_number(c) for c in counts) else None
    incomplete = any(r.get('answer_present') is False for r in result['diagnostics'].values())
    truncated = any(r.get('possible_truncation') for r in result['diagnostics'].values())
    result['effective_optimization'] = False if incomplete or result['errors'] else None
    result['evidence'] = ('存在调用失败或空答案；资源下降不能认定为有效优化。' if incomplete or result['errors'] else
                          '存在可能截断的答案，需要人工检查完整性和质量。' if truncated else
                          '有可见回答仍不代表任务完成；资源差异需要结合人工完成与质量判断。')
    return result
