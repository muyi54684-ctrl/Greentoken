import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from greentoken.gateway.service import GatewayService
from greentoken.showcase_support import load_settings

ROOT = Path(__file__).resolve().parents[1]


def reply(request, **kwargs):
    return io.BytesIO(json.dumps({'choices': [{'message': {'content': 'offline answer'}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30}}).encode())


class WaitProgressTests(unittest.TestCase):
    def service(self):
        config, prices = load_settings(ROOT)
        return GatewayService(config, gateway_pricing=prices)

    def test_four_group_progress_matches_requests_and_failures(self):
        events = []
        calls = 0
        def response(request, **kwargs):
            nonlocal calls
            calls += 1
            # The UI receives the current strategy before the network request.
            self.assertEqual(events[-1]['state'], 'running')
            self.assertEqual(events[-1]['order'], calls)
            self.assertEqual(events[-1]['finished'], calls - 1)
            body = json.loads(request.data)
            self.assertEqual(events[-1]['model'], body['model'])
            self.assertEqual(events[-1]['budget'], body['max_tokens'])
            if calls == 2:
                raise HTTPError('offline', 500, 'fixture', {}, None)
            return reply(request, **kwargs)
        with patch('greentoken.provider.urllib.request.urlopen', side_effect=response):
            report = self.service().execute_ablation('请翻译：你好', mode='live',
                api_key='offline-progress-secret', on_progress=events.append)
        self.assertEqual(calls, 4)
        self.assertEqual(len(events), 8)
        self.assertEqual([e['mode'] for e in events[::2]], report['planned_modes'])
        self.assertEqual([e['finished'] for e in events[1::2]], [1, 2, 3, 4])
        self.assertEqual([e['state'] for e in events[1::2]], ['returned', 'failed', 'returned', 'returned'])
        self.assertEqual(events[-1]['failed'], 1)
        self.assertTrue(all(e['elapsed_seconds'] >= 0 for e in events))
        self.assertNotIn('offline-progress-secret', json.dumps(events))
        self.assertNotIn('请翻译', json.dumps(events, ensure_ascii=False))

    def test_progress_display_exception_never_retries_or_cancels_requests(self):
        def broken_display(event):
            raise RuntimeError('display closed')
        with patch('greentoken.provider.urllib.request.urlopen', side_effect=reply) as http:
            report = self.service().execute('翻译：你好', mode='live', compare_baseline=True,
                api_key='offline-key', on_progress=broken_display)
        self.assertEqual(http.call_count, 2)
        self.assertFalse(report['errors'])

    def test_invalid_key_does_not_claim_request_started(self):
        events = []
        with patch('greentoken.provider.urllib.request.urlopen') as http:
            with self.assertRaises(ValueError):
                self.service().execute('翻译：你好', mode='live', api_key='', on_progress=events.append)
        self.assertEqual(events, [])
        http.assert_not_called()
