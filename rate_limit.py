# rate_limit.py - 接口限流模块（IP 级中间件 + 用户级依赖，内存滑动窗口）
# ---------------------------------------------------------------------------
# 职责：
#   1) RateLimiter：线程安全的滑动窗口限流器（key -> deque 时间戳）
#   2) RateLimitMiddleware：全局中间件，按客户端 IP 限流（防单 IP 刷接口）
#   3) user_rate_limit 依赖：按登录用户限流（业务端点注入，防单用户滥用）
# 响应头：X-RateLimit-Limit / X-RateLimit-Remaining / Retry-After
# 实现零第三方依赖：collections.deque + threading.Lock 即可。
# ---------------------------------------------------------------------------
from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Callable

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from auth import get_current_user

# ===== 配置（环境变量可覆盖，便于压测/演示）=====
IP_LIMIT = int(os.getenv("AFTERSALE_IP_RATE_LIMIT", "120"))     # 每 IP / 每窗口
USER_LIMIT = int(os.getenv("AFTERSALE_USER_RATE_LIMIT", "30"))  # 每用户 / 每窗口
WINDOW = int(os.getenv("AFTERSALE_RATE_WINDOW", "60"))          # 窗口（秒）

# 不参与 IP 限流的路径（文档/健康检查/前端页，避免误伤）
SKIP_PATHS = {"/", "/docs", "/redoc", "/openapi.json", "/static"}


class RateLimiter:
    """滑动窗口限流器。

    对每个 key 维护一个时间戳队列 deque；窗口外的旧时间戳先弹出，
    再判断队内个数是否达到上限。所有操作加锁保证多线程安全
    （FastAPI 同步端点跑在线程池，多个请求可同时到达）。
    """

    # key 数超过该阈值时清理一次空队列，防止海量 IP 长期占用内存
    _MAX_KEYS = 10_000

    def __init__(self, limit: int, window: int):
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def _gc(self):
        """清理已无时间戳的 key（仅在 key 数量膨胀时调用，降低锁竞争）。"""
        if len(self._hits) > self._MAX_KEYS:
            for k in [k for k, q in self._hits.items() if not q]:
                del self._hits[k]

    def allow(self, key: str) -> tuple[bool, int, float]:
        """记录一次请求并判断是否放行。

        返回 (是否放行, 剩余次数, 还需等待秒数)。"""
        now = time.monotonic()          # 单调时钟，不受系统改时间影响
        with self._lock:                # 临界区：读改写队列
            q = self._hits[key]
            while q and now - q[0] >= self.window:   # 弹出窗口外的旧时间戳
                q.popleft()
            if len(q) >= self.limit:    # 已达上限 → 拒绝
                wait = self.window - (now - q[0])
                return False, 0, round(wait, 1)
            q.append(now)               # 放行并记录本次时间戳
            self._gc()
            return True, self.limit - len(q), 0.0

    def remaining(self, key: str) -> int:
        """查询 key 当前剩余配额（不消耗）。"""
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] >= self.window:
                q.popleft()
            return max(0, self.limit - len(q))


# 全局实例：IP 级 + 用户级 各一个限流桶
_ip_limiter = RateLimiter(IP_LIMIT, WINDOW)
_user_limiter = RateLimiter(USER_LIMIT, WINDOW)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """全局中间件：在路由处理之前按客户端 IP 限流。

    执行位置：Starlette 的中间件栈中，位于路由分发之前——
    即「请求 → 中间件(限流) → 路由 → 依赖(鉴权/用户限流) → 业务」的第一道闸。
    """

    async def dispatch(self, request: Request, call_next: Callable):
        path = request.url.path
        if path in SKIP_PATHS or path.startswith("/static"):
            return await call_next(request)   # 白名单：跳过限流
        key = request.client.host if request.client else "unknown"
        ok, remaining, wait = _ip_limiter.allow(key)

        if ok:
            response = await call_next(request)   # 放行 → 进入路由层
        else:
            response = JSONResponse({"detail": "请求过于频繁，请稍后再试"}, status_code=429)
            response.headers["Retry-After"] = str(int(wait) + 1)

        # 把限流信息写进响应头，前端可展示剩余配额
        response.headers["X-RateLimit-Limit"] = str(IP_LIMIT)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response


def user_rate_limit(request: Request, username: str = Depends(get_current_user)) -> str:
    """用户级限流依赖：业务端点注入，key = 用户名（与 IP 级叠加，双保险）。

    同时承担鉴权职责（内部依赖 get_current_user），未登录直接 401。
    """
    ok, remaining, wait = _user_limiter.allow(username)
    if not ok:
        raise HTTPException(status_code=429,
                            detail=f"请求过于频繁，请 {int(wait) + 1} 秒后重试",
                            headers={"Retry-After": str(int(wait) + 1)})
    request.state.rate_remaining = remaining
    return username


def rate_limit_status(username: str = Depends(get_current_user)) -> dict:
    """查询当前用户与 IP 级限流配额（供前端展示/演示）。"""
    return {
        "username": username,
        "ip_limit_per_window": IP_LIMIT,
        "user_limit_per_window": USER_LIMIT,
        "user_remaining": _user_limiter.remaining(username),
        "window_seconds": WINDOW,
    }
