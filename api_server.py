# api_server.py - aftersale-cs 的 Web 服务层（FastAPI：鉴权 + 限流 + 多 Agent + 前端）
# ---------------------------------------------------------------------------
# 请求执行链路（对应《鉴权与限流实现说明.md》）：
#   浏览器 → uvicorn → RateLimitMiddleware(IP 限流) → 路由
#             → 依赖（get_current_user 鉴权 / user_rate_limit 用户限流）
#             → 业务逻辑（LangGraph 多 Agent 图 / 审批落盘）→ 响应
# 功能：
#   1) 鉴权：/api/auth/register|login|me（JWT Bearer Token，见 auth.py）
#   2) 限流：IP 级全局限流中间件 + 用户级限流依赖（见 rate_limit.py）
#   3) 客服业务：创建会话 / 续聊 / 批准 / 驳回 / 查询（需登录）
#   4) 前端：GET / 返回 static/index.html，静态资源走 /static
# ---------------------------------------------------------------------------
from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command
from pydantic import BaseModel

# 复用核心引擎（同一进程内 import 即加载 .env / 知识库 / 编译好的图）
from cs_supervisor import app, new_session_input, thread_cfg
from auth import LoginRequest, RegisterRequest, authenticate, create_token, get_current_user, register_user
from rate_limit import RateLimitMiddleware, rate_limit_status, user_rate_limit

load_dotenv()  # 兼容在项目根目录直接跑

# ===== 服务配置 =====
HOST = os.getenv("AFTERSALE_HOST", "0.0.0.0")
PORT = int(os.getenv("AFTERSALE_PORT", "8000"))
_CORS = os.getenv("AFTERSALE_CORS_ORIGINS", "*")
CORS_ORIGINS = [o.strip() for o in _CORS.split(",") if o.strip()]
STATIC_DIR = Path(__file__).parent / "static"

# ===== 会话登记表（元数据 + 状态 + 所属用户）=====
# 双写策略：内存 SESSIONS 做读写缓存（快），sessions.db 做持久化（重启不丢）。
# 图状态本身在 checkpointer 里按 session_id 存，本表只存"登记信息"。
SESSIONS_DB = Path(__file__).parent / "sessions.db"
# 会话登记容量上限：超出后按创建时间淘汰最旧会话
MAX_SESSIONS = int(os.getenv("AFTERSALE_MAX_SESSIONS", "1000"))


@contextmanager
def _sconn() -> sqlite3.Connection:
    """sessions.db 连接上下文：提交事务并关闭（防句柄泄漏）。"""
    conn = sqlite3.connect(SESSIONS_DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_sessions_db() -> None:
    with _sconn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending_approval',
                created_at TEXT,
                completed_at TEXT
            )""")


def _sess_load_all() -> dict[str, dict[str, Any]]:
    """启动时从磁盘加载全部会话登记（重启不丢）。"""
    if not SESSIONS_DB.exists():
        return {}
    with _sconn() as conn:
        rows = conn.execute(
            "SELECT session_id, username, status, created_at, completed_at FROM sessions").fetchall()
    return {r["session_id"]: {k: r[k] for k in ("username", "status", "created_at", "completed_at")}
            for r in rows}


def _sess_sync(sid: str) -> None:
    """把内存中的一条会话登记同步到磁盘（INSERT OR REPLACE）。"""
    d = SESSIONS[sid]
    with _sconn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sessions(session_id, username, status, created_at, completed_at)"
            " VALUES (?,?,?,?,?)",
            (sid, d.get("username", ""), d.get("status", ""),
             d.get("created_at"), d.get("completed_at")))


def _sess_delete(sid: str) -> None:
    """从磁盘删除一条会话登记（容量淘汰时调用）。"""
    with _sconn() as conn:
        conn.execute("DELETE FROM sessions WHERE session_id=?", (sid,))


_init_sessions_db()
SESSIONS: dict[str, dict[str, Any]] = _sess_load_all()


# ===== 请求体 =====
class StartRequest(BaseModel):
    message: str


class MessageRequest(BaseModel):
    message: str


class RejectRequest(BaseModel):
    feedback: str = ""


# ===== 工具函数 =====
def _state(thread_id: str) -> dict:
    """读取当前图状态（含 messages / category / context / draft）。"""
    snap = app.get_state(thread_cfg(thread_id))
    return snap.values if snap else {}


def _render_messages(msgs: list) -> list[dict]:
    return [{"type": type(m).__name__, "content": str(m.content)} for m in msgs]


def _run(sid: str, invoke_input: dict) -> dict:
    """执行一次图调用，记录会话状态，返回统一响应。"""
    result = app.invoke(invoke_input, thread_cfg(sid))
    if "__interrupt__" in result:
        intr = result["__interrupt__"][0].value
        draft = intr.get("draft", "")
        status = "pending_approval"
    else:
        draft = ""
        status = "completed"
    SESSIONS[sid]["status"] = status
    # 记录时间戳供质检用（与 create_session 保持 timezone-aware UTC 一致）
    if status == "pending_approval" and "created_at" not in SESSIONS[sid]:
        SESSIONS[sid]["created_at"] = datetime.now(timezone.utc).isoformat()
    if status == "completed":
        SESSIONS[sid]["completed_at"] = datetime.now(timezone.utc).isoformat()
    _sess_sync(sid)   # 状态变化同步到 sessions.db（重启不丢）

    st = _state(sid)
    return {
        "session_id": sid,
        "status": status,
        "pending_draft": draft,
        "category": st.get("category") or None,
        "context": st.get("context") or None,
        "draft": st.get("draft") or None,
        "messages": _render_messages(st.get("messages", [])),
    }


def _get_session(session_id: str, user: str) -> dict:
    """读取会话登记，并校验归属：session 必须属于当前登录用户（防 IDOR 越权）。"""
    s = SESSIONS.get(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    if s.get("username") != user:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    return s


def _has_pending_interrupt(session_id: str) -> bool:
    """检查图当前是否真的停在 human_approve 的 interrupt 上。
    防止重复 approve/reject（如并发双击）对已恢复的会话再次 resume 抛错。
    """
    snap = app.get_state(thread_cfg(session_id))
    return bool(snap and "__interrupt__" in (snap.next or []))


def _build_session_data(session_id: str, s: dict, st: dict) -> dict:
    """把会话登记 + 图状态组装成质检/补全模块需要的 session_data（统一结构）。"""
    return {
        "session_id": session_id,
        "messages": st.get("messages", []),
        "category": st.get("category", ""),
        "context": st.get("context", ""),
        "draft": st.get("draft", ""),
        "status": s["status"],
        "review_count": st.get("review_count", 0),
        "created_at": s.get("created_at"),
        "completed_at": s.get("completed_at"),
    }


# ===== FastAPI 应用 =====
api = FastAPI(title="售后智能客服多 Agent 系统", version="2.0")
# 中间件顺序：后 add 的先执行 → 请求先过 CORS（预检直接放行），再过 IP 限流
api.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
api.add_middleware(RateLimitMiddleware)


# ===== 前端页面（强制不缓存，根除"用户浏览器用旧 HTML"问题）=====
@api.get("/", include_in_schema=False)
def index():
    resp = FileResponse(STATIC_DIR / "index.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# /static 子目录走默认响应（被 GET / 的 no-store 头覆盖）
api.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ===== 鉴权端点 =====
@api.post("/api/auth/register")
def register(req: RegisterRequest):
    register_user(req.username, req.password)
    return {"message": "注册成功", "username": req.username.strip()}


@api.post("/api/auth/login")
def login(req: LoginRequest):
    username = authenticate(req.username, req.password)
    if username is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"access_token": create_token(username), "token_type": "bearer", "username": username}


@api.get("/api/auth/me")
def me(username: str = Depends(get_current_user)):
    return {"username": username}


# ===== 限流状态（供前端展示剩余配额）=====
@api.get("/api/rate-limit/status")
def rl_status(data: dict = Depends(rate_limit_status)):
    return data


# ===== 客服业务端点（全部需登录：Depends(user_rate_limit) 内含鉴权 + 用户限流）=====
@api.post("/api/sessions")
def create_session(req: StartRequest, user: str = Depends(user_rate_limit)):
    sid = str(uuid.uuid4())
    if len(SESSIONS) >= MAX_SESSIONS:
        # 容量保护：淘汰创建时间最早的会话（内存 + 磁盘同步删除）
        oldest = min(SESSIONS, key=lambda k: SESSIONS[k].get("created_at", ""))
        del SESSIONS[oldest]
        _sess_delete(oldest)
    SESSIONS[sid] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending_approval",
        "username": user,
    }
    _sess_sync(sid)   # 首次登记即落盘
    return _run(sid, new_session_input(req.message, username=user))


@api.post("/api/sessions/{session_id}/messages")
def send_message(session_id: str, req: MessageRequest, user: str = Depends(user_rate_limit)):
    s = _get_session(session_id, user)
    if s["status"] == "pending_approval":
        # 决策清单约定：有成稿待人工审核时，用户必须先批准/驳回，再继续提问（防状态机混乱）
        raise HTTPException(status_code=409,
                            detail="当前有回复待人工审核，请先批准发送或驳回重写后再继续提问")
    # 多轮续聊：只追加用户消息，不重置结构化字段（由 checkpointer 续跑）；username 同步注入
    return _run(session_id, {"messages": [req.message], "username": user})


@api.post("/api/sessions/{session_id}/approve")
def approve(session_id: str, user: str = Depends(user_rate_limit)):
    s = _get_session(session_id, user)
    if s["status"] == "completed":
        raise HTTPException(status_code=409, detail="session already completed")
    if not _has_pending_interrupt(session_id):
        raise HTTPException(status_code=409, detail="会话当前无待审核的成稿（可能已处理或仍在生成中）")
    # 批准：resume 给 human_approve 一个 approve 决策，图跑完并落盘 reply.md
    return _run(session_id, Command(resume={"action": "approve", "feedback": ""}))


@api.post("/api/sessions/{session_id}/reject")
def reject(session_id: str, req: RejectRequest, user: str = Depends(user_rate_limit)):
    s = _get_session(session_id, user)
    if s["status"] == "completed":
        raise HTTPException(status_code=409, detail="session already completed")
    if not _has_pending_interrupt(session_id):
        raise HTTPException(status_code=409, detail="会话当前无待审核的成稿（可能已处理或仍在生成中）")
    # 驳回：带修改意见退回 respond 重写（最多 AFTERSALE_MAX_REJECT 次后强制发送）
    return _run(session_id, Command(resume={"action": "reject", "feedback": req.feedback}))


@api.get("/api/sessions/{session_id}")
def get_session(session_id: str, user: str = Depends(user_rate_limit)):
    s = _get_session(session_id, user)
    st = _state(session_id)
    return {
        "session_id": session_id,
        "status": s["status"],
        "username": s.get("username"),
        "category": st.get("category") or None,
        "context": st.get("context") or None,
        "draft": st.get("draft") or None,
        "messages": _render_messages(st.get("messages", [])),
    }


# ===== 质检 + 知识库自补全 =====
@api.get("/api/quality/{session_id}")
def get_quality(session_id: str, user: str = Depends(user_rate_limit)):
    """对一次会话做质检：响应时间/检索命中/禁用词/驳回次数等，返回 quality_report。"""
    from quality_inspector import inspect_session
    s = _get_session(session_id, user)
    st = _state(session_id)
    return inspect_session(_build_session_data(session_id, s, st))


@api.post("/api/knowledge/suggest/{session_id}")
def suggest_faq(session_id: str, user: str = Depends(user_rate_limit)):
    """从一次已 approve 的会话里抽取新 FAQ 并追加到 客服FAQ.md（自动补全知识库）。
    只有质检通过+未命中现有 FAQ+成稿被人工 approve 才会真正追加。
    """
    from quality_inspector import quality_and_update
    s = _get_session(session_id, user)
    st = _state(session_id)
    report = quality_and_update(_build_session_data(session_id, s, st))
    return report


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api_server:api", host=HOST, port=PORT, reload=False)
