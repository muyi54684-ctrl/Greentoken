"""Wrap the unchanged Engine with ephemeral storage for product requests."""
import copy
import hashlib
import json
import os
import random
import time
import uuid

from ..engine import Engine, MODES
from ..evaluator import ManualEvaluator
from ..provider import ChatProvider
from .mock import MockProvider
from .report import build_report, merge_baseline_report
from .pricing import load_gateway_pricing, apply_gateway_pricing, select_gateway_pricing


def request_fingerprint(prompt, context):
    return hashlib.sha256(json.dumps([prompt, context], ensure_ascii=False).encode('utf-8')).hexdigest()


class MemoryStore:
    def __init__(self):
        self.records = []
        self.failed = []

    def save(self, record):
        self.records.append(copy.deepcopy(record))

    def save_failure(self, record):
        # No provider exception bodies or credentials retained in the product layer.
        self.failed.append({key: record.get(key) for key in ('run_id', 'experiment_id', 'mode', 'api_calls')})

    def failures(self):
        return copy.deepcopy(self.failed)


class ProductEngine(Engine):
    """Only dependency wiring differs; Engine.plan/run are inherited unchanged."""
    def __init__(self, config, provider):
        self.config = copy.deepcopy(config)
        self.provider = provider
        self.evaluator = ManualEvaluator()
        self.store = MemoryStore()


class GatewayService:
    def __init__(self, config, live_provider_factory=None, *, gateway_pricing=None):
        self.config = copy.deepcopy(config)
        self.live_provider_factory = live_provider_factory or ChatProvider
        self.pricing_profile = load_gateway_pricing() if gateway_pricing is None else select_gateway_pricing(gateway_pricing)
        self.product_config = apply_gateway_pricing(self.config, self.pricing_profile)

    def live_available(self):
        key = os.getenv(self.config['api_key_env'], '')
        return bool(key.strip() and key != 'replace_me')

    def plan(self, prompt, context=''):
        # Planning does not construct ChatProvider/SQLite or issue a request.
        plan = ProductEngine(self.config, provider=None).plan(prompt, context)
        return {'task_type': plan['task_type'], 'level': plan['level'], 'reason': plan['reason'],
                'baseline': {'model': self.config['baseline_model'], 'budget': self.config['baseline_output_budget']},
                'greenai': {'model': plan['selected_model'], 'budget': plan['requested_output_budget']}}

    def execute(self, prompt, context='', mode='mock', compare_baseline=None, *, api_key=None, on_progress=None):
        if mode not in ('mock', 'live'):
            raise ValueError('Unknown Gateway mode')
        mock = mode == 'mock'
        if compare_baseline is None:
            compare_baseline = mock
        if not isinstance(compare_baseline, bool):
            raise ValueError('compare_baseline must be boolean')
        modes = ['baseline', 'full_green'] if compare_baseline else ['full_green']
        return self._execute_modes(prompt, context, mock, modes, api_key, on_progress)

    def execute_ablation(self, prompt, context='', mode='mock', *, api_key=None, on_progress=None):
        """Run the existing four strategies once each, without database writes."""
        if mode not in ('mock', 'live'):
            raise ValueError('Unknown Gateway mode')
        modes = list(MODES)
        random.SystemRandom().shuffle(modes)
        return self._execute_modes(prompt, context, mode == 'mock', modes, api_key, on_progress)

    def _execute_modes(self, prompt, context, mock, modes, api_key, on_progress=None):
        decision = self.plan(prompt, context)
        if not mock:
            if api_key is not None:
                if not isinstance(api_key, str) or not api_key.strip() or api_key.strip() == 'replace_me':
                    raise ValueError('请输入有效的 API Key；没有发送请求。')
                api_key = api_key.strip()
            elif not self.live_available():
                raise ValueError('未配置可用 API Key；请在页面输入 Key 或使用 Mock 模式。')
        provider = None
        try:
            if mock:
                provider = MockProvider(decision)
            elif api_key is not None:
                provider = self.live_provider_factory(self.config, api_key=api_key)
            else:
                # Preserve the v0.1 service interface for environment-based callers.
                provider = self.live_provider_factory(self.config)
            engine = ProductEngine(self.product_config, provider)
            records, errors = {}, {}
            experiment_id = str(uuid.uuid4())
            started = time.monotonic()
            def notify(name, order, state):
                if on_progress is None:
                    return
                routed = name in ('router_only', 'full_green')
                controlled = name in ('output_only', 'full_green')
                event = {'mode': name, 'order': order, 'total': len(modes), 'state': state,
                         'model': decision['greenai' if routed else 'baseline']['model'],
                         'budget': decision['greenai' if controlled else 'baseline']['budget'],
                         'finished': len(records) + len(errors), 'failed': len(errors),
                         'elapsed_seconds': time.monotonic() - started}
                try:
                    on_progress(event)
                except Exception:
                    # A display failure must not repeat or cancel a model request.
                    pass
            for order, name in enumerate(modes, 1):
                notify(name, order, 'running')
                try:
                    records[name] = engine.run(name, prompt, context, experiment_id=experiment_id, order=order)
                except Exception:
                    errors[name] = '本次调用未成功；没有自动重试或切换成 Mock。请核对 Key、模型、网络及 Provider 账单；真实请求是否计费未知。'
                notify(name, order, 'failed' if name in errors else 'returned')
            report = build_report(decision, records, errors, self.product_config, mock, engine.store.failures())
            report['request_fingerprint'] = request_fingerprint(prompt, context)
            report['experiment_id'] = experiment_id
            report['planned_modes'] = list(modes)
            return report
        finally:
            if provider is not None and hasattr(provider, 'clear_api_key'):
                provider.clear_api_key()
            api_key = None

    def add_baseline(self, green_report, prompt, context='', *, api_key=None):
        if green_report.get('source') != 'live' or not green_report.get('greenai_available'):
            raise ValueError('请先完成一次 Real API GreenAI 调用。')
        if green_report.get('request_fingerprint') != request_fingerprint(prompt, context):
            raise ValueError('输入已改变；不能与上次回答直接比较，请重新运行 GreenAI。')
        if green_report.get('decision') != self.plan(prompt, context):
            raise ValueError('模型或预算配置已改变；请重新运行 GreenAI。')
        previous = next((r for r in green_report['resources'] if r['mode'] == 'full_green'), {})
        if previous.get('pricing_signature') != self.pricing_profile['signature']:
            raise ValueError('Gateway 价格版本或参考单价已改变；请重新运行 GreenAI，不重算旧报告。')
        baseline = self._execute_modes(prompt, context, False, ['baseline'], api_key)
        return merge_baseline_report(green_report, baseline)
