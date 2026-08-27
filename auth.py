# auth.py - 用户鉴权模块（SQLite 用户表 + pbkdf2 密码哈希 + JWT Token）
# ---------------------------------------------------------------------------
# 职责：
#   1) 用户注册 / 登录（users.db，SQLite，密码以 pbkdf2 加盐哈希存储，绝不明文）
#   2) 内置管理员 admin（首次初始化自动创建）
#   3) JWT 签发与校验（PyJWT，HS256，24h 有效期）
#   4) get_current_user 依赖：供受保护端点校验 Authorization: Bearer <token>
# 与 api_server.py 的关系：鉴权端点与业务端点的安全依赖都来自本模块。
# ---------------------------------------------------------------------------
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

# ===== 配置（环境变量可覆盖）=====
DB_PATH = Path(__file__).parent / "users.db"                 # 用户库（建议加入 .gitignore）
JWT_SECRET = os.getenv("AFTERSALE_JWT_SECRET", "dev-secret-change-me-in-production-please-32bytes")
JWT_ALGO = "HS256"
TOKEN_TTL_HOURS = 24                                         # Token 有效期
ADMIN_USER = "admin"
ADMIN_PASS = os.getenv("AFTERSALE_ADMIN_PASS", "admin123")   # 内置管理员初始密码
PBKDF2_ITERATIONS = 100_000                                  # 密码哈希迭代次数

_bearer = HTTPBearer(auto_error=False)  # 从请求头解析 Bearer token，缺失时不直接报错


# ===== 密码哈希（标准库实现，零第三方依赖）=====
def _hash_password(password: str, salt: Optional[bytes] = None) -> tuple[str, str]:
    """返回 (salt_hex, hash_hex)。每次注册生成随机盐，相同密码产生不同哈希。"""
    salt = salt or secrets.token_bytes(16)                    # 16 字节随机盐
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return salt.hex(), dk.hex()


def _verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    """校验密码：用库里的盐重算哈希，再与存储哈希常量时间比较。"""
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(dk.hex(), hash_hex)            # 防时序攻击


# ===== SQLite 用户表 =====
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row                            # 行按字段名访问
    return conn


def _init_db() -> None:
    """建表 + 首次运行创建内置 admin。"""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")
        row = conn.execute("SELECT 1 FROM users WHERE username=?", (ADMIN_USER,)).fetchone()
        if row is None:
            salt, h = _hash_password(ADMIN_PASS)
            conn.execute(
                "INSERT INTO users(username, salt, password_hash, created_at) VALUES (?,?,?,?)",
                (ADMIN_USER, salt, h, datetime.now(timezone.utc).isoformat()))
            print(f"[auth] 已创建内置管理员 {ADMIN_USER}（默认密码 {ADMIN_PASS}，请尽快修改）")


_init_db()


# ===== JWT 签发 / 校验 =====
def create_token(username: str) -> str:
    payload = {
        "sub": username,                                       # subject：用户标识
        "iat": int(time.time()),                               # 签发时间
        "exp": int(time.time()) + TOKEN_TTL_HOURS * 3600,      # 过期时间
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def decode_token(token: str) -> Optional[str]:
    """校验并解析 token，返回用户名；无效/过期返回 None。"""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        return payload.get("sub")
    except jwt.PyJWTError:
        return None


# ===== 注册 / 登录 =====
def register_user(username: str, password: str) -> None:
    username = username.strip()
    if not (3 <= len(username) <= 32):
        raise HTTPException(status_code=400, detail="用户名长度需 3~32 字符")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    salt, h = _hash_password(password)
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO users(username, salt, password_hash, created_at) VALUES (?,?,?,?)",
                (username, salt, h, datetime.now(timezone.utc).isoformat()))
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="用户名已存在")


def authenticate(username: str, password: str) -> Optional[str]:
    """校验账号密码，成功返回用户名，失败返回 None。"""
    with _conn() as conn:
        row = conn.execute(
            "SELECT username, salt, password_hash FROM users WHERE username=?",
            (username,)).fetchone()
    if row is None or not _verify_password(password, row["salt"], row["password_hash"]):
        return None
    return row["username"]


# ===== FastAPI 安全依赖 =====
def get_current_user(cred: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> str:
    """受保护端点注入本依赖：自动校验 Bearer token，返回用户名。

    流程：HTTPBearer 解析出 token → decode_token 校验 → 无效抛 401。
    """
    if cred is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="未登录：缺少 Authorization 头",
                            headers={"WWW-Authenticate": "Bearer"})
    username = decode_token(cred.credentials)
    if username is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Token 无效或已过期",
                            headers={"WWW-Authenticate": "Bearer"})
    return username


# ===== 请求体模型 =====
class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str
