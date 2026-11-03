CATEGORIES = ["Translation", "Writing / Polishing", "Summarization", "Programming", "Concept Explanation", "Paper Analysis", "Information Extraction", "Complex Reasoning"]
RULES = [
    ("Paper Analysis", ("论文", "paper analysis", "methodology")),
    ("Programming", ("代码", "debug", "python", "编程", "traceback")),
    ("Complex Reasoning", ("推理", "证明", "多步骤", "prove", "reasoning")),
    ("Translation", ("翻译", "translate", "translation")),
    ("Writing / Polishing", ("润色", "改写", "polish", "rewrite", "写作")),
    ("Summarization", ("总结", "摘要", "summarize", "summary")),
    ("Information Extraction", ("提取", "抽取", "extract", "格式转换")),
]

def classify(prompt, context="", task_type=None):
    p = prompt.lower()
    if task_type and task_type not in CATEGORIES:
        raise ValueError("Unknown task type")
    category = task_type or next((name for name, keys in RULES if any(k in p for k in keys)), "Concept Explanation")
    # Complexity uses task wording and context length, regardless of category override.
    hard = ("debug", "traceback", "证明", "多步骤", "复杂", "推理", "prove", "reasoning", "科研", "eio-lca", "研究方法", "研究产业链")
    if category in ("Paper Analysis", "Complex Reasoning") or any(k in p for k in hard):
        level, reason = "HARD", "论文/推理类别或复杂任务关键词"
    elif len(prompt + context) > 6000:
        level, reason = "HARD", "输入超过 6000 字符，保守升级"
    elif category in ("Programming", "Summarization") or len(prompt + context) > 1200:
        level, reason = "MEDIUM", "编程/总结类别或输入超过 1200 字符"
    else:
        level, reason = "EASY", "短输入且未触发复杂任务规则"
    return {"task_type": category, "level": level, "reason": reason, "method": "rules-v1"}
