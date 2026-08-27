# -*- coding: utf-8 -*-
"""
benchmark.py —— 测试集批量跑（模块 11 核心组件 1）

职责：用一组"问题 + 期望类别"测试集，逐条跑通 aftersale-agent 的多 Agent 图，
     对每条会话打点（耗时/成功/失败）+ 评估（规则质检），最后产出汇总。

关键设计：
- stub 开关：llm=None 时自动注入桩 LLM（无 API key 也能跑通全链路，验证流程）
- 人工审批模拟：图在 human_approve 处 interrupt，本组件自动 resume "approve"
  （demo 场景自动批准；真实场景可改为"随机驳回一次再批准"验证 reject 分支）
- 观测闭环：调 monitoring.metrics / monitoring.evaluator / monitoring.reporter
"""
import time
import uuid
from datetime import datetime, timezone
from typing import List, Optional

# ---------- 测试集（与客服 FAQ 主题对齐，可在真实场景扩展）----------
DEFAULT_CASES = [
    {"question": "我买的鞋子尺码不合适想退货，怎么操作？", "category": "退换货与售后"},
    {"question": "我订单 CS20260820001 是什么状态？", "category": "退换货与售后"},
    {"question": "客服一直不回复怎么办？可以投诉吗？", "category": "投诉与维权"},
    {"question": "物流单号 SF1029384756 到哪里了？", "category": "退换货与售后"},
    {"question": "你们的退款多久能到账？", "category": "退换货与售后"},
]


class BenchmarkRunner:
    """测试集批量执行器。

    用法：
        runner = BenchmarkRunner(cases=DEFAULT_CASES, llm=None, auto_approve=True)
        results = runner.run_all()          # 逐条跑图 + 打点
        runner.report(output_dir="reports") # 生成三格式报告
    """

    def __init__(self, cases: Optional[List[dict]] = None,
                 llm=None, auto_approve: bool = True):
        self.cases = cases or DEFAULT_CASES
        self.llm = llm            # None → 用桩 LLM（无 key 可跑）
        self.auto_approve = auto_approve
        self._results = []

        # 延迟导入，避免包级循环依赖
        from monitoring.metrics import MetricsCollector
        from monitoring.evaluator import SessionEvaluator
        from monitoring.logger import setup_logger
        self.metrics = MetricsCollector()
        self.evaluator = SessionEvaluator(llm=self.llm)
        self.log = setup_logger("benchmark", log_dir="logs")

    # ---------- 桩 LLM（无 API key 时保证可跑通）----------
    def _make_stub_llm(self):
        """构造一个极简桩 LLM：主管按状态机路由，各 Agent 返回固定内容。"""
        from langchain_core.messages import AIMessage
        from langchain_core.messages import SystemMessage

        class StructuredStub:
            def invoke(self, messages):
                st = next((str(x.content) for x in messages
                           if type(x).__name__ == "SystemMessage"), "")
                if "主管" in st:
                    s2 = str(messages[1].content) if len(messages) > 1 else ""
                    if "未分类" in s2:
                        return {"next": "classify"}
                    if "已检索=无" in s2:
                        return {"next": "retrieve"}
                    if "已生成成稿=无" in s2:
                        return {"next": "respond"}
                    return {"next": "human_approve"}
                if "分类器" in st:
                    return {"category": "退换货与售后"}
                return {"next": "FINISH"}

        class BindToolsStub:
            def __init__(self, tools):
                self.tools = tools
                self._n = 0
            def invoke(self, messages):
                self._n += 1
                if self._n == 1 and self.tools:
                    return AIMessage(content="", tool_calls=[{
                        "name": "lookup_faq",
                        "args": {"query": "退货", "category": "退换货与售后"},
                        "id": "stub-1"}])
                return AIMessage(content="ok")

        class StubLLM:
            def with_structured_output(self, schema):
                return StructuredStub()
            def bind_tools(self, tools):
                return BindToolsStub(tools)
            def invoke(self, messages):
                return AIMessage(content="【成稿】亲，已为您查询到相关处理方案，请保持商品完好，可在订单页申请退换。")

        return StubLLM()

    # ---------- 单条会话执行 ----------
    def _run_one(self, case: dict) -> dict:
        """跑一条测试用例，返回 {case, ok, elapsed, messages, session_data}。"""
        import cs_supervisor as cs
        from langgraph.types import Command

        sid = f"bench-{uuid.uuid4().hex[:8]}"
        thread = cs.thread_cfg(sid)
        t0 = time.time()
        ok = True
        # 注入 stub 或真实 llm：cs_supervisor 节点引用模块级 cs.llm，用后必须恢复原值
        # （避免与 api_server 等其他调用方互相污染全局状态）
        original_llm = cs.llm
        try:
            cs.llm = self._make_stub_llm() if self.llm is None else self.llm
            result = cs.app.invoke(cs.new_session_input(case["question"]), thread)
            # 自动人工审批（图停在 human_approve）
            while "__interrupt__" in result:
                if not self.auto_approve:
                    break
                result = cs.app.invoke(
                    Command(resume={"action": "approve", "feedback": ""}), thread)
        except Exception as e:
            ok = False
            self.log.error(f"bench_case_failed: {case['question'][:30]} | {type(e).__name__}: {str(e)[:120]}")
            elapsed = time.time() - t0
            self.metrics.counter("errors")
            return {"case": case, "ok": False, "elapsed": elapsed,
                    "error": f"{type(e).__name__}: {str(e)[:120]}"}
        finally:
            cs.llm = original_llm

        elapsed = time.time() - t0
        state = cs.app.get_state(thread).values if cs.app.get_state(thread) else {}
        session_data = {
            "session_id": sid,
            "messages": state.get("messages", []),
            "category": state.get("category", ""),
            "context": state.get("context", ""),
            "draft": state.get("draft", ""),
            "status": "completed" if ok else "failed",
            "review_count": state.get("review_count", 0),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        # 打点 + 评估
        self.metrics.counter("requests")
        self.metrics.counter("tokens", max(1, len(str(state.get("draft", ""))) // 2))
        self.metrics.timing("latency", elapsed)
        eval_result = self.evaluator.evaluate(session_data)

        category_ok = (state.get("category", "") == case["category"]) if case.get("category") else True
        return {
            "case": case, "ok": ok, "elapsed": round(elapsed, 3),
            "category": state.get("category", ""),
            "category_match": category_ok,
            "eval": eval_result,
            "session_data": session_data,
        }

    # ---------- 批量执行 ----------
    def run_all(self) -> List[dict]:
        """逐条跑测试集，收集结果。"""
        print(f"[benchmark] 开始跑 {len(self.cases)} 条测试用例...")
        for i, case in enumerate(self.cases, 1):
            r = self._run_one(case)
            self._results.append(r)
            tag = "✅" if r["ok"] else "❌"
            print(f"  {tag} [{i}/{len(self.cases)}] {case['question'][:24]}… "
                  f"耗时 {r['elapsed']}s | 类别={r.get('category','?')} "
                  f"| 匹配={r.get('category_match', '-')} | 分={r['eval']['score']}")
        print(f"[benchmark] 完成，成功率 {self.success_rate()}")
        return self._results

    # ---------- 汇总指标 ----------
    def success_rate(self) -> float:
        if not self._results:
            return 0.0
        return round(sum(1 for r in self._results if r["ok"]) / len(self._results), 3)

    def _build_summary(self) -> dict:
        """评估汇总（复用 evaluator.aggregate）。"""
        evals = [r["eval"] for r in self._results]
        summary = self.evaluator.aggregate(evals)
        summary["success_rate"] = self.success_rate()
        summary["avg_latency"] = (
            round(sum(r["elapsed"] for r in self._results) / len(self._results), 3)
            if self._results else None)
        return summary

    # ---------- 报告输出（衔接 monitoring.reporter）----------
    def report(self, output_dir: str = "reports", title: str = None) -> str:
        """生成三格式报告到 output_dir，返回主报告路径。"""
        from monitoring.reporter import ReportGenerator

        # messages 是 LangChain 对象，序列化前转成可 JSON 的普通 dict
        def _serializable(session_data: dict) -> dict:
            sd = dict(session_data)
            msgs = []
            for m in sd.get("messages", []):
                msgs.append({"type": type(m).__name__, "content": str(m.content)})
            sd["messages"] = msgs
            return sd

        rg = ReportGenerator(title or "aftersale-agent 综合实战报告（模块 11）")
        cases_out = [{**r, "session_data": _serializable(r["session_data"])}
                     for r in self._results]
        report = rg.build(self.metrics.snapshot(), self._build_summary(),
                          {"cases": cases_out})
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = f"{output_dir}/benchmark-{stamp}"
        rg.save(report, f"{base}.json", "json")
        rg.save(report, f"{base}.md", "markdown")
        rg.save(report, f"{base}.html", "html")
        return f"{base}.md"


# 命令行直接跑：python -m practice.benchmark
if __name__ == "__main__":
    runner = BenchmarkRunner()   # llm=None → 桩 LLM，无需 API key
    runner.run_all()
    path = runner.report()
    print(f"\n✅ 报告已生成：{path}")
