# -*- coding: utf-8 -*-
"""
evaluator.py —— 会话质量评估器（模块 10 核心组件 3）

设计：两层评估叠加
1. 规则层（零成本）  : 复用 quality_inspector.inspect_session（响应时间/检索命中/禁用词/驳回次数）
2. LLM 层（可选）    : 用 LLM 对"成稿"打三个维度分：相关性 / 忠实度 / 帮助度（0~10）
   - 有 API key → 真实打分
   - 无 key / 失败 → 回退为"规则分数 + 中性 LLM 分数"（不崩，缺省占位）

与项目衔接：practice 包（模块 11）在每次会话结束后调用 evaluator，
把分数 + 指标一起写进报告 → 综合实战的"评估闭环"。
"""
import os
from typing import Optional


def _try_import_quality_inspector():
    """延迟导入 quality_inspector，避免包级循环依赖。"""
    try:
        import quality_inspector as qi
        return qi
    except ImportError:
        return None


class SessionEvaluator:
    """会话质量评估器。

    用法：
        ev = SessionEvaluator()
        result = ev.evaluate(session_data)   # 规则质检 + LLM 评分
        summary = ev.aggregate([r1, r2])     # 多会话汇总
    """

    def __init__(self, llm=None):
        """llm 可选：传则用 LLM 打分；不传则只做规则评估。"""
        self._qi = _try_import_quality_inspector()
        self._llm = llm  # ChatOpenAI 实例（可选）

    # ---------- 规则层：复用 quality_inspector ----------
    def _rule_report(self, session_data: dict) -> dict:
        """规则质检：响应时间/检索命中/成稿长度/禁用词/驳回次数。"""
        if self._qi is None:
            return {"score": 100, "passed": True, "issues": []}
        report = self._qi.inspect_session(session_data)
        return {
            "rule_score": report["score"],
            "rule_passed": report["passed"],
            "issues": report["issues"],
        }

    # ---------- LLM 层：相关性/忠实度/帮助度 ----------
    def _llm_score(self, session_data: dict) -> dict:
        """用 LLM 给成稿打三维度分（0~10）。

        无 llm / 无 key / 调用失败 → 返回中性占位（不阻断流程）。
        """
        if self._llm is None:
            return {"relatedness": None, "faithfulness": None, "helpfulness": None,
                    "mode": "rule_only"}

        question = self._extract_user_question(session_data)
        draft = session_data.get("draft", "")
        context = session_data.get("context", "")

        prompt = (
            "你是客服质检员。请从三个维度给下面的客服回答打分（0~10 整数）：\n"
            "1) 相关性 relatedness：回答是否针对用户问题\n"
            "2) 忠实度 faithfulness：回答是否基于给定检索上下文（不编造）\n"
            "3) 帮助度 helpfulness：回答是否真正解决问题\n\n"
            f"【用户问题】{question}\n"
            f"【检索上下文】{context[:500]}\n"
            f"【客服回答】{draft[:500]}\n\n"
            "只输出 JSON：{\"relatedness\": 8, \"faithfulness\": 7, \"helpfulness\": 9}"
        )
        try:
            resp = self._llm.invoke(prompt)
            import re, json
            m = re.search(r"\{.*\}", str(resp.content), re.DOTALL)
            if not m:
                return self._neutral("llm_bad_format")
            scores = json.loads(m.group(0))
            return {
                "relatedness": scores.get("relatedness"),
                "faithfulness": scores.get("faithfulness"),
                "helpfulness": scores.get("helpfulness"),
                "mode": "llm",
            }
        except Exception as e:
            return {**self._neutral("llm_error"), "error": str(e)[:120]}

    @staticmethod
    def _neutral(mode: str) -> dict:
        return {"relatedness": None, "faithfulness": None, "helpfulness": None,
                "mode": mode}

    @staticmethod
    def _extract_user_question(session_data: dict) -> str:
        """从 messages 里取第一条用户消息作为问题。"""
        for m in session_data.get("messages", []):
            if type(m).__name__ == "HumanMessage":
                return str(m.content)
        return ""

    # ---------- 对外主接口 ----------
    def evaluate(self, session_data: dict) -> dict:
        """对一次会话做完整评估（规则 + LLM），返回合并报告。"""
        rule = self._rule_report(session_data)
        llm = self._llm_score(session_data)
        # 综合分：规则分为主，LLM 三维度取均值按 20% 加权（若可得）
        llm_vals = [v for v in (llm["relatedness"], llm["faithfulness"], llm["helpfulness"])
                    if v is not None]
        final = rule["rule_score"]
        if llm_vals:
            llm_avg = sum(llm_vals) / len(llm_vals) * 10  # 0~10 → 0~100
            final = round(0.8 * rule["rule_score"] + 0.2 * llm_avg, 1)
        return {
            "session_id": session_data.get("session_id", ""),
            "status": session_data.get("status", ""),
            "score": final,
            "rule": rule,
            "llm": llm,
            "checked_at": __import__("datetime").datetime.now().isoformat(),
        }

    # ---------- 多会话汇总 ----------
    def aggregate(self, results: list) -> dict:
        """汇总一批评估结果：平均分/通过率/问题频次 TopN。"""
        if not results:
            return {"count": 0}
        scores = [r["score"] for r in results if r.get("score") is not None]
        passed = [r for r in results if r["rule"]["rule_passed"]]
        # 问题频次统计
        issue_counter: dict = {}
        for r in results:
            for iss in r["rule"]["issues"]:
                issue_counter[iss] = issue_counter.get(iss, 0) + 1
        top_issues = sorted(issue_counter.items(), key=lambda x: -x[1])[:5]
        return {
            "count": len(results),
            "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
            "pass_rate": round(len(passed) / len(results), 3) if results else 0,
            "top_issues": top_issues,
        }
