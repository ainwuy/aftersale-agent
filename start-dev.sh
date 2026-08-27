#!/usr/bin/env bash
# ============================================================
# aftersale-agent 一键启动（macOS / Linux / Git Bash on Windows）
# 用法：
#   ./start-dev.sh          首次：自动建 .env、起服务
#   ./start-dev.sh rebuild  强制重建镜像
#   ./start-dev.sh stop     停止服务
#
# 职责（开箱即用三件事）：
#   1. .env 不存在 → 从 .env.example 复制，并自动生成随机 JWT_SECRET
#   2. 校验 QWEN key 是否已填（缺失则提示，不阻塞启动——向量检索会回退轻量模式）
#   3. docker compose up -d --build 一键起 Redis + API（含热重载）
# ============================================================
set -e
cd "$(dirname "$0")"     # 切到项目目录（脚本所在位置）

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $1${NC}"; }
err()  { echo -e "${RED}❌ $1${NC}"; }

# ---------- 0. 检查 Docker ----------
if ! command -v docker >/dev/null 2>&1; then
  err "未检测到 Docker。请先安装 Docker Desktop：https://www.docker.com/products/docker-desktop/"
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  err "Docker 守护进程未运行。请先启动 Docker Desktop 再重试。"
  exit 1
fi
ok "Docker 就绪"

# ---------- 1. 准备 .env ----------
if [ ! -f .env ]; then
  echo "首次运行：正在生成 .env ..."
  if [ -f .env.example ]; then cp .env.example .env; fi
  # 生成随机 JWT 密钥（32+ 字节，避免 PyJWT 警告）
  SECRET=$(head -c 48 /dev/urandom | base64 | tr -d '/+=' | head -c 48)
  if grep -q "^AFTERSALE_JWT_SECRET=dev-secret" .env 2>/dev/null; then
    sed -i.bak "s/^AFTERSALE_JWT_SECRET=.*/AFTERSALE_JWT_SECRET=$SECRET/" .env && rm -f .env.bak
  else
    echo "AFTERSALE_JWT_SECRET=$SECRET" >> .env
  fi
  ok ".env 已生成（JWT_SECRET 已随机化）"
else
  ok ".env 已存在"
fi

# ---------- 2. 校验 QWEN key（LLM + embedding 必需）----------
if ! grep -qE "^QWEN=.+" .env 2>/dev/null; then
  warn "QWEN key 未配置。LLM 会不可用，请编辑 .env 填入："
  warn "  QWEN=你的DashScope_API_Key"
  warn "  （无 key 时系统可启动但回答依赖 API，将报错）"
fi

# ---------- 3. 起服务 ----------
case "${1:-up}" in
  stop)
    docker compose down
    ok "服务已停止"
    ;;
  rebuild)
    docker compose up -d --build
    ;;
  *)
    docker compose up -d --build
    ;;
esac

# ---------- 4. 等待就绪 ----------
echo -n "等待 API 就绪"
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8000/ >/dev/null 2>&1; then
    echo ""; ok "服务已就绪"
    break
  fi
  echo -n "."; sleep 2
  if [ "$i" = "30" ]; then
    echo ""; warn "服务未在 60s 内就绪，请查看日志：docker compose logs -f api"
  fi
done

echo ""
echo "=============================================="
echo "  📍 前端界面   : http://127.0.0.1:8000"
echo "  🔑 默认账号   : admin / admin123"
echo "  🔄 热重载     : 修改源码自动生效（--reload）"
echo "  日志查看      : docker compose logs -f api"
echo "  停止服务      : ./start-dev.sh stop"
echo "=============================================="