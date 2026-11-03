"""Versioned human assessment payloads stored in the existing note field."""
import json
import math

COMPLETION_LABELS = ('Completed', 'Partially Completed', 'Failed', 'Not Applicable')
ISSUES = ('Missing key information', 'Incorrect information', 'Terminology issue',
          'Unnatural expression', 'Format violation', 'Empty answer', 'Truncation',
          'Budget exhausted', 'Other')
BASELINE_COMPARISONS = ('Better', 'Similar', 'Worse', 'Cannot compare')
OPTIMIZATION_EFFECTS = ('Effective optimization', 'Quality degradation', 'No clear benefit', 'Unable to judge')
SCORED_STATUSES = ('Completed', 'Partially Completed')
KIND = 'greentoken_human_evaluation'


def valid_score(value):
    return isinstance(value, (int,float)) and not isinstance(value,bool) and math.isfinite(value) and 1 <= value <= 5


def assessment(completion, score=None, issues=(), comparison=None, effect=None, notes=''):
    if completion not in COMPLETION_LABELS:
        raise ValueError('请选择必填的 Completion Status')
    if completion in SCORED_STATUSES:
        if not valid_score(score):
            raise ValueError('Completed / Partially Completed 必须填写1–5分的 Quality Score')
    elif score is not None:
        raise ValueError('Failed / Not Applicable 的 Quality Score 必须为 Not Applicable')
    if not isinstance(issues,(list,tuple)) or any(i not in ISSUES for i in issues):
        raise ValueError('Invalid observed issues')
    if comparison is not None and comparison not in BASELINE_COMPARISONS:
        raise ValueError('Invalid baseline comparison')
    if effect is not None and effect not in OPTIMIZATION_EFFECTS:
        raise ValueError('Invalid optimization effect')
    if not isinstance(notes,str):
        raise ValueError('Notes must be text')
    return {'kind':KIND,'schema_version':1,'completion_status':completion,
            'quality_score':score,'quality_applicable':completion in SCORED_STATUSES,
            'observed_issues':list(dict.fromkeys(issues)), 'baseline_comparison':comparison,
            'optimization_effect':effect,'notes':notes}


def parse_assessment(note):
    """Unrecognized/ordinary JSON remains a legacy note, never a fabricated label."""
    if not isinstance(note,str):
        return None
    try:
        value=json.loads(note)
        if not isinstance(value,dict) or value.get('kind')!=KIND or value.get('schema_version')!=1:
            return None
        return assessment(value.get('completion_status'), value.get('quality_score'),
                          value.get('observed_issues',[]), value.get('baseline_comparison'),
                          value.get('optimization_effect'),value.get('notes',''))
    except (ValueError,TypeError):
        return None


def encode_assessment(value):
    clean=assessment(value.get('completion_status'),value.get('quality_score'),value.get('observed_issues',[]),
                     value.get('baseline_comparison'),value.get('optimization_effect'),value.get('notes',''))
    return json.dumps(clean,ensure_ascii=False,sort_keys=True)


def legacy_status(completion,current):
    if completion=='Completed':
        return 'completed'
    if completion in ('Partially Completed','Failed'):
        return 'incomplete'
    # N/A makes no new success/failure judgment in the old field.
    return current
