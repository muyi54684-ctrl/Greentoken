"""Pure response diagnostics. Never store reasoning text or estimate missing usage."""

DIAGNOSTIC_FIELDS = (
    'diagnostics_schema_version', 'api_returned_successfully', 'content_state',
    'answer_present', 'answer_chars', 'finish_reason', 'reasoning_tokens',
    'reasoning_ratio', 'reasoning_usage_available', 'reasoning_usage_status',
    'reasoning_content_present', 'budget_exhausted', 'response_diagnostic',
    'needs_manual_evaluation', 'task_completion_status', 'completion_source',
)
COST_CAUTION = '成本下降不等于有效优化，必须结合任务完成率和质量判断。'


def valid_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def text_state(container, key):
    if key not in container:
        return 'missing'
    value = container[key]
    if value is None:
        return 'null'
    if not isinstance(value, str):
        return 'invalid_type'
    if value == '':
        return 'empty'
    return 'text' if value.strip() else 'whitespace'


def diagnose_response(message, usage, budget, finish_reason, api_returned_successfully=True):
    message = message if isinstance(message, dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    state = text_state(message, 'content')
    content = message.get('content')
    present = None if state == 'invalid_type' else state == 'text'
    chars = len(content) if isinstance(content, str) else None
    details = usage.get('completion_tokens_details')
    details = details if isinstance(details, dict) else {}
    nested_present = 'reasoning_tokens' in details
    top_present = 'reasoning_tokens' in usage
    reasoning = details.get('reasoning_tokens') if nested_present else usage.get('reasoning_tokens')
    completion = usage.get('completion_tokens')
    reasoning_status = 'missing'
    if nested_present or top_present:
        reasoning_status = 'null' if reasoning is None else 'valid' if valid_count(reasoning) else 'invalid'
        if nested_present and top_present and reasoning != usage['reasoning_tokens']:
            reasoning_status = 'conflicting'
        if reasoning_status == 'valid' and valid_count(completion) and reasoning > completion:
            reasoning_status = 'exceeds_completion'
    available = reasoning_status == 'valid'
    reasoning = reasoning if available else None
    ratio = reasoning / completion if available and valid_count(completion) and completion > 0 else None
    exhausted = completion >= budget if valid_count(completion) and valid_count(budget) and budget > 0 else None
    reasoning_state = text_state(message, 'reasoning_content')
    reasoning_present = None if reasoning_state == 'invalid_type' else reasoning_state == 'text'
    if api_returned_successfully is False:
        diagnostic = 'api_error'
        present, chars = False, None
    elif present is None:
        diagnostic = 'invalid_content_type'
    elif not present:
        if exhausted and available and reasoning == completion:
            diagnostic = 'empty_answer_reasoning_budget_exhausted'
        elif exhausted:
            diagnostic = 'empty_answer_budget_exhausted'
        else:
            diagnostic = 'empty_answer_stop' if finish_reason == 'stop' else 'empty_answer'
    elif finish_reason == 'length':
        diagnostic = 'partial_answer_length'
    elif finish_reason in ('content_filter', 'tool_calls', 'insufficient_system_resource', 'aborted'):
        diagnostic = 'answer_requires_review_' + finish_reason
    else:
        diagnostic = 'visible_answer' if finish_reason == 'stop' else 'visible_answer_unknown_finish'
    status = 'incomplete' if present is False else 'pending'
    return dict(diagnostics_schema_version=1, api_returned_successfully=api_returned_successfully,
                content_state=state, answer_present=present, answer_chars=chars,
                finish_reason=finish_reason, reasoning_tokens=reasoning, reasoning_ratio=ratio,
                reasoning_usage_available=available, reasoning_usage_status=reasoning_status,
                reasoning_content_present=reasoning_present, budget_exhausted=exhausted,
                response_diagnostic=diagnostic, task_completion_status=status,
                completion_source='automatic_no_answer' if status == 'incomplete' else 'unassessed',
                needs_manual_evaluation=status == 'pending')


def diagnose_record(record, failure=False):
    """Derive from recorded evidence; historical raw response states stay unknown."""
    message = {'content': record['answer']} if 'answer' in record else {}
    api_success = False if failure else record.get('api_returned_successfully')
    if api_success is None and record.get('api_calls') == 1 and record.get('finish_reason') is not None:
        api_success = True
    result = diagnose_response(message, record.get('raw_usage'), record.get('requested_output_budget'),
                               record.get('finish_reason'), api_success)
    result['content_state'] = record.get('content_state', 'unknown')
    result['reasoning_content_present'] = record.get('reasoning_content_present')
    if 'answer' not in record and not failure:
        result.update(answer_present=None, answer_chars=None, response_diagnostic='unknown_content',
                      task_completion_status='pending', completion_source='unassessed', needs_manual_evaluation=True)
    if record.get('diagnostics_schema_version') == 1:
        result.update({k: record[k] for k in DIAGNOSTIC_FIELDS if k in record})
    return result


def completion_evidence(reference, comparison):
    records = [reference, comparison]
    states = [r.get('task_completion_status', diagnose_record(r)['task_completion_status']) for r in records]
    if any(s == 'incomplete' for s in states) or any(diagnose_record(r)['answer_present'] is False for r in records):
        return 'blocked_incomplete_task'
    if states != ['completed', 'completed']:
        return 'pending_evaluation'
    return 'both_tasks_completed'


def summarize(records):
    """Unknowns excluded from their own rate denominator; expose all denominators."""
    rows = [{**r, **diagnose_record(r, failure=r.get('response_diagnostic') == 'api_error'),
             'task_completion_status': r.get('task_completion_status', diagnose_record(r)['task_completion_status'])}
            for r in records]
    n = len(rows)
    completed = sum(r['task_completion_status'] == 'completed' for r in rows)
    incomplete = sum(r['task_completion_status'] == 'incomplete' for r in rows)
    pending = n - completed - incomplete
    answer_known = [r for r in rows if r['answer_present'] is not None]
    finish_known = [r for r in rows if r['finish_reason'] is not None]
    reasoning_known = [r for r in rows if r['reasoning_usage_available'] and
                       valid_count((r.get('raw_usage') or {}).get('completion_tokens'))]
    completion_tokens = sum(r['raw_usage']['completion_tokens'] for r in reasoning_known)
    evaluated = completed + incomplete
    return {'records': n, 'completed': completed, 'incomplete': incomplete, 'pending': pending,
            'empty_answer_rate': sum(not r['answer_present'] for r in answer_known)/len(answer_known) if answer_known else None,
            'answer_evidence_count': len(answer_known),
            'truncation_rate': sum(r['finish_reason'] == 'length' for r in finish_known)/len(finish_known) if finish_known else None,
            'finish_evidence_count': len(finish_known),
            'task_completion_rate': completed/n if n else None,
            'evaluated_task_completion_rate': completed/evaluated if evaluated else None,
            'completion_evaluation_coverage': evaluated/n if n else None,
            'manual_completion_evaluation_coverage': sum(r.get('completion_source') == 'manual' for r in records)/n if n else None,
            'reasoning_token_ratio': sum(r['reasoning_tokens'] for r in reasoning_known)/completion_tokens if completion_tokens else None,
            'reasoning_evidence_count': len(reasoning_known)}


def summary_by_mode(records):
    groups = {}
    for r in records:
        mode = 'full_green' if r.get('mode') == 'green' else r.get('mode', 'unknown')
        groups.setdefault(mode, []).append(r)
    return [{'mode': mode, **summarize(rows)} for mode, rows in groups.items()]
