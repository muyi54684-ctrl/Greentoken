from typing import Protocol

class Evaluator(Protocol):
    def evaluate(self, answer: str, rubric: str) -> dict: ...

class ManualEvaluator:
    def evaluate(self, answer, rubric):
        return {"method": "manual", "score": None, "rubric": rubric, "status": "pending"}

RUBRIC = "1–5 分：正确性、任务完成度、必要信息完整性、格式遵循；建议盲评。另记录是否截断。评分需检查 finish_reason 和答案完整性。"
