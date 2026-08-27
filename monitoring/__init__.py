"""monitoring 观测评估包（模块 10）

职责：
- metrics.py    : 指标采集（计数器、耗时分布、p50/p95/p99、快照）
- logger.py     : 结构化 JSON 日志（文件 + 控制台双输出）
- evaluator.py  : 会话质量评估（规则质检 + LLM 相关性/忠实度评分，无 key 回退规则版）
- reporter.py   : 报告生成（JSON / Markdown / HTML 三格式）

与项目衔接：
- evaluator 复用 quality_inspector.py 的规则质检
- 被 practice 包（模块 11）调用，实现"综合实战"的观测闭环
"""
