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
import uuid
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

# 会话登记表（记录元数据 + 当前状态 + 所属用户；图状态本身在 MemorySaver 里按 session_id 存）
SESSIONS: dict[str, dict[str, Any]] = {}


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


def _get_session(session_id: str) -> dict:
    if session_id not in SESSIONS:
        raise HTTPException(status_code=404, detail="session not found")
    return SESSIONS[session_id]


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
    SESSIONS[sid] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending_approval",
        "username": user,
    }
    return _run(sid, new_session_input(req.message))


@api.post("/api/sessions/{session_id}/messages")
def send_message(session_id: str, req: MessageRequest, user: str = Depends(user_rate_limit)):
    _get_session(session_id)
    # 多轮续聊：只追加用户消息，不重置结构化字段（由 checkpointer 续跑）
    return _run(session_id, {"messages": [req.message]})


@api.post("/api/sessions/{session_id}/approve")
def approve(session_id: str, user: str = Depends(user_rate_limit)):
    s = _get_session(session_id)
    if s["status"] == "completed":
        raise HTTPException(status_code=409, detail="session already completed")
    # 批准：resume 给 human_approve 一个 approve 决策，图跑完并落盘 reply.md
    return _run(session_id, Command(resume={"action": "approve", "feedback": ""}))


@api.post("/api/sessions/{session_id}/reject")
def reject(session_id: str, req: RejectRequest, user: str = Depends(user_rate_limit)):
    s = _get_session(session_id)
    if s["status"] == "completed":
        raise HTTPException(status_code=409, detail="session already completed")
    # 驳回：带修改意见退回 respond 重写（最多 AFTERSALE_MAX_REJECT 次后强制发送）
    return _run(session_id, Command(resume={"action": "reject", "feedback": req.feedback}))


@api.get("/api/sessions/{session_id}")
def get_session(session_id: str, user: str = Depends(user_rate_limit)):
    s = _get_session(session_id)
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
    s = _get_session(session_id)
    st = _state(session_id)
    session_data = {
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
    return inspect_session(session_data)


@api.post("/api/knowledge/suggest/{session_id}")
def suggest_faq(session_id: str, user: str = Depends(user_rate_limit)):
    """从一次已 approve 的会话里抽取新 FAQ 并追加到 客服FAQ.md（自动补全知识库）。
    只有质检通过+未命中现有 FAQ+成稿被人工 approve 才会真正追加。
    """
    from quality_inspector import quality_and_update
    s = _get_session(session_id)
    st = _state(session_id)
    session_data = {
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
    report = quality_and_update(session_data)
    return report


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api_server:api", host=HOST, port=PORT, reload=False)
