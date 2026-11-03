"""Showcase-only projections. No API calls, database writes or repricing."""
import copy
import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path

from .human_evaluation import assessment


def load_settings(root):
    root = Path(root)
    # Page credentials are explicit; never read .env into process-wide state.
    config = json.loads((root / 'showcase.config.json').read_text(encoding='utf-8'))
    pricing = json.loads((root / 'showcase.pricing.json').read_text(encoding='utf-8'))
    return config, pricing


def review_answer(report, mode, completion, score=None, notes=''):
    if report.get('source') != 'live':
        raise ValueError('模拟回答不接受真实质量评价。')
    if mode not in report.get('answers', {}) or mode in report.get('errors', {}):
        raise ValueError('该策略没有可评价的响应。')
    if completion in ('Completed', 'Partially Completed') and not report['answers'][mode].strip():
        raise ValueError('空答案不能判为完成或部分完成。')
    result = assessment(completion, score, notes=notes)
    resource = next(r for r in report['resources'] if r['mode'] == mode)
    return {**result, 'mode': mode, 'run_id': resource['run_id'],
            'saved_at': datetime.now(timezone.utc).isoformat()}


def report_export(report, reviews=(), request_input=None, include_task=False):
    """Export explicit response fields; no credentials or local settings."""
    result = copy.deepcopy(report)
    result['human_evaluation_history'] = copy.deepcopy(list(reviews))
    result['export_notice'] = 'Contains model answers and evaluation notes; review before sharing.'
    if include_task and request_input:
        result['task'] = {'prompt': request_input['prompt'], 'context': request_input['context']}
    return result


def safe_cell(value):
    # Neutralize spreadsheet formula interpretation for user-controlled text.
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


def resource_csv(report, reviews=()):
    latest = {r['run_id']: r for r in reviews}
    fields = ('source', 'mode', 'model', 'run_id', 'input_tokens', 'output_tokens',
              'total_tokens', 'cost', 'currency', 'latency_seconds', 'budget',
              'pricing_version', 'cost_estimation_method', 'completion_status',
              'quality_score', 'notes')
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for row in report['resources']:
        review = latest.get(row['run_id'], {})
        item = {k: row.get(k) for k in fields}
        item.update(source=report['source'], completion_status=review.get('completion_status'),
                    quality_score=review.get('quality_score'), notes=review.get('notes'))
        writer.writerow({k: safe_cell(v) for k, v in item.items()})
    return stream.getvalue()


def private_history_path(root):
    root = Path(root)
    settings = root / 'local.settings.json'
    if not settings.exists():
        return None
    value = json.loads(settings.read_text(encoding='utf-8')).get('private_history')
    if not isinstance(value, str) or not value:
        return None
    path = (root / value).resolve()
    return path if path.is_file() else None
