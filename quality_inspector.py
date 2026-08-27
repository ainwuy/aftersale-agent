"""会话质检 + 知识库自动补全

职责（对应业务需求分析报告 §10 演进路线）：
- 质检规则：响应时间、检索命中、成稿长度、禁用词、解决率
- 质检报告：每会话生成 quality_report（dict）
- 知识库自补全：对话历史里"用户问+AI答"若 FAQ 未命中且成稿被人工 approve → 自动追加到 客服FAQ.md
- 阈值：必须有人工 approve 才追加（防止垃圾入库）
"""
import os
import re
import time
from datetime import datetime
from typing import Optional

# === 质检规则配置 ===
QUALITY_RULES = {
    "max_response_time_sec": 90,    # 单次响应超时阈值
    "min_draft_chars": 20,          # 成稿最短字数
    "max_draft_chars": 500,         # 成稿最长字数（避免啰嗦）
    "min_context_chars": 5,         # 检索上下文最短字数（小于则视为未检索到）
    "forbidden_words": ["傻逼", "滚", "不知道", "无法回答", "我猜"],
    "required_phrases_for_apology": ["抱歉", "对不起", "遗憾"],  # 投诉类问题必备
}


def inspect_session(session_data: dict) -> dict:
    """对一次会话做质检，返回 quality_report dict。
    session_data 应含：messages, category, context, draft, status, review_count, created_at, completed_at
    """
    report = {
        "session_id": session_data.get("session_id", ""),
        "category": session_data.get("category", ""),
        "status": session_data.get("status", ""),
        "issues": [],
        "score": 100,           # 满分 100，每个问题扣分
        "passed": True,
        "checked_at": datetime.now().isoformat(),
    }

    # 规则 1：响应时间
    created = session_data.get("created_at")
    completed = session_data.get("completed_at")
    if created and completed:
        elapsed = (datetime.fromisoformat(completed) - datetime.fromisoformat(created)).total_seconds()
        report["response_time_sec"] = round(elapsed, 1)
        if elapsed > QUALITY_RULES["max_response_time_sec"]:
            report["issues"].append(f"响应超时（{int(elapsed)}s > {QUALITY_RULES['max_response_time_sec']}s）")
            report["score"] -= 20

    # 规则 2：检索是否命中
    context = session_data.get("context", "") or ""
    if len(context) < QUALITY_RULES["min_context_chars"]:
        report["issues"].append("未检索到 FAQ/订单（context 为空或过短）")
        report["score"] -= 25

    # 规则 3：成稿长度
    draft = session_data.get("draft", "") or ""
    if len(draft) < QUALITY_RULES["min_draft_chars"]:
        report["issues"].append(f"成稿过短（{len(draft)} 字 < {QUALITY_RULES['min_draft_chars']}）")
        report["score"] -= 20
    elif len(draft) > QUALITY_RULES["max_draft_chars"]:
        report["issues"].append(f"成稿过长（{len(draft)} 字 > {QUALITY_RULES['max_draft_chars']}）")
        report["score"] -= 10

    # 规则 4：禁用词
    for w in QUALITY_RULES["forbidden_words"]:
        if w in draft:
            report["issues"].append(f"成稿含禁用词：{w}")
            report["score"] -= 30
            break

    # 规则 5：投诉类问题必须有致歉语
    if session_data.get("category") == "投诉与维权":
        if not any(p in draft for p in QUALITY_RULES["required_phrases_for_apology"]):
            report["issues"].append("投诉类问题成稿未包含致歉语")
            report["score"] -= 15

    # 规则 6：被驳回次数过多
    review_count = session_data.get("review_count", 0)
    if review_count >= 2:
        report["issues"].append(f"被驳回 {review_count} 次（可能成稿质量差）")
        report["score"] -= 10

    report["passed"] = report["score"] >= 60
    return report


# === 知识库自补全：从 approve 过的对话抽 FAQ ===

def _extract_faq_from_session(session_data: dict) -> Optional[dict]:
    """从一次已 approve 的会话里抽取"用户问 + AI答"作为新 FAQ。
    必须满足：
    - status == 'completed'（已 approve）
    - 检索 context 未命中现有 FAQ（视为新问题）
    - 成稿 draft 长度合理
    - 用户消息明确为问题（不是闲聊）
    """
    if session_data.get("status") != "completed":
        return None

    # 找用户首条消息作为问题
    user_msgs = [m for m in session_data.get("messages", [])
                 if type(m).__name__ == "HumanMessage"]
    if not user_msgs:
        return None
    question = str(user_msgs[0].content).strip()
    if len(question) < 5 or len(question) > 100:
        return None  # 太短或太长都不像 FAQ 问题

    # 成稿作为答案
    draft = (session_data.get("draft") or "").strip()
    if len(draft) < QUALITY_RULES["min_draft_chars"]:
        return None

    # context 是否未命中现有 FAQ：看 context 是否含"【"标记（FAQ 命中会带【大类】前缀）
    context = session_data.get("context", "") or ""
    if "【" in context and "】Q：" in context:
        return None  # 已命中现有 FAQ，不需要补全

    return {
        "category": session_data.get("category", "其他"),
        "question": question,
        "answer": draft,
        "source_session": session_data.get("session_id", ""),
        "added_at": datetime.now().isoformat(),
    }


def append_to_faq(faq_entry: dict, faq_path: str = None) -> bool:
    """把新 FAQ 追加到 客服FAQ.md。
    追加格式：
        ## N、<category>类（自动补全）
        **Q：<question>**
        A：<answer>

    幂等：检查问题是否已在 FAQ 里（避免重复追加）。
    """
    faq_path = faq_path or os.path.join(os.path.dirname(__file__), "客服FAQ.md")
    if not os.path.exists(faq_path):
        print(f"[knowledge] FAQ 文件不存在: {faq_path}")
        return False

    # 幂等检查：问题文本是否已存在
    with open(faq_path, "r", encoding="utf-8") as f:
        existing = f.read()
    if faq_entry["question"] in existing:
        print(f"[knowledge] 问题已存在，跳过: {faq_entry['question'][:30]}")
        return False

    # 追加到文件末尾
    block = f"""
## 自动补全 · {faq_entry['category']}类

**Q：{faq_entry['question']}**
A：{faq_entry['answer']}

（来源：会话 {faq_entry.get('source_session', '')[:8]}，{faq_entry.get('added_at', '')[:10]} 自动入库）
"""
    with open(faq_path, "a", encoding="utf-8") as f:
        f.write(block)
    print(f"[knowledge] 已追加新 FAQ：{faq_entry['question'][:30]}...")
    return True


def quality_and_update(session_data: dict, faq_path: str = None) -> dict:
    """一站式：先质检，再判断是否补全知识库。返回综合报告。"""
    report = inspect_session(session_data)

    # 只有质检通过 + 已完成 + 抽出有效条目 才追加
    if report["passed"] and session_data.get("status") == "completed":
        faq_entry = _extract_faq_from_session(session_data)
        if faq_entry:
            added = append_to_faq(faq_entry, faq_path)
            report["knowledge_updated"] = added
            report["new_faq"] = faq_entry if added else None
        else:
            report["knowledge_updated"] = False
    else:
        report["knowledge_updated"] = False

    return report