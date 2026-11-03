import random, time, uuid
from datetime import datetime, timezone
from .classifier import classify
from .router import route
from .output import output_control
from .provider import ChatProvider
from .evaluator import ManualEvaluator, RUBRIC
from .storage import Store
from .pricing import calculate_cost
from .diagnostics import diagnose_record, completion_evidence, summary_by_mode, COST_CAUTION

def estimate_cost(usage, model, cfg):
    """Legacy helper; production uses a timestamp captured before each request."""
    return calculate_cost(usage, model, cfg, datetime.now(timezone.utc))["estimated_api_cost"]

class Engine:
    def __init__(self, config, provider=None, cleaner=None, evaluator=None):
        self.config = config
        self.provider = provider or ChatProvider(config)
        # cleaner kept as a constructor argument for compatibility; never used in v0.2.
        self.evaluator = evaluator or ManualEvaluator()
        self.store = Store(config["database"])

    def plan(self, prompt, context="", task_type=None):
        if not prompt.strip():
            raise ValueError("Prompt 不能为空")
        classification = classify(prompt, context, task_type)
        budget, instruction = output_control(prompt, classification, self.config)
        return {**classification, "selected_model": route(classification, self.config), "requested_output_budget": budget, "instruction": instruction}

    def run(self, mode, prompt, context="", task_type=None, experiment_id=None, order=None):
        mode = "full_green" if mode == "green" else mode
        if mode not in MODES:
            raise ValueError("Unknown mode")
        started = time.perf_counter()
        plan = self.plan(prompt, context, task_type)
        use_router = mode in ("router_only", "full_green")
        use_output = mode in ("output_only", "full_green")
        model = plan["selected_model"] if use_router else self.config["baseline_model"]
        budget = plan["requested_output_budget"] if use_output else self.config["baseline_output_budget"]
        system_instruction = plan["instruction"] if use_output else None
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        user = prompt + ("\n\n参考上下文：\n" + context if context else "")
        messages.append({"role": "user", "content": user})
        run_id = str(uuid.uuid4())
        experiment_id = experiment_id or str(uuid.uuid4())
        request_time = datetime.now(timezone.utc)
        try:
            response = self.provider.complete(model, messages, budget)
        except Exception as exc:
            self.store.save_failure({"run_id": run_id, "experiment_id": experiment_id, "mode": mode,
                "actual_messages": messages, "system_instruction": system_instruction,
                "requested_output_budget": budget, "difficulty": plan["level"],
                "request_timestamp": request_time.isoformat(),
                "created_at": datetime.now(timezone.utc).isoformat(), "original_prompt": prompt,
                "original_context": context, "selected_model": model,
                "api_calls": getattr(exc, "api_calls", None), "error": str(exc),
                "latency_seconds": time.perf_counter()-started,
                "usage": None, "cost": None, "call_order": order})
            raise
        usage = response["usage"]
        record = {"run_id": run_id, "experiment_id": experiment_id or str(uuid.uuid4()), "task_id": experiment_id or run_id,
                  "created_at": datetime.now(timezone.utc).isoformat(), "mode": mode, "call_order": order,
                  "task_type": plan["task_type"], "original_prompt": prompt, "original_context": context,
                  "optimized_context": context, "context": context, "sent_messages": messages, "actual_messages": messages,
                  "system_instruction": system_instruction, "difficulty": plan["level"], "classification": plan,
                  "schema_version": 2, "router_enabled": use_router, "output_control_enabled": use_output,
                  "selected_model": model, "requested_output_budget": budget, "input_tokens": usage.get("prompt_tokens"),
                  "output_tokens": usage.get("completion_tokens"), "actual_output_tokens": usage.get("completion_tokens"),
                  "total_tokens": usage.get("total_tokens"), "raw_usage": usage,
                  "estimated_api_cost": None, "currency": self.config.get("currency"),
                  "context_cleaner": "passthrough-v1", "context_reduction_ratio": 0.0,
                  "original_input_tokens": None, "optimized_input_tokens": usage.get("prompt_tokens"),
                  "quality": self.evaluator.evaluate(response["answer"], RUBRIC),
                  "config_snapshot": {k:v for k,v in self.config.items() if k not in ("database", "api_key_env")},
                  **{k:v for k,v in response.items() if k != "usage"}}
        record.update(calculate_cost(usage, model, self.config, request_time))
        record["pricing_schema_version"] = 1
        record["latency_seconds"] = time.perf_counter() - started
        record["possible_truncation"] = record.get("finish_reason") == "length"
        record["quality_score"] = record["quality"]["score"]
        record["timestamp"] = record["created_at"]
        record["latency"] = record["latency_seconds"]
        record["cost"] = record["estimated_api_cost"]
        record.setdefault('api_returned_successfully', True)
        record.update(diagnose_record(record))
        self.store.save(record)
        return record

    def compare(self, prompt, context="", task_type=None):
        # Legacy API keeps result key 'green'; stored mode is now full_green.
        return self._compare(["baseline", "green"], prompt, context, task_type)

    def ablation_compare(self, prompt, context="", task_type=None):
        return self._compare(list(MODES), prompt, context, task_type)

    def _compare(self, modes, prompt, context, task_type):
        self.plan(prompt, context, task_type)  # validate before any API requests
        eid = str(uuid.uuid4())
        random.SystemRandom().shuffle(modes)
        records, errors = {}, {}
        for order, mode in enumerate(modes, 1):
            try:
                records[mode] = self.run(mode, prompt, context, task_type, eid, order)
            except Exception as exc:
                errors[mode] = str(exc)
        failures = [f for f in self.store.failures() if f.get('experiment_id') == eid]
        return {"experiment_id": eid, "planned_modes": modes, "runs": records,
                "errors": errors, "failures": failures, 'diagnostic_summary': summary_by_mode([*records.values(), *failures]),
                "comparison": comparison(records), "contrasts": contrasts(records)}

MODES = ("baseline", "output_only", "router_only", "full_green")
CROSS_MODEL_WARNING = "Different models may use different tokenization and reasoning processes. Token counts across models should not be interpreted as directly equivalent computational work."

def pair_comparison(b, g):
    if not b or not g:
        return {"status": "incomplete", "cross_model_comparison": None,
                'completion_evidence': 'missing_run', 'effective_optimization': None, 'cost_caution': COST_CAUTION}
    def reduction(a, z):
        return None if a is None or z is None or a == 0 else (a-z)/a
    cross = b["selected_model"] != g["selected_model"]
    # Returned provider IDs may expose different versions behind the same alias.
    if b.get("response_model") and g.get("response_model"):
        cross = cross or b["response_model"] != g["response_model"]
    evidence = completion_evidence(b, g)
    return {"status": "complete", "cross_model_comparison": cross,
            'reference_task_completion': b.get('task_completion_status', diagnose_record(b)['task_completion_status']),
            'comparison_task_completion': g.get('task_completion_status', diagnose_record(g)['task_completion_status']),
            'completion_evidence': evidence,
            'effective_optimization': False if evidence == 'blocked_incomplete_task' else None,
            'cost_caution': COST_CAUTION,
            "token_reduction_ratio": reduction(b.get("total_tokens"), g.get("total_tokens")),
            "latency_reduction_ratio": reduction(b.get("latency_seconds"), g.get("latency_seconds")),
            "cost_reduction_ratio": reduction(b.get("estimated_api_cost"), g.get("estimated_api_cost")) if b.get("currency") and b.get("currency") == g.get("currency") else None,
            "quality_difference": None if b.get("quality", {}).get("score") is None or g.get("quality", {}).get("score") is None else g["quality"]["score"]-b["quality"]["score"],
            "original_input_tokens": b.get("input_tokens"), "optimized_input_tokens": g.get("input_tokens"),
            "note": CROSS_MODEL_WARNING if cross else "同模型用量可比较；Token 差异仍不等于能耗差异。输出指令也会增加输入用量。"}

def comparison(records):
    return pair_comparison(records.get("baseline"), records.get("full_green") or records.get("green"))

def contrasts(records):
    full = records.get("full_green") or records.get("green")
    return {"baseline_vs_output_only": pair_comparison(records.get("baseline"), records.get("output_only")),
            "baseline_vs_router_only": pair_comparison(records.get("baseline"), records.get("router_only")),
            "baseline_vs_full_green": pair_comparison(records.get("baseline"), full),
            "router_only_vs_full_green": pair_comparison(records.get("router_only"), full),
            "output_only_vs_full_green": pair_comparison(records.get("output_only"), full)}
