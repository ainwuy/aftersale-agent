# cs_supervisor.py - Task 6：售后客服多 Agent（Supervisor 模式落地 aftersale-cs）
# ---------------------------------------------------------------------------
# 流水线：supervisor(主管路由) → classify(分类) / retrieve(检索FAQ)
#         / respond(回答) / human_approve(人工把关) / FINISH
# 知识库：运行时解析本地 客服FAQ.md（问答对；可选构建 ChromaDB + bge-m3 向量索引）
# 检索：lookup_faq 优先走 ChromaDB 真向量检索（vector_store.py），不可用时自动回退轻量关键词检索
# 工程能力：checkpointer 断点续跑 + interrupt 人工介入（复用 Task 5）
# ---------------------------------------------------------------------------

import os, re, time, string
from pathlib import Path
from dotenv import load_dotenv

# .env 优先读本项目目录；本项目没有则复用 supervisor_demo 的（避免 API key 散落多份）
_ENV_LOCAL = Path(__file__).parent / ".env"
_ENV_FB = Path("E:/python/code/supervisor_demo/.env")
load_dotenv(_ENV_LOCAL if _ENV_LOCAL.exists() else _ENV_FB)

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import MemorySaver          # 断点续跑
from langgraph.types import interrupt, Command               # 人工介入
from typing import TypedDict, Annotated, Literal
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from langchain_core.tools import tool

# ===== LLM（与 supervisor_demo 同配置，复用 QWEN key）=====
llm = ChatOpenAI(
    model="qwen3.7-plus-2026-05-26",
    temperature=0,                       # 路由/分类要稳定，设 0
    api_key=os.getenv("QWEN"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    timeout=120,
    max_retries=2,
)

# ===== 知识库：解析 客服FAQ.md（轻量，不依赖 chromadb）=====
# 分类 Agent 可输出的业务大类（与下方 FAQ 解析出的 category 一一对应）
CLEAN_CATEGORIES = ["售前咨询", "下单与支付", "发货与物流", "退换货与售后",
                    "退款、差价与补偿", "发票、会员与活动", "账号与人工服务", "投诉与维权", "其他"]


def _clean_cat(tag: str) -> str:
    """把 '四、退换货与售后类（高频）' 这类长名清洗为 '退换货与售后'。"""
    tag = re.sub(r'^[一二三四五六七八九十]+、', '', tag)   # 去序号
    tag = re.sub(r'（高频）', '', tag)                    # 去高频标注
    tag = re.sub(r'类$', '', tag)                         # 去末尾「类」
    return tag.strip()


def _load_faq(path: Path) -> list[dict]:
    """把客服 FAQ Markdown 解析成 [{category, question, answer}] 列表（不依赖 chromadb）。

    用正则一次性抽取 '**Q：...**' 与紧随的 'A：...' 块,可正确处理 Q/A 之间的空行,
    也不会因为 answer 内出现 '**加粗**' 而截断(靠 lookahead 定位到下一个 Q 块为止)。
    """
    if not path.exists():
        return []
    content = path.read_text(encoding="utf-8")
    pat_qa = re.compile(r'\*\*Q：(.*?)\*\*\s*\nA：(.*?)(?=\n\s*\*\*Q：|$)', re.DOTALL)
    pat_main = re.compile(r'^##\s+(.*)$', re.MULTILINE)
    sections = re.split(r'(^##\s+.+$)', content, flags=re.MULTILINE)
    chunks = []
    current_tag = ""
    for sec in sections:
        sec_stripped = sec.strip()
        if not sec_stripped:
            continue
        if sec_stripped.startswith("## "):
            m = pat_main.match(sec_stripped)
            if m:
                current_tag = _clean_cat(m.group(1))
            continue
        for q, a in pat_qa.findall(sec):
            chunks.append({"category": current_tag, "question": q.strip(), "answer": a.strip()})
    return chunks


FAQ_LIST = _load_faq(Path(__file__).parent / "客服FAQ.md")
print(f"[知识库] 已加载 {len(FAQ_LIST)} 条 FAQ（来源：客服FAQ.md）")

# ---- 轻量中文检索（bigram + 单字命中计数）----
_PUNCT = string.punctuation + "，。！？；：、（）《》「」“”‘’—…·\t\n "


def _tokenize(text: str) -> set:
    text = text.translate(str.maketrans("", "", _PUNCT))
    grams = set(text)
    for j in range(len(text) - 1):
        grams.add(text[j:j + 2])          # 2-gram 提升召回
    return grams


def _score(faq: dict, q_tokens: set) -> int:
    doc = faq["question"] + " " + faq["answer"]
    return len(q_tokens & _tokenize(doc))


@tool
def lookup_faq(query: str, category: str = "") -> str:
    """在客服 FAQ 知识库检索相关问题与标准答案。
    query: 用户的问题; category: 可选,传入业务大类名(如'退换货与售后')可缩小检索范围。
    返回命中度最高的若干条标准问答;若知识库无相关内容,明确说明未找到。

    检索实现：优先走 ChromaDB + bge-m3 真向量检索（vector_store.py），
    向量库不可用时自动回退到本文件下方的轻量 bigram 检索。"""
    if not FAQ_LIST:
        return "（知识库未加载，请确认 客服FAQ.md 在同目录）"
    # ---- 优先：真向量检索（ChromaDB + bge-m3）----
    if VECTOR_STORE is not None:
        try:
            hits = VECTOR_STORE.search(query, k=3, category=category or None)
            if hits:
                return "\n\n".join(
                    f"【{h['category']}】Q：{h['question']}\nA：{h['answer']}"
                    for h in hits)
        except Exception as e:
            print(f"  ⚠️ 向量检索失败，回退轻量检索：{type(e).__name__}: {e}")
    # ---- 兜底：轻量中文检索（bigram + 单字命中计数）----
    q_tokens = _tokenize(query)
    candidates = FAQ_LIST
    if category:
        in_cat = [f for f in FAQ_LIST if f["category"] == category]
        if in_cat:
            candidates = in_cat
    scored = [(f, _score(f, q_tokens)) for f in candidates]
    scored = [s for s in scored if s[1] > 0]
    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[:3]
    if not top:
        return "（未在知识库中找到相关条目）"
    return "\n\n".join(f"【{f['category']}】Q：{f['question']}\nA：{f['answer']}" for f, _ in top)


# ---- 向量检索初始化（ChromaDB + 多 embedding 后端），失败自动回退轻量检索 ----
def _init_vector_store():
    # 默认 dashscope：复用已有 QWEN key（百炼 qwen3.7-text-embedding），无需注册新平台
    backend = os.getenv("AFTERSALE_EMBED_BACKEND", "dashscope").strip().lower()
    if backend == "off":
        print("[向量检索] 已通过 AFTERSALE_EMBED_BACKEND=off 关闭，使用轻量检索")
        return None
    # dashscope 后端用 QWEN key；siliconflow 后端用 SILICONFLOW_API_KEY
    api_key = os.getenv("QWEN" if backend == "dashscope" else "SILICONFLOW_API_KEY")
    try:
        from vector_store import BgeM3EmbeddingFunction, FAQVectorStore
        embed_fn = BgeM3EmbeddingFunction(backend=backend, api_key=api_key)
        store = FAQVectorStore(
            persist_dir=os.getenv("AFTERSALE_VECTOR_DIR", "faq_chromadb"),
            embedding_function=embed_fn)
        if store.count() == 0:
            n = store.rebuild(FAQ_LIST)
            print(f"[向量检索] 已构建索引 {n} 条（backend={backend}，目录 {store.persist_dir}）")
        else:
            print(f"[向量检索] 加载已有索引 {store.count()} 条（backend={backend}）")
        return store
    except Exception as e:
        print(f"[向量检索] ⚠️ 初始化失败，回退轻量检索：{type(e).__name__}: {e}")
        return None


VECTOR_STORE = _init_vector_store()


# ===== State / Route =====
class State(TypedDict):
    messages: Annotated[list, add_messages]   # 全部对话历史（累加）
    next: str                                 # 主管写、路由函数读
    step: int                                 # 循环计数（防死循环）
    category: str                             # 分类 Agent 输出
    context: str                              # 检索 Agent 输出（检索到的 FAQ）
    draft: str                                # 回答 Agent 成稿
    review_count: int                         # 已被退回修改的次数


class Route(TypedDict):
    next: Literal["classify", "retrieve", "respond", "human_approve", "FINISH"]


class Category(TypedDict):
    category: Literal["售前咨询", "下单与支付", "发货与物流", "退换货与售后",
                      "退款、差价与补偿", "发票、会员与活动", "账号与人工服务", "投诉与维权", "其他"]


# ===== Supervisor（决策清单，禁止跳步）=====
def supervisor(state):
    if state.get("step", 0) >= 8:
        print("  ⚠️ 达到循环上限(8),强制 FINISH")
        return {"next": "FINISH"}
    print(f"\n🧠 主管思考中... (第 {state.get('step', 0) + 1} 轮)")
    status = (f"当前状态 -> 分类={state.get('category') or '未分类'}; "
              f"已检索={'有' if state.get('context') else '无'}; "
              f"已生成成稿={'有' if state.get('draft') else '无'}")
    t = time.time()
    decision = llm.with_structured_output(Route).invoke([
        SystemMessage(
            "你是售后客服中心主管,严格按下列决策清单(逐项检查)派单:\n"
            "1) 若'分类'为'未分类' -> 选 classify\n"
            "2) 若已分类,但'已检索'为'无' -> 选 retrieve\n"
            "3) 若已检索,但'已生成成稿'为'无' -> 选 respond\n"
            "4) 若已生成成稿 -> 选 human_approve(交人工审核)\n"
            "5) 审核通过后 -> 选 FINISH\n"
            "禁止跳步;LLM 路由有随机性,请严格按清单走。"
        ),
        HumanMessage(status + "\n\n对话历史:\n" + str(state["messages"])),
    ])
    print(f"  ✓ 主管决策: {decision['next']}  (耗时 {round(time.time() - t, 1)}s)")
    return {"next": decision["next"], "step": state.get("step", 0) + 1}


def route(state):
    return state["next"]


# ===== 通用 ReAct 循环（LLM -> tool_calls? -> ToolNode -> LLM -> ...）=====
def _agent_loop(state, tools, system_prompt, name, max_inner=4):
    # 工具名 → 工具对象 映射，方便手动调
    tool_map = {t.name: t for t in tools} if tools else {}
    msgs = [SystemMessage(system_prompt)] + list(state["messages"])
    llm_t = llm.bind_tools(tools) if tools else llm
    rounds = 0
    while rounds < max_inner:
        resp = llm_t.invoke(msgs)
        msgs.append(resp)
        if not resp.tool_calls or not tool_map:
            break
        for tc in resp.tool_calls:
            print(f"  🔧 {name} 调用工具: {tc['name']}({tc.get('args')})")
            # 手动调工具，绕开 ToolNode API 差异
            try:
                tool_obj = tool_map.get(tc["name"])
                if tool_obj:
                    tool_result = tool_obj.invoke(tc.get("args", {}))
                    msgs.append(ToolMessage(content=str(tool_result), tool_call_id=tc.get("id", "")))
                else:
                    msgs.append(ToolMessage(content=f"（工具 {tc['name']} 不存在）", tool_call_id=tc.get("id", "")))
            except Exception as e:
                msgs.append(ToolMessage(content=f"（工具执行失败: {e}）", tool_call_id=tc.get("id", "")))
        rounds += 1
    print(f"  ✓ {name} 完成 (内部 {rounds} 轮)")
    return msgs[1 + len(state["messages"]):]     # 只回传本轮新增，避免历史重复累加


# ===== 分类 Agent =====
def classify(state):
    print("🏷️ 分类 Agent 启动")
    t = time.time()
    cat = llm.with_structured_output(Category).invoke([
        SystemMessage("你是客服分类器。判断用户问题属于哪个业务大类,从给定枚举中选一个。"),
        HumanMessage(str(state["messages"])),
    ])
    c = cat["category"]
    print(f"  ✓ 分类结果: {c}  (耗时 {round(time.time() - t, 1)}s)")
    # 把分类结论写进对话历史,方便主管后续判断;同时存结构化字段
    return {"messages": [AIMessage(f"【分类】用户问题属于：{c}")], "category": c}


# ===== 检索 Agent =====
def retrieve(state):
    """业务工具 Agent：可查 FAQ、查订单、查物流、申请退货/退款。
    决策：用户问题里若出现订单号（CS 开头）/物流单号（SF/YT 等开头）/退款退货关键词，
    优先调对应订单工具；纯 FAQ 咨询调 lookup_faq。LLM 自己根据工具描述判断即可。
    """
    print("🔎 业务工具 Agent 启动（FAQ + 订单/物流/退款）")
    # 把 FAQ 检索工具 + 订单系统工具一起挂给 LLM，让它根据用户问题选工具
    all_tools = [lookup_faq]
    try:
        from tools import ORDER_TOOLS
        all_tools.extend(ORDER_TOOLS)
    except Exception as e:
        print(f"  ⚠️ 订单工具未加载：{e}")
    new_msgs = _agent_loop(
        state,
        tools=all_tools,
        system_prompt=(
            "你是售后业务助手，必须根据用户问题调用工具：\n"
            "- 纯 FAQ 咨询 → 调 lookup_faq（可传 category 缩小范围）\n"
            "- 含订单号（如 CS20260820001）→ 调 lookup_order 查详情\n"
            "- 含物流单号（如 SF1029384756）→ 调 track_logistics 查物流\n"
            "- 申请退货/退款 → 调 request_return / request_refund（先调 lookup_order 确认订单存在）\n"
            "禁止返回空。返回的工具调用结果将作为上下文交给回答 Agent。"
        ),
        name="retrieve",
    )
    # 从新增消息里取最后一条 ToolMessage 作为检索上下文
    ctx = ""
    for m in reversed(new_msgs):
        if type(m).__name__ == "ToolMessage":
            ctx = str(m.content)
            break
    return {"messages": new_msgs, "context": ctx}


# ===== 回答 Agent（只生成成稿，不直接发送）=====
def respond(state):
    print("✍️ 回答 Agent 启动（生成成稿,暂不发送,等人工审核）")
    new_msgs = _agent_loop(
        state,
        tools=[],                       # 写手不挂工具,只产出文本
        system_prompt=(
            "你是客服应答助手。根据【检索到的 FAQ 上下文】,用友好、专业、口语化的语气,"
            "生成一段直接回复用户的成稿(150字内)。严格基于 FAQ,不要编造政策。"
            "输出纯文本,不要调用任何工具,不要自己发送。"
        ),
        name="respond",
    )
    draft = ""
    for m in reversed(new_msgs):
        if type(m).__name__ == "AIMessage":
            draft = str(m.content)
            break
    return {"messages": new_msgs, "draft": draft}


# ===== 人工审核节点（human-in-the-loop）=====
def _save_reply(content: str, filename: str = "reply.md") -> str:
    path = Path(__file__).parent / filename
    path.write_text(content.strip(), encoding="utf-8")
    return str(path)


def human_approve(state):
    draft = state.get("draft", "")
    # interrupt() 让图在此暂停,把成稿交给人类;resume 的值即人类传回的字典
    decision = interrupt({
        "type": "approval",
        "draft": draft,
        "question": "请审核客服回复成稿。approve=发送 / reject=退回修改(最多2次后强制发送)。",
    })
    action = (decision or {}).get("action", "approve")
    reviews = state.get("review_count", 0)
    if action == "reject" and reviews < 2:
        feedback = (decision or {}).get("feedback", "")
        print(f"  ↩️ 人工退回修改(第 {reviews + 1} 次): {feedback}")
        return {
            "messages": [HumanMessage(f"人工审核意见,请据此修改回复: {feedback}")],
            "next": "respond",
            "review_count": reviews + 1,
        }
    if action == "reject":
        print("  ⚠️ 已达最大退回次数,强制发送")
    path = _save_reply(draft)                       # 仅此处（批准后）才真正落盘
    print(f"  📢 人工已批准,回复已发送并保存至 {path}")
    return {"next": "FINISH"}


# ===== Graph 组装 =====
g = StateGraph(State)
g.add_node("supervisor", supervisor)
g.add_node("classify", classify)
g.add_node("retrieve", retrieve)
g.add_node("respond", respond)
g.add_node("human_approve", human_approve)
g.add_edge(START, "supervisor")
g.add_conditional_edges("supervisor", route,
    {"classify": "classify", "retrieve": "retrieve", "respond": "respond",
     "human_approve": "human_approve", "FINISH": END})
g.add_edge("classify", "supervisor")
g.add_edge("retrieve", "supervisor")
g.add_edge("respond", "supervisor")
g.add_conditional_edges("human_approve", route, {"respond": "respond", "FINISH": END})

# 挂 checkpointer：崩溃/暂停后,用同一 thread_id 能接着跑。
# 默认 MemorySaver（单进程内存）；配 AFTERSALE_CHECKPOINTER=redis + Redis 连接参数 → 跨进程持久。
def _make_checkpointer():
    backend = os.getenv("AFTERSALE_CHECKPOINTER", "memory").strip().lower()
    if backend == "redis":
        try:
            from langgraph.checkpoint.redis import RedisSaver
            host = os.getenv("AFTERSALE_REDIS_HOST", "127.0.0.1")
            port = int(os.getenv("AFTERSALE_REDIS_PORT", "6379"))
            db = int(os.getenv("AFTERSALE_REDIS_DB", "0"))
            password = os.getenv("AFTERSALE_REDIS_PASSWORD") or None
            import redis as _redis
            client = _redis.Redis(host=host, port=port, db=db, password=password,
                                  socket_connect_timeout=3, socket_timeout=3)
            client.ping()  # 探活，失败立刻抛
            saver = RedisSaver(redis_client=client)
            print(f"[checkpointer] RedisSaver 已启用：{host}:{port}/{db}")
            return saver
        except Exception as e:
            print(f"[checkpointer] ⚠️ Redis 连接失败，回退 MemorySaver：{type(e).__name__}: {e}")
    print("[checkpointer] MemorySaver 已启用（单进程内存）")
    return MemorySaver()

checkpointer = _make_checkpointer()
app = g.compile(checkpointer=checkpointer)


# ===== 供 API / 外部调用的会话入口（不依赖交互式 input）=====
def new_session_input(user_message: str) -> dict:
    """首轮调用所需的输入（含初始结构化字段 + 用户消息）。"""
    return {
        "messages": [HumanMessage(user_message)],
        "step": 0, "category": "", "context": "", "draft": "", "review_count": 0,
    }


def thread_cfg(thread_id: str) -> dict:
    """把 session_id 映射成 LangGraph 的 thread 配置（断点续跑钥匙）。"""
    return {"configurable": {"thread_id": thread_id}}


# ===== 入口 =====
if __name__ == "__main__":
    thread = {"configurable": {"thread_id": "aftersale-task6"}}   # 断点续跑的会话钥匙
    print("=== 售后客服多 Agent（Supervisor 模式）===")
    result = app.invoke({
        "messages": [HumanMessage("我买的鞋子尺码不合适想退货，怎么操作？")],
        "step": 0, "category": "", "context": "", "draft": "", "review_count": 0,
    }, thread)

    # 图会在 human_approve 的 interrupt() 处暂停,result 里带 __interrupt__
    while "__interrupt__" in result:
        for intr in result["__interrupt__"]:
            payload = intr.value
            print("\n📋 === 人工审核（图已暂停，等待你确认）===")
            print("客服回复预览：\n" + str(payload.get("draft", ""))[:800])
        choice = input("\n👉 是否发送？(输入 approve 发送 / reject 退回修改): ").strip().lower()
        feedback = ""
        if choice == "reject":
            feedback = input("请输入修改意见: ").strip()
        resume_val = {"action": "approve" if choice == "approve" else "reject", "feedback": feedback}
        print("   恢复运行...")
        result = app.invoke(Command(resume=resume_val), thread)

    print("\n=== 运行结束 ===")
    print("最后 4 条消息:")
    for m in result["messages"][-4:]:
        print(f"  [{type(m).__name__}] {str(m.content)[:120]}")

    # 检索命中状态标记（让你一眼看出有没有查到 FAQ）
    hit = any(type(m).__name__ == "ToolMessage" and "【" in str(m.content) for m in result["messages"])
    marker = ("✅ 已在 FAQ 知识库检索到相关标准答案" if hit
              else "⚠️ 未在 FAQ 知识库检索到相关条目（已成稿但建议补充知识库）")
    print("\n" + marker)
