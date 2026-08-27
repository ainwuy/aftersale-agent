# -*- coding: utf-8 -*-
"""
orchestrator.py —— 端到端编排器（模块 11 核心组件 2）

职责：把模块 1~11 学到的能力串成一个"可演示的综合实战"：
  1. 多 Agent 图           （cs_supervisor：分类→检索→成稿→人工审核）
  2. 业务工具调用          （tools：查订单/查物流/退货/退款）
  3. 向量检索              （lookup_faq / ChromaDB）
  4. 人工介入 + 断点续跑   （interrupt + checkpointer）
  5. 会话质检 + 知识库补全 （quality_inspector）
  6. 观测评估              （monitoring：metrics/evaluator/reporter）

编排流程：
  模拟 3 个真实用户会话（含 approve 与 reject 分支）→ 全链路打点 →
  综合评估 → 生成一份"综合实战报告"（HTML 可浏览器打开）。
"""
import time
import uuid
from typing import Optional

from monitoring.logger import setup_logger, event_logger


class PracticeOrchestrator:
    """综合实战编排器。

    用法：
        orch = PracticeOrchestrator(llm=None)          # 桩 LLM，无需 key
        orch.run_simulated_sessions()                   # 跑 3 个模拟会话
        orch.generate_report("reports/practice.html")   # 出报告
    """

    def __init__(self, llm=None):
        self.llm = llm
        self.log = setup_logger("orchestrator", log_dir="logs")
        self.ev = event_logger(self.log)
        from monitoring.metrics import MetricsCollector
        from monitoring.evaluator import SessionEvaluator
        from monitoring.reporter import ReportGenerator
        self.metrics = MetricsCollector()
        self.evaluator = SessionEvaluator(llm=self.llm)
        self.reporter = ReportGenerator("aftersale-agent 综合实战报告（模块 11）")
        self._session_logs = []

    # ---------- 桩 LLM（无 key 可跑）----------
    def _stub_llm(self):
        """与 benchmark 相同的桩逻辑（抽到这里避免重复导入）。"""
        from practice.benchmark import BenchmarkRunner
        return BenchmarkRunner(llm=None)._make_stub_llm()

    # ---------- 模拟会话 ----------
    def _run_session(self, question: str, mode: str = "approve") -> dict:
        """跑一个会话。mode: approve（直接批准）/ reject_then_approve（先驳回再批准）。"""
        import cs_supervisor as cs
        from langgraph.types import Command

        if self.llm is None:
            cs.llm = self._stub_llm()
        else:
            cs.llm = self.llm

        sid = f"orch-{uuid.uuid4().hex[:8]}"
        thread = cs.thread_cfg(sid)
        self.ev("session_start", session_id=sid, question=question, mode=mode)
        t0 = time.time()

        # 首轮：跑到 human_approve 暂停
        result = cs.app.invoke(cs.new_session_input(question), thread)
        state = cs.app.get_state(thread).values
        draft = state.get("draft", "")
        self.ev("draft_generated", session_id=sid, draft_len=len(draft))

        # 人工审批环节（HITL 演示）
        if mode == "reject_then_approve":
            self.ev("human_reject", session_id=sid, feedback="语气请更亲切一些")
            result = cs.app.invoke(
                Command(resume={"action": "reject", "feedback": "语气请更亲切一些"}), thread)
            # 主管重新路由 → 重写成稿 → 再次 interrupt
            result = cs.app.invoke(
                Command(resume={"action": "approve", "feedback": ""}), thread)

        self.ev("human_approve", session_id=sid)
        result = cs.app.invoke(
            Command(resume={"action": "approve", "feedback": ""}), thread)
        elapsed = time.time() - t0

        state = cs.app.get_state(thread).values if cs.app.get_state(thread) else {}
        session_data = {
            "session_id": sid,
            "messages": state.get("messages", []),
            "category": state.get("category", ""),
            "context": state.get("context", ""),
            "draft": state.get("draft", ""),
            "status": "completed",
            "review_count": state.get("review_count", 0),
            "created_at": __import__("datetime").datetime.now().isoformat(),
            "completed_at": __import__("datetime").datetime.now().isoformat(),
        }
        # 打点 + 评估 + 知识库补全（衔接模块 3 质检/补全）
        self.metrics.counter("requests")
        self.metrics.timing("latency", elapsed)
        eval_result = self.evaluator.evaluate(session_data)

        knowledge_updated = False
        try:
            from quality_inspector import quality_and_update
            kq = quality_and_update(session_data)
            knowledge_updated = kq.get("knowledge_updated", False)
        except Exception as e:
            self.ev("knowledge_update_error", session_id=sid, error=str(e)[:100])

        entry = {
            "session_id": sid, "question": question, "mode": mode,
            "elapsed": round(elapsed, 3), "eval": eval_result,
            "knowledge_updated": knowledge_updated,
        }
        self._session_logs.append(entry)
        self.ev("session_end", session_id=sid, elapsed=round(elapsed, 3),
                score=eval_result["score"], knowledge_updated=knowledge_updated)
        return entry

    # ---------- 一键跑模拟 ----------
    def run_simulated_sessions(self) -> list:
        """跑 3 个代表性会话：FAQ 咨询 / 订单查询 / 投诉（含驳回再批准）。"""
        cases = [
            ("我买的鞋子尺码不合适想退货，怎么操作？", "approve"),
            ("我订单 CS20260820001 是什么状态？", "approve"),
            ("客服一直不回复，我要投诉！", "reject_then_approve"),
        ]
        print("[orchestrator] 开始综合实战模拟（3 个会话）...")
        for q, mode in cases:
            entry = self._run_session(q, mode)
            print(f"  {'✅' if entry['eval']['score'] >= 60 else '⚠️'} "
                  f"{q[:20]}… 耗时 {entry['elapsed']}s | "
                  f"分 {entry['eval']['score']} | 补全={entry['knowledge_updated']}")
        return self._session_logs

    # ---------- 报告 ----------
    def generate_report(self, path: str = "reports/practice.html") -> str:
        """生成综合报告（默认 HTML，浏览器可直接打开）。"""
        evals = [e["eval"] for e in self._session_logs]
        summary = self.evaluator.aggregate(evals)
        summary["sessions_detail"] = self._session_logs
        summary["avg_latency"] = (
            round(sum(e["elapsed"] for e in self._session_logs) / len(self._session_logs), 3)
            if self._session_logs else None)
        report = self.reporter.build(self.metrics.snapshot(), summary)
        self.reporter.save(report, path, "html")
        self.reporter.save(report, path.replace(".html", ".json"), "json")
        print(f"[orchestrator] 综合实战报告已生成 → {path}")
        return path


# 命令行直接跑：python -m practice.orchestrator
if __name__ == "__main__":
    orch = PracticeOrchestrator(llm=None)   # 桩 LLM，无需 API key
    orch.run_simulated_sessions()
    orch.generate_report("reports/practice.html")
