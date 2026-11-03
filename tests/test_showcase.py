import io
import csv
import json
import unittest
import tempfile
import sqlite3
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from streamlit.testing.v1 import AppTest
from greentoken.gateway.service import GatewayService
from greentoken.showcase_support import load_settings, report_export, resource_csv, review_answer
from greentoken.showcase_history import ShowcaseHistory, pending_count

ROOT = Path(__file__).resolve().parents[1]


def reply(request, **kwargs):
    body = json.loads(request.data)
    return io.BytesIO(json.dumps({'model': body['model'], 'id': 'offline-showcase',
        'choices': [{'message': {'content': 'Green technology improves resource efficiency.'}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 60, 'total_tokens': 160,
                  'prompt_cache_hit_tokens': 0, 'prompt_cache_miss_tokens': 100,
                  'completion_tokens_details': {'reasoning_tokens': 20}}}).encode())


class ShowcaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.history_path = Path(self.temp.name) / 'local_experiments.sqlite'
        patcher = patch('greentoken.showcase_history.history_path', return_value=self.history_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def app(self):
        return AppTest.from_file(str(ROOT / 'showcase.py'), default_timeout=20).run()

    def test_real_default_open_navigation_and_mock_never_call_api(self):
        with patch('greentoken.provider.urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')) as http:
            app = self.app()
            self.assertFalse(app.exception)
            self.assertEqual(app.radio(key='gt_mode').value, '真实 API')
            app.radio(key='gt_page').set_value('实验复盘').run()
            self.assertFalse(app.exception)
            self.assertEqual([m.value for m in app.metric], ['0', '0', '0', '0'])
            app.radio(key='gt_history_section').set_value('示例案例').run()
            self.assertEqual([m.value for m in app.metric], ['31', '4', '7', '5'])
            app.radio(key='gt_page').set_value('使用指南').run()
            self.assertFalse(app.exception)
            app.radio(key='gt_page').set_value('开始任务').run()
            app.radio(key='gt_mode').set_value('离线模拟').run()
            app.text_area(key='gt_prompt').set_value('请翻译：你好')
            app.button(key='gt_run').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state['gt_report']['source'], 'mock')
            self.assertEqual(app.session_state['gt_report']['actual_api_calls'], 0)
            http.assert_not_called()

    def test_real_single_call_key_cleared_no_research_write_and_review_export(self):
        with patch('greentoken.engine.Store', side_effect=AssertionError('DB forbidden')), \
             patch('greentoken.provider.urllib.request.urlopen', side_effect=reply) as http:
            app = self.app()
            app.text_area(key='gt_prompt').set_value('翻译：绿色技术')
            app.text_input(key='gt_key').set_value('offline-showcase-credential')
            app.button(key='gt_run').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(http.call_count, 1)
            saved = ShowcaseHistory(ROOT).list()
            self.assertEqual(len(saved), 1)
            self.assertNotIn('offline-showcase-credential', json.dumps(saved))
            self.assertEqual(app.text_input(key='gt_key').value, '')
            report = app.session_state['gt_report']
            self.assertEqual(report['source'], 'live')
            self.assertNotIn('offline-showcase-credential', json.dumps(report))
            rid = report['resources'][0]['run_id']
            app.selectbox(key='gt_completion_' + rid).set_value('已完成').run()
            app.number_input(key='gt_score_' + rid).set_value(4.5)
            app.text_area(key='gt_note_' + rid).set_value('=unsafe_spreadsheet_formula')
            app.button(key='gt_save_review').click().run()
            self.assertFalse(app.exception)
            reviews = app.session_state['gt_reviews']
            self.assertEqual(reviews[-1]['quality_score'], 4.5)
            self.assertIn("'=unsafe_spreadsheet_formula", resource_csv(report, reviews))
            public = report_export(report, reviews, {'prompt': 'private task', 'context': 'private context'})
            self.assertNotIn('task', public)
            self.assertEqual(len(public['human_evaluation_history']), 1)
            self.assertNotIn('offline-showcase-credential', json.dumps(public))
            self.assertEqual(http.call_count, 1)
            self.assertEqual(len(ShowcaseHistory(ROOT).list()[0]['reviews']), 1)

    def test_explicit_comparison_exactly_two_calls_and_navigation_no_extra(self):
        with patch('greentoken.provider.urllib.request.urlopen', side_effect=reply) as http:
            app = self.app()
            app.text_area(key='gt_prompt').set_value('翻译：你好')
            app.text_input(key='gt_key').set_value('offline-key')
            app.radio(key='gt_operation').set_value('对照实验').run()
            self.assertEqual(app.radio(key='gt_experiment_scope').value, '两组对照')
            app.button(key='gt_run').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(http.call_count, 2)
            self.assertTrue(app.session_state['gt_report']['baseline_available'])
            app.radio(key='gt_page').set_value('实验复盘').run()
            app.radio(key='gt_page').set_value('开始任务').run()
            self.assertFalse(app.exception)
            self.assertEqual(http.call_count, 2)
            self.assertEqual(app.text_area(key='gt_prompt').value, '翻译：你好')

    def test_four_groups_preserve_factor_controls_and_export_reviews(self):
        with patch('greentoken.engine.Store', side_effect=AssertionError('DB forbidden')), \
             patch('greentoken.provider.urllib.request.urlopen', side_effect=reply) as http:
            app = self.app()
            app.radio(key='gt_operation').set_value('对照实验').run()
            app.radio(key='gt_experiment_scope').set_value('完整四组').run()
            self.assertTrue(any('将发送 4 次请求' in c.value for c in app.caption))
            http.assert_not_called()
            app.text_area(key='gt_prompt').set_value('请翻译：你好')
            app.text_area(key='gt_context').set_value('完整上下文，不应删减。')
            app.text_input(key='gt_key').set_value('offline-four-key')
            app.button(key='gt_run').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(http.call_count, 4)
            self.assertEqual(app.text_input(key='gt_key').value, '')
            report = app.session_state['gt_report']
            modes = ['baseline', 'output_only', 'router_only', 'full_green']
            self.assertCountEqual(report['planned_modes'], modes)
            self.assertEqual(report['actual_api_calls'], 4)
            bodies = {mode: json.loads(call.args[0].data) for mode, call in zip(report['planned_modes'], http.call_args_list)}
            self.assertEqual(bodies['baseline']['model'], bodies['output_only']['model'])
            self.assertEqual(bodies['router_only']['model'], bodies['full_green']['model'])
            self.assertNotEqual(bodies['baseline']['model'], bodies['router_only']['model'])
            self.assertEqual(bodies['baseline']['max_tokens'], bodies['router_only']['max_tokens'])
            self.assertEqual(bodies['output_only']['max_tokens'], bodies['full_green']['max_tokens'])
            self.assertNotEqual(bodies['baseline']['max_tokens'], bodies['output_only']['max_tokens'])
            user_messages = [b['messages'][-1] for b in bodies.values()]
            self.assertTrue(all(m == user_messages[0] for m in user_messages))
            self.assertIn('完整上下文，不应删减。', user_messages[0]['content'])
            for mode, body in bodies.items():
                self.assertEqual(any(m['role'] == 'system' for m in body['messages']), mode in ('output_only', 'full_green'))
            self.assertEqual(len(report['comparisons_by_mode']), 3)
            self.assertCountEqual(app.selectbox(key='gt_review_mode').options, [
                'Baseline · 原始策略', 'Output Only · 输出控制', 'Router Only · 模型路由', 'Full Green · 组合策略'])
            for mode in ('output_only', 'router_only'):
                app.selectbox(key='gt_review_mode').set_value(mode).run()
                rid = next(r['run_id'] for r in report['resources'] if r['mode'] == mode)
                app.selectbox(key='gt_completion_' + rid).set_value('已完成').run()
                app.number_input(key='gt_score_' + rid).set_value(4.0)
                app.text_area(key='gt_note_' + rid).set_value('夹具回答符合任务要求。')
                app.button(key='gt_save_review').click().run()
                self.assertFalse(app.exception)
            reviews = app.session_state['gt_reviews']
            rows = list(csv.DictReader(io.StringIO(resource_csv(report, reviews))))
            self.assertCountEqual([r['mode'] for r in rows], modes)
            self.assertEqual(len(report_export(report, reviews)['human_evaluation_history']), 2)
            self.assertNotIn('offline-four-key', json.dumps(report_export(report, reviews)))
            app.radio(key='gt_experiment_scope').set_value('两组对照').run()
            self.assertEqual(http.call_count, 4)
            self.assertTrue(any('上次运行结果' in i.value for i in app.info))

    def test_four_groups_partial_failure_retains_successes_and_unknown_comparison(self):
        count = 0
        def fail_second(request, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise HTTPError('offline', 500, 'fixture', {}, None)
            return reply(request, **kwargs)
        with patch('greentoken.gateway.service.random.SystemRandom.shuffle'), \
             patch('greentoken.provider.urllib.request.urlopen', side_effect=fail_second) as http:
            app = self.app()
            app.radio(key='gt_operation').set_value('对照实验').run()
            app.radio(key='gt_experiment_scope').set_value('完整四组').run()
            app.text_area(key='gt_prompt').set_value('请翻译：你好')
            app.text_input(key='gt_key').set_value('offline-key')
            app.button(key='gt_run').click().run()
            self.assertFalse(app.exception)
            report = app.session_state['gt_report']
            self.assertEqual(http.call_count, 4)
            self.assertEqual(report['actual_api_calls'], 4)
            self.assertEqual(list(report['errors']), ['output_only'])
            self.assertEqual(len(report['resources']), 3)
            self.assertFalse(report['effective_optimization'])
            self.assertTrue(all(r['direction'] == 'unknown' for r in report['comparisons_by_mode']['output_only']))
            self.assertEqual(len(list(csv.DictReader(io.StringIO(resource_csv(report, []))))), 3)
            self.assertIn('output_only', report_export(report, [])['errors'])
            self.assertTrue(any('本次调用未成功' in e.value for e in app.error))
            self.assertNotIn('output_only', app.selectbox(key='gt_review_mode').value)

    def test_mock_four_groups_and_switch_back_to_single(self):
        with patch('greentoken.provider.urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')) as http:
            app = self.app()
            app.radio(key='gt_mode').set_value('离线模拟').run()
            app.radio(key='gt_operation').set_value('对照实验').run()
            app.radio(key='gt_experiment_scope').set_value('完整四组').run()
            app.text_area(key='gt_prompt').set_value('翻译：你好')
            app.button(key='gt_run').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.session_state['gt_report']['answers']), 4)
            self.assertEqual(app.session_state['gt_report']['actual_api_calls'], 0)
            app.radio(key='gt_operation').set_value('完成任务').run()
            app.button(key='gt_run').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(list(app.session_state['gt_report']['answers']), ['full_green'])
            http.assert_not_called()

    def test_old_session_report_survives_new_controls(self):
        app = self.app()
        config, pricing = load_settings(ROOT)
        app.session_state['gt_report'] = GatewayService(config, gateway_pricing=pricing).execute('翻译：你好')
        app.session_state['gt_request'] = {'prompt': '翻译：你好', 'context': '', 'mode_label': '离线模拟', 'compare': True}
        app.run()
        self.assertFalse(app.exception)
        self.assertTrue(any('上次运行结果' in i.value for i in app.info))

    def test_missing_key_no_network_and_http_error_no_mock_fallback(self):
        with patch('greentoken.provider.urllib.request.urlopen', side_effect=HTTPError('offline', 401, 'fixture', {}, None)) as http:
            app = self.app()
            app.text_area(key='gt_prompt').set_value('翻译：你好')
            app.button(key='gt_run').click().run()
            self.assertFalse(app.exception)
            http.assert_not_called()
            app.text_input(key='gt_key').set_value('offline-key')
            app.button(key='gt_run').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(http.call_count, 1)
            self.assertEqual(app.text_input(key='gt_key').value, '')
            report = app.session_state['gt_report']
            self.assertEqual(report['source'], 'live')
            self.assertTrue(report['errors'])
            self.assertEqual(report['resources'], [])

    def test_empty_answer_cannot_receive_completed_review(self):
        config, pricing = load_settings(ROOT)
        with patch('greentoken.provider.urllib.request.urlopen', side_effect=reply):
            report = GatewayService(config, gateway_pricing=pricing).execute('翻译：你好', mode='live', api_key='offline-key')
        report['answers']['full_green'] = ''
        with self.assertRaises(ValueError):
            review_answer(report, 'full_green', 'Completed', 5)
        review = review_answer(report, 'full_green', 'Failed')
        self.assertIsNone(review['quality_score'])

    def test_shared_cases_contain_no_original_content_or_identifiers(self):
        data = json.loads((ROOT / 'data/public_cases.json').read_text(encoding='utf-8'))
        denied = {'original_prompt', 'prompt', 'original_context', 'context', 'answer', 'note',
                  'notes', 'run_id', 'experiment_id', 'config_snapshot', 'api_key'}
        def walk(value):
            if isinstance(value, dict):
                self.assertFalse(denied.intersection(value))
                for v in value.values(): walk(v)
            elif isinstance(value, list):
                for v in value: walk(v)
        walk(data)
        self.assertEqual(len(data['cases']), 3)
        self.assertEqual(sum(len(c['runs']) for c in data['cases']), 12)

    def test_history_survives_new_session_filters_and_review_sync(self):
        with patch('greentoken.provider.urllib.request.urlopen', side_effect=reply) as http:
            app = self.app()
            app.radio(key='gt_operation').set_value('对照实验').run()
            app.radio(key='gt_experiment_scope').set_value('完整四组').run()
            app.text_area(key='gt_prompt').set_value('翻译：新增实验可持续复盘')
            app.text_input(key='gt_key').set_value('offline-persist-key')
            app.button(key='gt_run').click().run()
            self.assertFalse(app.exception)
            entry = ShowcaseHistory(ROOT).list()[0]
            self.assertEqual(pending_count(entry), 4)
            fresh = self.app()
            fresh.radio(key='gt_page').set_value('实验复盘').run()
            self.assertFalse(fresh.exception)
            self.assertEqual([m.value for m in fresh.metric], ['1', '1', '0', '0'])
            fresh.text_input(key='gt_history_query').set_value('不存在的任务').run()
            self.assertEqual(fresh.metric[0].value, '0')
            fresh.text_input(key='gt_history_query').set_value('持续复盘').run()
            fresh.selectbox(key='gt_history_kind').set_value('两组对照').run()
            self.assertEqual(fresh.metric[0].value, '0')
            fresh.selectbox(key='gt_history_kind').set_value('完整四组').run()
            prefix = 'history_' + entry['id'] + '_'
            fresh.selectbox(key=prefix + 'review_mode').set_value('output_only').run()
            rid = next(r['run_id'] for r in entry['report']['resources'] if r['mode'] == 'output_only')
            fresh.selectbox(key=prefix + 'completion_' + rid).set_value('已完成').run()
            fresh.number_input(key=prefix + 'score_' + rid).set_value(4.0)
            fresh.text_area(key=prefix + 'note_' + rid).set_value('完整回答了任务要求。')
            fresh.button(key=prefix + 'save_review').click().run()
            self.assertFalse(fresh.exception)
            updated = ShowcaseHistory(ROOT).list()[0]
            self.assertEqual(pending_count(updated), 3)
            self.assertEqual(len(updated['reviews']), 1)
            app.run()
            self.assertEqual(len(app.session_state['gt_reviews']), 1)
            self.assertEqual(http.call_count, 4)

    def test_auto_save_opt_out_manual_save_idempotent_and_mock_separate(self):
        with patch('greentoken.provider.urllib.request.urlopen', side_effect=AssertionError('No network')) as http:
            app = self.app()
            app.radio(key='gt_mode').set_value('离线模拟').run()
            app.checkbox(key='gt_auto_save').uncheck()
            app.text_area(key='gt_prompt').set_value('翻译：保存测试')
            app.button(key='gt_run').click().run()
            self.assertFalse(self.history_path.exists())
            app.button(key='gt_archive').click().run()
            report, request = app.session_state['gt_report'], app.session_state['gt_request']
            ShowcaseHistory(ROOT).save(report, request)
            self.assertEqual(len(ShowcaseHistory(ROOT).list()), 1)
            app.radio(key='gt_page').set_value('实验复盘').run()
            self.assertEqual(app.metric[0].value, '0')
            app.radio(key='gt_history_source').set_value('离线模拟').run()
            self.assertEqual(app.metric[0].value, '1')
            self.assertFalse(app.exception)
            http.assert_not_called()

    def test_save_failure_keeps_result_and_retry_never_repeats_api(self):
        with patch('greentoken.provider.urllib.request.urlopen', side_effect=reply) as http:
            app = self.app()
            app.text_area(key='gt_prompt').set_value('翻译：保存失败后重试')
            app.text_input(key='gt_key').set_value('offline-key')
            with patch.object(ShowcaseHistory, 'save', side_effect=sqlite3.OperationalError('fixture')):
                app.button(key='gt_run').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.text_input(key='gt_key').value, '')
            self.assertTrue(app.session_state['gt_report']['answers'])
            self.assertTrue(any('自动保存未成功' in w.value for w in app.warning))
            app.button(key='gt_archive').click().run()
            self.assertEqual(http.call_count, 1)
            self.assertEqual(len(ShowcaseHistory(ROOT).list()), 1)

    def test_history_counts_failures_empty_answers_and_pending_filters(self):
        config, pricing = load_settings(ROOT)
        with patch('greentoken.provider.urllib.request.urlopen', side_effect=HTTPError('offline', 500, 'fixture', {}, None)):
            failure = GatewayService(config, gateway_pricing=pricing).execute_ablation('翻译：失败', mode='live', api_key='offline-key')
        ShowcaseHistory(ROOT).save(failure, {'prompt': '失败任务'})
        with patch('greentoken.provider.urllib.request.urlopen', side_effect=reply):
            report = GatewayService(config, gateway_pricing=pricing).execute('翻译：空答案', mode='live', api_key='offline-key')
        report['answers']['full_green'] = ''
        ShowcaseHistory(ROOT).save(report, {'prompt': '空答案任务'})
        app = self.app()
        app.radio(key='gt_page').set_value('实验复盘').run()
        self.assertEqual([m.value for m in app.metric], ['2', '0', '1', '4'])
        app.checkbox(key='gt_history_pending').check().run()
        self.assertEqual(app.metric[0].value, '1')
        key = report['experiment_id']
        prefix = 'history_' + key + '_'
        rid = report['resources'][0]['run_id']
        app.selectbox(key=prefix + 'completion_' + rid).set_value('未完成').run()
        app.button(key=prefix + 'save_review').click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.metric[0].value, '0')

    def test_guide_is_user_facing_and_legacy_navigation_migrates(self):
        app = AppTest.from_file(str(ROOT / 'showcase.py'), default_timeout=20)
        app.session_state['gt_page'] = '项目是怎么工作的'
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.radio(key='gt_page').value, '使用指南')
        self.assertEqual(app.title[0].value, '使用指南')
        content = '\n'.join(m.value for m in app.markdown)
        self.assertIn('数据保存与分享', '\n'.join(h.value for h in app.subheader))
        self.assertNotIn('个人贡献', content)
        self.assertFalse(self.history_path.exists())


if __name__ == '__main__':
    unittest.main()
