def output_control(prompt, classification, config):
    p = prompt.lower()
    detailed = any(x in p for x in ("详细", "完整", "全面", "detailed", "comprehensive", "full"))
    budget = config["budgets"]["detailed"] if detailed else config["budgets"][classification["level"]]
    if classification["task_type"] == "Translation":
        instruction = "只返回译文，保留原文信息与格式；用户明确要求的解释也应提供。"
    else:
        instruction = "直接完成用户任务，避免重复和无关铺垫；保留必要依据、步骤及限制。"
    instruction += " 用户明确提出的内容、长度和格式要求优先。"
    return budget, instruction
