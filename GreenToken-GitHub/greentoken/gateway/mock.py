"""Explicitly synthetic classroom fixtures, not an offline language model."""
from ..diagnostics import diagnose_response

ANSWERS = {
    'Translation': '绿色技术有助于提高资源利用效率。',
    'Summarization': '示例摘要：绿色数字技术通过任务分类、模型路由和输出控制改善资源使用；效果需结合任务完成和答案质量验证。',
    'Paper Analysis': '示例分析框架：核对研究问题、方法与假设、实验设计、主要结果及局限。这里没有阅读或分析实际论文，不能作为论文结论。',
    'Programming': '示例代码：\ndef add(a, b):\n    return a + b\n\n这段固定示例没有实现或验证你的实际编程需求。',
    'Writing / Polishing': '示例润色：本文探讨绿色数字技术在提高人工智能资源利用效率方面的应用。',
    'Information Extraction': '示例提取：任务分类、模型路由、输出预算、资源报告。',
    'Concept Explanation': '示例解释：Gateway 在用户与模型 API 之间选择模型和输出预算，并报告 Token、费用与延迟。',
    'Complex Reasoning': '示例推理框架：明确假设，拆解问题，逐步验证，检查边界条件。这里没有解决实际推理问题。',
}

# (baseline input/output, Green input/output, baseline/Green latency).
# These values are fixed illustrations, not predictions from a user's prompt.
FIXTURES = {
    'EASY': (120, 880, 160, 140, 3.0, 1.1),
    'MEDIUM': (500, 1600, 540, 480, 8.0, 3.5),
    'HARD': (1200, 1000, 1240, 1000, 9.0, 9.0),
}


class MockProvider:
    def __init__(self, decision):
        self.decision = decision

    def complete(self, model, messages, budget):
        optimized = bool(messages and messages[0].get('role') == 'system')
        bi, bo, gi, go, bl, gl = FIXTURES[self.decision['level']]
        input_tokens, desired, latency = (gi, go, gl) if optimized else (bi, bo, bl)
        output_tokens = min(desired, budget)
        finish = 'length' if desired > budget else 'stop'
        answer = 'Mock 固定示例（不是真实模型回答）：\n' + ANSWERS[self.decision['task_type']]
        if finish == 'length':
            answer = 'Mock 部分输出：此场景的模拟用量达到了输出预算，需要检查完整性。'
        usage = {'prompt_tokens': input_tokens, 'completion_tokens': output_tokens,
                 'total_tokens': input_tokens + output_tokens,
                 'prompt_cache_hit_tokens': 0, 'prompt_cache_miss_tokens': input_tokens,
                 'completion_tokens_details': {'reasoning_tokens': 0}}
        return {'answer': answer, 'usage': usage, 'finish_reason': finish,
                'api_calls': 0, 'api_latency_seconds': None, 'response_model': model,
                'mock_latency_seconds': latency,
                **diagnose_response({'content': answer}, usage, budget, finish,
                                    api_returned_successfully=None)}
