"""practice 综合实战包（模块 11）

职责：
- benchmark.py   : 测试集批量跑 aftersale-agent，采集指标 + 评估（模块 10 的观测闭环）
- orchestrator.py: 端到端编排器（多会话 + 人工审批模拟 + 质检 + 观测打点 + 报告）
- run_demo.py    : 一键综合实战入口

衔接关系：
  practice（模块 11）────调用──▶ cs_supervisor（多 Agent 图）
        │                              │
        ├─ 每会话结束调用 ──▶ monitoring.evaluator（规则+LLM 评估）
        ├─ 全程打点 ──────▶ monitoring.metrics（指标采集）
        └─ 汇总输出 ──────▶ monitoring.reporter（JSON/MD/HTML 报告）
"""
