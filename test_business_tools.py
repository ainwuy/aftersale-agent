"""验证 retrieve 节点能调用 order 工具（用桩 LLM 模拟 LLM 的 tool_calls 决策）"""
import os
os.environ["AFTERSALE_EMBED_BACKEND"] = "off"
os.environ["AFTERSALE_CHECKPOINTER"] = "memory"

import cs_supervisor as cs
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

# 桩 LLM：模拟 LLM 看到 "查 CS20260820001 订单" → 调 lookup_order 工具
class BindToolsStub:
    def __init__(s, t):
        s.t = t
        s._call_count = 0
    def invoke(s, m):
        s._call_count += 1
        if s._call_count == 1:
            # 第一次：模拟 LLM 决策 → 调 lookup_order 工具
            return AIMessage(content="", tool_calls=[{
                "name": "lookup_order",
                "args": {"order_no": "CS20260820001"},
                "id": "call_1"
            }])
        # 第二次：拿到 ToolMessage 后就结束（无 tool_calls）
        return AIMessage(content="已查到订单 CS20260820001，状态：已签收")

class StructuredStub:
    def invoke(s, m):
        st = next((str(x.content) for x in m if type(x).__name__ == "SystemMessage"), "")
        if "主管" in st:
            s2 = str(m[1].content) if len(m) > 1 else ""
            if "未分类" in s2: return {"next": "classify"}
            if "已检索=无" in s2: return {"next": "retrieve"}
            if "已生成成稿=无" in s2: return {"next": "respond"}
            return {"next": "human_approve"}
        if "分类器" in st: return {"category": "退换货与售后"}
        return {"next": "FINISH"}

class FakeLLM:
    def with_structured_output(s, sc): return StructuredStub()
    def bind_tools(s, t): return BindToolsStub(t)
    def invoke(s, m): return AIMessage(content="【成稿】已查到您的订单 CS20260820001，状态：已签收。")

cs.llm = FakeLLM()

# 直接调 retrieve 节点函数
state = {
    "messages": [HumanMessage("我订单 CS20260820001 是什么状态？")],
    "category": "退换货与售后",
    "step": 2,
}
result = cs.retrieve(state)
print("=== retrieve 返回 ===")
print("新增消息数:", len(result["messages"]))
print("context 长度:", len(result["context"]))
print("context 首字:", result["context"][:100] if result["context"] else "(空)")

# 验证 context 包含订单号
assert "CS20260820001" in result["context"], "context 应包含订单号"
# 验证有 ToolMessage（订单工具被调）
has_tool_msg = any(type(m).__name__ == "ToolMessage" for m in result["messages"])
assert has_tool_msg, "应有 ToolMessage（lookup_order 被调用）"

print("\n✅ 业务闭环验证通过：retrieve 能调用 lookup_order 工具，工具结果成为 context")