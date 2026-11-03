"""GreenToken showcase. Start with: python -m streamlit run showcase.py"""
import json
import sqlite3
from pathlib import Path

import streamlit as st

from greentoken.gateway.service import GatewayService
from greentoken.gateway.report import comparison_rows
from greentoken.showcase_history import ShowcaseHistory, experiment_key, strategy_modes, pending_count
from greentoken.showcase_support import (
    load_settings, private_history_path, report_export, resource_csv, review_answer,
)

ROOT = Path(__file__).resolve().parent
MODES = {'baseline': 'Baseline · 原始策略', 'output_only': 'Output Only · 输出控制',
         'router_only': 'Router Only · 模型路由', 'full_green': 'Full Green · 组合策略',
         'green': 'Full Green · 旧版'}
TASKS = {'Translation': '翻译', 'Writing / Polishing': '写作与润色', 'Summarization': '摘要',
         'Programming': '编程', 'Concept Explanation': '概念解释', 'Paper Analysis': '论文分析',
         'Information Extraction': '信息提取', 'Complex Reasoning': '复杂推理'}
EXAMPLES = {
    '短句翻译': '请翻译成英文，只返回译文：绿色技术有助于提高资源利用效率。',
    '概念解释': '请用一个简单例子解释什么是机会成本，约150字。',
    '科研推理': '请完整分析：使用投入产出模型研究产业链变化对碳排放的影响，需要哪些假设？有哪些局限？',
}

st.set_page_config(page_title='GreenToken · 把每次调用看清楚', page_icon='🌱', layout='wide')
st.markdown('''<style>
.stApp {background: #f7f8f4; color: #1b3026;}
[data-testid="stSidebar"] {background: #eaf0e7;}
.block-container {max-width: 1180px; padding-top: 2.5rem; padding-bottom: 4rem;}
h1,h2,h3 {color: #173e2b; letter-spacing: -.025em;}
[data-testid="stMetric"] {background: white; border: 1px solid #dce6d9;
 border-radius: 14px; padding: 16px;}
.gt-eyebrow {font-size: 12px; font-weight: 700; letter-spacing: .18em; color: #51725b; margin-bottom: 10px;}
.gt-lead {font-size: 18px; line-height: 1.8; color: #526557; max-width: 780px; margin-bottom: 24px;}
.gt-step {background: #fff; border: 1px solid #dce6d9; border-radius: 14px; padding: 20px; min-height: 118px;}
.gt-step b {display:block; color:#245238; margin-bottom:8px;}
.stButton > button[kind="primary"] {background:#24583c; border-color:#24583c;}
</style>''', unsafe_allow_html=True)


def clear_key():
    st.session_state.gt_key = ''


def choose_example(name):
    st.session_state.gt_prompt = EXAMPLES[name]
    st.session_state.gt_prompt_saved = EXAMPLES[name]


def remember_input():
    st.session_state.gt_prompt_saved = st.session_state.get('gt_prompt', '')
    st.session_state.gt_context_saved = st.session_state.get('gt_context', '')


def money(value, currency='USD'):
    return '未知' if value is None else f'{currency} {value:.8f}'.rstrip('0').rstrip('.')


def comparison_kind():
    if st.session_state.get('gt_operation', '完成任务') == '完成任务':
        return 'single'
    return 'ablation' if st.session_state.get('gt_experiment_scope') == '完整四组' else 'pair'


def render_answer(report, mode):
    st.markdown('#### ' + MODES[mode])
    if mode in report['errors']:
        st.error(report['errors'][mode])
        return
    answer = report['answers'].get(mode, '')
    if answer.strip():
        st.write(answer)
    else:
        st.warning('没有返回可见答案。此次请求仍可能产生费用。')
    row = next((r for r in report['resources'] if r['mode'] == mode), None)
    if row:
        st.caption(f"请求模型：{row['model']} · 返回模型：{row.get('response_model') or '未知'} · 输出预算：{row['budget']}")
    diagnosis = report['diagnostics'].get(mode, {})
    if diagnosis.get('reasoning_usage_available'):
        st.caption(f"推理用量：{diagnosis['reasoning_tokens']} Token（计入输出用量和输出预算）。")
    if diagnosis.get('response_diagnostic') == 'empty_answer_reasoning_budget_exhausted':
        st.warning('本次输出预算全部用于推理，未返回可见答案。请将它作为未完成的结果复盘，不能认定为有效节省。')
    elif diagnosis.get('possible_truncation'):
        st.warning('输出达到停止上限，可能未完成任务；请先检查答案，再判断资源收益。')


def render_review(report, reviews=None, prefix='gt_', archived=False):
    if reviews is None:
        reviews = st.session_state.setdefault('gt_reviews', [])
    if report['source'] != 'live':
        st.caption('模拟答案不参与人工质量评价。')
        return
    modes = [m for m in MODES if m in report['answers'] and m not in report['errors']]
    if not modes:
        return
    with st.expander('评价这次回答', expanded=False):
        st.caption('先判断完成情况，再评价质量。已存入「我的实验」的记录会同步保存新评价，并保留历次判断。')
        mode = st.selectbox('评价对象', modes, format_func=MODES.get, key=prefix + 'review_mode')
        rid = next(r['run_id'] for r in report['resources'] if r['mode'] == mode)
        labels = {'尚未判断': None, '已完成': 'Completed', '部分完成': 'Partially Completed',
                  '未完成': 'Failed', '不适用': 'Not Applicable'}
        label = st.selectbox('任务完成情况', list(labels), key=prefix + 'completion_' + rid)
        status = labels[label]
        score = None
        if status in ('Completed', 'Partially Completed'):
            score = st.number_input('回答质量（1–5分）', min_value=1.0, max_value=5.0,
                                    value=None, step=0.5, key=prefix + 'score_' + rid)
        else:
            st.caption('未完成或不适用的回答不新增质量分。')
        note = st.text_area('判断依据', key=prefix + 'note_' + rid)
        if st.button('保存评价' if archived else '保存到本次报告', key=prefix + 'save_review'):
            try:
                review = review_answer(report, mode, status, score, note)
                if archived or st.session_state.get('gt_archived_id') == experiment_key(report):
                    ShowcaseHistory(ROOT).append_review(experiment_key(report), review)
                reviews.append(review)
                st.success('评价已保存。')
                if archived:
                    st.rerun()
            except ValueError as exc:
                st.error(str(exc))
            except (OSError, sqlite3.Error):
                st.error('本机保存失败，请重试保存评价；这不会重新调用模型。')
        if reviews:
            st.dataframe(reviews, hide_index=True, width='stretch')


def render_result():
    report = st.session_state.get('gt_report')
    if not report:
        st.info('输入任务后开始一次真实调用。你也可以先到「实验复盘」查看已有证据。')
        return
    saved = st.session_state.gt_request
    if st.session_state.get('gt_archived_id') == experiment_key(report):
        try:
            entry = next((e for e in ShowcaseHistory(ROOT).list() if e['id'] == experiment_key(report)), None)
            if entry:
                st.session_state.gt_reviews = entry['reviews']
        except (OSError, sqlite3.Error, ValueError):
            st.warning('暂时无法同步本机评价，下面保留当前会话已有结果。')
    current = (st.session_state.get('gt_prompt', ''), st.session_state.get('gt_context', ''),
               st.session_state.get('gt_mode'), comparison_kind())
    saved_kind = saved.get('comparison_kind', 'pair' if saved.get('compare') else 'single')
    if current != (saved['prompt'], saved['context'], saved['mode_label'], saved_kind):
        st.info('下方是上次运行结果；当前输入或选项已改变，点击运行后才会更新。')
    st.divider()
    st.subheader('02 · 回答与资源')
    if report['source'] == 'mock':
        st.warning('离线模拟：回答与用量均为固定示例，不代表真实任务的结果或节省。')
    else:
        st.caption('真实调用结果 · ' + str(report['actual_api_calls']) + ' 次 API 请求')
    with st.expander('本次任务'):
        st.text(saved['prompt'])
        if saved['context']:
            st.text(saved['context'])
    render_report_details(report)
    render_review(report)
    st.subheader('03 · 保留这次结果')
    if st.session_state.get('gt_archived_id') == experiment_key(report):
        st.success('已保存到「实验复盘 → 我的实验」，重开页面后仍可查看。')
    else:
        st.caption('这次结果尚未保存到本机，关闭会话前请保存或下载。')
        if st.button('保存到我的实验', key='gt_archive'):
            save_current_experiment()
            st.rerun()
    if st.session_state.get('gt_archive_error'):
        st.warning(st.session_state.gt_archive_error)
    render_downloads(report, st.session_state.get('gt_reviews', []), saved, 'gt_')


def save_current_experiment():
    try:
        st.session_state.gt_archived_id = ShowcaseHistory(ROOT).save(
            st.session_state.gt_report, st.session_state.gt_request, st.session_state.get('gt_reviews', []))
        st.session_state.pop('gt_archive_error', None)
    except (OSError, sqlite3.Error):
        st.session_state.gt_archive_error = '自动保存未成功，回答仍在当前页面。请重试「保存到我的实验」或下载报告；无需重新运行任务。'


def render_report_details(report):
    modes = [m for m in MODES if m in report['answers'] or m in report['errors']]
    for start in range(0, len(modes), 2):
        chunk = modes[start:start + 2]
        for col, name in zip(st.columns(len(chunk)), chunk):
            with col:
                render_answer(report, name)
    resources = sorted(report['resources'], key=lambda r: list(MODES).index(r['mode']))
    rows = [{'策略': MODES[r['mode']], '模型': r['model'], '输出预算': r['budget'],
             '输入 Token': r['input_tokens'], '输出 Token': r['output_tokens'],
             '总 Token': r['total_tokens'], '参考费用': money(r['cost'], r['currency']),
             '延迟（秒）': round(r['latency_seconds'], 3) if r['latency_seconds'] is not None else None}
            for r in resources]
    if rows:
        st.dataframe(rows, hide_index=True, width='stretch')
    if 'baseline' in modes and len(modes) > 1:
        st.markdown('#### 各策略相对 Baseline 的变化')
        comparisons = []
        for name in modes:
            if name == 'baseline':
                continue
            for row in comparison_rows(report, name):
                comparisons.append({'策略': MODES[name], '指标': row['Metric'], 'Baseline': row['Baseline'],
                                    '当前策略': row['GreenAI'], '变化': row['Change']})
        st.dataframe(comparisons, hide_index=True, width='stretch')
        st.caption('负数表示资源下降，正数表示增加；Unknown 表示无法比较。单次差异需要结合完成情况与质量判断。')
    else:
        st.caption('没有完整的 Baseline 对照，不报告节省比例。')
    st.caption('费用按固定峰时参考价估计，未计峰闲折扣，不是账单。Token 包含推理消耗；延迟不是首 Token 时间。')
    st.info(report['evidence'])
    with st.expander('查看用量依据与价格版本'):
        st.json({'prices': report['gateway_pricing'], 'resources': report['resources'],
                 'diagnostics': report['diagnostics'], 'execution_order': report.get('planned_modes')})


def render_downloads(report, reviews, saved, prefix):
    st.caption('JSON 包含回答和评价备注，分享前请检查；CSV 仅导出指标和评价。')
    include = st.checkbox('JSON 同时包含任务原文与参考上下文', value=False, key=prefix + 'export_task')
    left, right = st.columns(2)
    left.download_button('下载完整报告 JSON', json.dumps(report_export(report, reviews, saved, include), ensure_ascii=False, indent=2),
                         'greentoken-report.json', 'application/json', key=prefix + 'json')
    right.download_button('下载指标与评价 CSV', ('\ufeff' + resource_csv(report, reviews)).encode('utf-8'),
                          'greentoken-metrics.csv', 'text/csv', key=prefix + 'csv')


def render_live():
    st.markdown('<div class="gt-eyebrow">01 / TRY GREENTOKEN</div>', unsafe_allow_html=True)
    st.title('完成任务，也看清每一次消耗。')
    st.markdown('<div class="gt-lead">让任务决定模型与输出预算，再用真实回答检验这次选择。先用一次，再决定是否需要对照。</div>', unsafe_allow_html=True)
    config, pricing = load_settings(ROOT)
    service = GatewayService(config, gateway_pricing=pricing)
    if st.session_state.pop('gt_clear_key', False):
        clear_key()
    st.session_state.setdefault('gt_prompt', st.session_state.get('gt_prompt_saved', ''))
    st.session_state.setdefault('gt_context', st.session_state.get('gt_context_saved', ''))
    st.session_state.setdefault('gt_reviews', [])
    st.subheader('01 · 输入任务')
    left, right = st.columns([2.1, 1], gap='large')
    with left:
        for col, name in zip(st.columns(3), EXAMPLES):
            col.button(name, on_click=choose_example, args=(name,), use_container_width=True)
        prompt = st.text_area('你希望完成什么？', key='gt_prompt', height=165,
                              placeholder='例如：翻译一段文字，或解释一个概念。', on_change=remember_input)
        with st.expander('添加参考上下文（可选）'):
            context = st.text_area('参考内容会完整发送给模型', key='gt_context', height=120, on_change=remember_input)
    with right:
        with st.container(border=True):
            mode_label = st.radio('运行方式', ['真实 API', '离线模拟'], key='gt_mode', on_change=clear_key)
            live = mode_label == '真实 API'
            if live:
                st.text_input('本次调用的 API Key', type='password', key='gt_key')
                st.caption('Key 只用于本次提交，提交后清空。请在可信本机使用。')
                st.caption('请求发送至：' + config['base_url'])
            else:
                st.caption('固定示例用于熟悉流程，不调用模型，也不产生费用。')
            st.radio('这次想做什么？', ['完成任务', '对照实验'], key='gt_operation', horizontal=True)
            if st.session_state.gt_operation == '对照实验':
                st.radio('对照范围', ['两组对照', '完整四组'], key='gt_experiment_scope', horizontal=True)
            kind = comparison_kind()
            request_count = {'single': 1, 'pair': 2, 'ablation': 4}[kind]
            st.caption({'single': 'Full Green：使用模型路由与输出控制。',
                        'pair': 'Baseline + Full Green：观察组合策略的整体效果。',
                        'ablation': 'Baseline / Output Only / Router Only / Full Green：分别观察输出控制、模型路由与组合效果。'}[kind])
            if kind == 'ablation':
                st.caption('四组使用同一任务和上下文，随机顺序逐组运行；输出控制包含指令和预算。')
            st.caption(f'将发送 {request_count} 次请求，均可能计费；每组运行一次，无自动重试。' if live
                       else f'将展示 {request_count} 组模拟结果，0 次 API 请求。')
            auto_save = st.checkbox('自动保存到我的实验', value=True, key='gt_auto_save')
            st.caption('保存任务原文、上下文、回答、指标及评价到本机，供以后复盘；不保存 API Key，不随分享包发送。')
            submit = st.button('运行真实任务' if live else '运行离线示例', type='primary', key='gt_run', use_container_width=True)
    if prompt.strip():
        plan = service.plan(prompt, context)
        st.caption(f"识别为{TASKS.get(plan['task_type'], plan['task_type'])} · {plan['level']} · {plan['greenai']['model']} · 输出上限 {plan['greenai']['budget']} Token")
        with st.expander('为什么这样选择？'):
            st.write(plan['reason'])
            st.write('路由由关键词和输入长度规则决定。输出上限也包含可能的推理消耗；较小预算可能导致空答案。')
            st.json(plan)
    if submit:
        remember_input()
        progress_bar = st.progress(0, text='准备请求…')
        current_step = st.empty()
        st.caption('等待时间为本次操作累计用时；进度按已返回或失败的策略数计算，不是模型生成百分比。')
        st.caption(f"网络等待超时配置为 {config['timeout_seconds']} 秒，不是整个任务的总时限。当前需要收到完整响应后才显示回答；页面无法判断服务端正在排队还是生成。")
        def show_progress(event):
            progress_bar.progress(event['finished'] / event['total'],
                                  text=f"已结束 {event['finished']}/{event['total']} 组 · 失败 {event['failed']} 组")
            label = MODES[event['mode']]
            if event['state'] == 'running':
                current_step.info(f"第 {event['order']}/{event['total']} 组：{label} · 模型 {event['model']} · 输出上限 {event['budget']} Token。正在等待完整响应…")
            elif event['state'] == 'failed':
                current_step.warning(f"{label} 未成功返回，已记录失败；没有自动重试。")
            else:
                current_step.info(f"{label} 已返回响应；还需检查答案是否完整。")
        try:
            with st.spinner('正在等待模型回答…' if live else '正在加载固定示例…', show_time=True):
                options = {'mode': 'live' if live else 'mock',
                           'on_progress': show_progress,
                           'api_key': st.session_state.get('gt_key', '') if live else None}
                if kind == 'ablation':
                    report = service.execute_ablation(prompt, context, **options)
                else:
                    report = service.execute(prompt, context, compare_baseline=kind == 'pair', **options)
            st.session_state.gt_report = report
            st.session_state.gt_request = {'prompt': prompt, 'context': context, 'mode_label': mode_label, 'comparison_kind': kind}
            st.session_state.gt_reviews = []
            st.session_state.pop('gt_archived_id', None)
            st.session_state.pop('gt_archive_error', None)
            if auto_save:
                save_current_experiment()
            st.session_state.pop('gt_error', None)
        except ValueError as exc:
            st.session_state.gt_error = str(exc)
        finally:
            st.session_state.gt_clear_key = True
        st.rerun()
    if st.session_state.get('gt_error'):
        st.error(st.session_state.gt_error)
    render_result()


def render_history():
    st.markdown('<div class="gt-eyebrow">02 / EVIDENCE & REFLECTION</div>', unsafe_allow_html=True)
    st.title('实验复盘')
    st.markdown('<div class="gt-lead">回看每次任务的回答、资源消耗与人工评价，让新实验成为下一次判断的依据。</div>', unsafe_allow_html=True)
    section = st.radio('查看内容', ['我的实验', '示例案例'], horizontal=True, key='gt_history_section')
    if section == '示例案例':
        render_examples()
        return
    st.caption('这里只展示本机已保存的新实验。真实调用与离线模拟分开统计，示例案例不会混入。')
    st.button('刷新实验列表', key='gt_refresh_history')
    try:
        entries = ShowcaseHistory(ROOT).list()
    except (OSError, sqlite3.Error, ValueError):
        st.error('暂时无法读取本机实验记录。请检查文件是否可访问；已有记录未被覆盖。')
        return
    source = st.radio('数据来源', ['真实 API', '离线模拟'], horizontal=True, key='gt_history_source')
    left, right = st.columns(2)
    kind = left.selectbox('实验范围', ['全部', '单组任务', '两组对照', '完整四组'], key='gt_history_kind')
    query = right.text_input('搜索任务内容', key='gt_history_query').strip().lower()
    pending_only = st.checkbox('仅看待评价实验', key='gt_history_pending', disabled=source != '真实 API')
    def matches(entry):
        report = entry['report']
        return (report['source'] == ('live' if source == '真实 API' else 'mock')
                and (kind == '全部' or len(strategy_modes(report)) == {'单组任务': 1, '两组对照': 2, '完整四组': 4}[kind])
                and query in entry['request']['prompt'].lower()
                and (source != '真实 API' or not pending_only or pending_count(entry) > 0))
    selected = [e for e in entries if matches(e)]
    complete_four = sum(set(('baseline', 'output_only', 'router_only', 'full_green')).issubset(e['report']['answers']) for e in selected)
    empty = sum(not a.strip() for e in selected for a in e['report']['answers'].values())
    failures = sum(len(e['report']['errors']) for e in selected)
    for col, label, value in zip(st.columns(4), ['实验次数', '四组均返回', '空答案', '调用失败'],
                                 [len(selected), complete_four, empty, failures]):
        col.metric(label, value)
    st.caption('统计随当前筛选更新。四组均返回只说明收到了响应，不代表四组均完成任务；空答案和调用失败分别计数。')
    if not selected:
        st.info('暂无符合条件的记录。运行任务时勾选「自动保存到我的实验」，结果就会出现在这里；也可以调整筛选。')
        return
    by_id = {e['id']: e for e in selected}
    def label(key):
        e = by_id[key]
        return e['created_at'][:19].replace('T', ' ') + ' UTC · ' + e['request']['prompt'].replace('\n', ' ')[:45] + ' · ' + key[:8]
    key = st.selectbox('选择一次实验（最新在前）', list(by_id), format_func=label, key='gt_history_selected')
    entry = by_id[key]
    report = entry['report']
    st.caption(f"本次 {len(strategy_modes(report))} 组策略 · API 请求 {report['actual_api_calls'] if report['actual_api_calls'] is not None else '未知'} 次")
    st.text(entry['request']['prompt'])
    if entry['request']['context']:
        with st.expander('本次参考上下文'):
            st.text(entry['request']['context'])
    if report['source'] == 'mock':
        st.warning('离线模拟数据，仅用于熟悉流程，不能作为真实效果证据。')
    else:
        st.caption(f'待评价响应：{pending_count(entry)}。可在下方继续评价，保存后更新本机记录。')
    render_report_details(report)
    prefix = 'history_' + key + '_'
    render_review(report, entry['reviews'], prefix, archived=True)
    render_downloads(report, entry['reviews'], entry['request'], prefix)


def render_examples():
    st.caption('以下是随项目提供的脱敏示例，数据日期固定；你新增的实验在「我的实验」中持续更新。')
    data = json.loads((ROOT / 'data/public_cases.json').read_text(encoding='utf-8'))
    overview = data['overview']
    for col, label, key in zip(st.columns(4), ['历史运行', '不同原始输入', '完整四组实验', '空答案'],
                               ['runs', 'unique_inputs', 'complete_ablation_groups', 'empty_answers']):
        col.metric(label, overview[key])
    st.caption('数据日期：' + data['recorded_date'] + ' · 31条运行不是31个独立任务；费用为当时记录的估计值。')
    case = st.selectbox('选择一个复盘问题', data['cases'], format_func=lambda c: c['title'], key='gt_case')
    st.subheader(case['title'])
    st.write(case['description'])
    rows = []
    rank = {'baseline': 0, 'output_only': 1, 'router_only': 2, 'full_green': 3}
    for r in sorted(case['runs'], key=lambda r: rank[r['mode']]):
        rows.append({'策略': MODES[r['mode']], '模型': r['selected_model'], '预算': r['requested_output_budget'],
                     '总 Token': r['total_tokens'], '历史费用': money(r['estimated_api_cost'], r['currency']),
                     '延迟（秒）': round(r['latency_seconds'], 3), '可见答案': '有' if r['answer_present'] else '无',
                     '人工完成判定': r['completion_status'], '历史分数': r['historical_quality_score']})
    st.dataframe(rows, hide_index=True, width='stretch')
    lessons = {
        'translation': 'Full Green 比 Baseline 使用更多 Token，但费用更低。跨模型的单价与缓存情况不同，Token 不能替代费用指标。',
        'summary': 'Output Only 与 Full Green 都没有可见答案。费用下降不能抵消任务未完成；优先检查输出预算和推理用量。',
        'variability': 'Baseline 与 Router Only 的模型和预算相同，这一次却有不同结果。不能把这种差异当成路由贡献，仍需重复实验。',
    }
    st.info(lessons[case['id']])
    st.caption('历史分数不覆盖后续完成判定。旧分数、Similar 与综合效果标签可能存在不一致，保留待复核；不自动修正为有利结论。')
    with st.expander('四组策略分别改变了什么？'):
        st.table([{'策略': MODES[m], '模型路由': '开启' if m in ('router_only', 'full_green') else '关闭',
                   '输出控制': '开启' if m in ('output_only', 'full_green') else '关闭'} for m in rank])
        st.caption('输出控制同时改变指令和预算。Baseline 也有安全输出上限；上下文始终完整保留。')
    path = private_history_path(ROOT)
    if path:
        with st.expander('本机完整历史（私有，只读）'):
            st.caption('来自独立的本机历史副本，不随分享包分发。这里只查看，不修改旧评价。')
            from greentoken.storage import Store
            from greentoken.evaluation_analysis import performance
            runs = Store(str(path), read_only=True).runs()
            ids = list(dict.fromkeys(r['experiment_id'] for r in runs))
            eid = st.selectbox('实验组', ids, key='gt_private_group')
            group = [r for r in runs if r['experiment_id'] == eid]
            st.text(group[0].get('original_prompt', ''))
            if group[0].get('original_context'):
                st.text(group[0]['original_context'])
            for r in sorted(group, key=lambda r: rank.get(r['mode'], 4)):
                st.markdown('#### ' + MODES.get(r['mode'], r['mode']))
                st.text(r.get('answer') or '没有可见答案')
                completion, score = performance(r)
                st.caption(f'最新人工评价：{completion} · 适用评分：{score if score is not None else "不适用 / 未评分"}')
    st.caption(data['notice'])
    st.download_button('下载脱敏案例指标', json.dumps(data, ensure_ascii=False, indent=2), 'greentoken-public-cases.json', 'application/json')


def render_guide():
    st.markdown('<div class="gt-eyebrow">03 / USER GUIDE</div>', unsafe_allow_html=True)
    st.title('使用指南')
    st.markdown('<div class="gt-lead">从完成一个任务开始，了解如何选择对照方式、阅读结果，并持续积累自己的实验记录。</div>', unsafe_allow_html=True)
    steps = [('1 · 输入任务', '写清目标和要求；需要时添加参考材料，材料会完整发送给模型。'),
             ('2 · 选择运行方式', '真实 API 使用自己的 Key 并可能计费；离线模拟可先熟悉操作。'),
             ('3 · 阅读并评价', '先检查回答是否完成任务，再评质量，最后看资源变化。'),
             ('4 · 保存与复盘', '默认保存到本机「我的实验」，以后可继续评价或下载报告。')]
    for col, (title, body) in zip(st.columns(4), steps):
        col.markdown(f'<div class="gt-step"><b>{title}</b>{body}</div>', unsafe_allow_html=True)
    st.subheader('选择适合你的操作')
    st.table([{'操作': '完成任务', '适用场景': '直接获取一次回答，使用 Full Green', '真实请求数': 1},
              {'操作': '对照实验 → 两组对照', '适用场景': '比较 Baseline 与组合策略的整体表现', '真实请求数': 2},
              {'操作': '对照实验 → 完整四组', '适用场景': '分别观察输出控制、模型路由和组合策略', '真实请求数': 4}])
    st.caption('每次点击运行都会重新执行所选策略，均可能计费。切换页面、筛选、评分、刷新列表和下载报告不会调用模型。失败不会自动重试。')
    with st.expander('四种策略有什么区别？'):
        st.table([{'策略': 'Baseline', '模型路由': '关闭', '输出控制': '关闭'},
                  {'策略': 'Output Only', '模型路由': '关闭', '输出控制': '开启'},
                  {'策略': 'Router Only', '模型路由': '开启', '输出控制': '关闭'},
                  {'策略': 'Full Green', '模型路由': '开启', '输出控制': '开启'}])
        st.write('模型路由根据任务规则选择模型；输出控制同时调整指令和输出预算。Baseline 也有输出上限。四组使用同一任务与完整上下文，并按随机顺序运行。')
    st.subheader('读懂结果，而不只看节省比例')
    st.table([{'指标': 'Token', '如何理解': '输入与输出的用量；输出可能包含推理消耗，跨模型不等于相同计算量。'},
              {'指标': '参考费用', '如何理解': '按报告记录的固定参考价估算，不等于实际账单；未知不会按零处理。'},
              {'指标': '延迟', '如何理解': '一次调用等待的总时间，不是首字出现的时间；网络和服务状态也会影响。'},
              {'指标': '相对 Baseline 的变化', '如何理解': '负数表示资源下降，正数表示增加；还需要核对完成情况和质量。'}])
    st.subheader('区分数据来源')
    st.table([{'来源': '真实 API', '能说明什么': '本次请求的回答与返回用量；需人工核验完成情况'},
              {'来源': '我的实验', '能说明什么': '本机保存的任务与评价；真实调用和模拟记录分开显示'},
              {'来源': '示例案例', '能说明什么': '随项目提供的历史指标，帮助理解收益与失败，不保证新任务效果'},
              {'来源': '离线模拟', '能说明什么': '只说明界面与流程，不能证明节省或回答质量'}])
    st.subheader('如何评价与持续复盘')
    st.write('在「评价这次回答」中先选择完成情况。已完成或部分完成需要填写1–5分及判断依据；未完成或不适用不新增质量分。空答案不能标记完成，模拟回答不参与真实质量评分。')
    st.write('运行前默认勾选「自动保存到我的实验」。结果会出现在「实验复盘 → 我的实验」，支持按来源、实验范围、任务内容和待评价状态筛选。可以继续评价，历次判断会保留；重复保存同一次结果不会新增实验。')
    st.subheader('数据保存与分享')
    st.write('本机记录包含任务原文、上下文、回答、指标和评价，刷新或重开页面后仍可查看。取消自动保存后只保留在当前会话，也可事后点击「保存到我的实验」。API Key 在提交后清空，不写入实验记录。')
    st.write('JSON 默认不带任务原文，但包含回答和评价备注；CSV 包含指标和评价。分享报告前请检查敏感内容。发布的脱敏项目包不包含你的本机实验记录；同一台电脑上使用该项目的人可能查看本机记录。')
    with st.expander('遇到空答案或调用失败怎么办？'):
        st.write('先检查任务是否完整、Key 和模型是否可用。空答案可能是输出预算被推理消耗，仍可能产生费用。失败组会保留提示，其他组继续执行；查看服务商账单后再决定是否重新运行。')
    with st.expander('结果有哪些使用边界？'):
        st.write('规则分类和路由并不保证每次选择最优。单次结果有波动，同分不代表质量等效，四组对照也不是四个独立任务。建议在多种任务上重复验证。当前不测量能耗或碳排放，费用下降不能直接解释为减碳。')


with st.sidebar:
    st.markdown('## 🌱 GreenToken')
    st.caption('生成式 AI 调用优化与评测')
    if st.session_state.get('gt_page') == '项目是怎么工作的':
        st.session_state.gt_page = '使用指南'
    page = st.radio('导航', ['开始任务', '实验复盘', '使用指南'], key='gt_page', on_change=clear_key)
    st.divider()
    st.caption('真实调用优先 · 质量与资源一起看')
    st.caption('新实验默认保存到本机，可在实验复盘中查看与继续评价。')
    st.markdown('[模型与参考价格来源](https://api-docs.deepseek.com/quick_start/pricing/)')
    st.caption('参考价格核对：2026-11-03')

{'开始任务': render_live, '实验复盘': render_history, '使用指南': render_guide}[page]()
