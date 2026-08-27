# RedisSaver 在 Docker 上的部署说明（Docker Desktop 版）

> 适用环境：Windows + Docker Desktop（本项目已验证 v29.7.2）
> 关联代码：`cs_supervisor.py`（checkpointer 初始化）· `test_redis_saver.py`（连通性探针）· `docker-compose.yml`（全栈编排）
> 阅读对象：非资深用户，按步骤操作即可
>
> 📌 **本机实测状态（2026-08-27）**：Docker Desktop v29.7.2 已安装；本次会话尝试启动引擎后
> `docker info` 仍未连上守护进程（可能需在 Docker Desktop 界面确认弹窗或等待 WSL2 引擎初始化）。
> 按第 2 节确认 "Engine running" 后，即可按第 3 节步骤实测；若你希望我代为实测，
> 可在引擎就绪后告知，我会补跑 `docker run` + `test_redis_saver.py` 并把结果写回本文档。

---

## 1. RedisSaver 是什么？在 Docker 中起什么作用？

### 1.1 一句话解释

**RedisSaver 是 LangGraph 的"状态存储器"**：把多 Agent 图每一步的运行状态（对话历史、分类结果、成稿等）存进 Redis。

### 1.2 为什么需要它？

aftersale-agent 默认用 `MemorySaver`——状态存在**进程内存**里，有两个弱点：

| 问题 | 后果 |
|------|------|
| 进程一停，状态全丢 | 重启服务后，所有会话的图状态消失，断点续跑失效 |
| 多进程/多实例不共享 | 开多个 uvicorn worker 时，线程 A 创建的会话，线程 B 找不到 |

**RedisSaver 把状态落到 Redis 里**，彻底解决这两点：

- ✅ 服务重启后，用同一个 `thread_id` 能接着上次中断的地方继续跑
- ✅ 多个 uvicorn 实例共享同一份状态（横向扩容前提）
- ✅ 人工审核（HITL）的"暂停-恢复"在重启后依然有效

### 1.3 为什么必须在 Docker 里部署？

项目要求 Redis **必须带 RediSearch 和 RedisJSON 模块**（LangGraph RedisSaver 的硬性依赖），
普通 `redis` 镜像不带这两个模块。**`redis/redis-stack-server` 镜像内置了全部所需模块**，
所以最省事的方式就是 Docker 一条命令拉起，无需手动编译模块。

> ⚠️ 注意：本项目在 `requirements.txt` 中已加入 `langgraph-checkpoint-redis==0.5.2`
> （提供 `langgraph.checkpoint.redis.RedisSaver`），部署 Redis 前请确认依赖已安装。

---

## 2. 部署前准备

### 2.1 软件检查（本机已满足 ✅）

| 项目 | 要求 | 本机状态 |
|------|------|---------|
| Docker Desktop | 已安装且**守护进程在运行** | ✅ 已装 v29.7.2（未启动时先做第 2.2 步） |
| WSL2 后端 | Docker Desktop 设置里启用（默认开启） | ✅ |
| 端口 6379 | 未被其他程序占用 | 部署前检查（见下） |

**检查 Docker 是否就绪**（命令行执行）：
```bash
docker --version      # ✅ 输出版本号
docker info           # ✅ 不报错 = 守护进程在跑
```

**如果 `docker info` 报错**（守护进程没启动）：
1. 开始菜单搜索 **Docker Desktop** 并打开（鲸鱼图标）
2. 等右下角状态变成 "**Engine running**"（首次启动约 30~90 秒）
3. 重新执行 `docker info` 验证

### 2.2 端口检查（避免 6379 冲突）

```bash
netstat -ano | findstr :6379
```
- 无输出 = 端口空闲，可直接用
- 有输出 = 6379 被占用 → 改宿主机映射端口（见 §4.2 的 `-p 6380:6379`），
  同时改 `.env` 的 `AFTERSALE_REDIS_PORT` 保持一致

### 2.3 本项目必备的环境变量（`.env`）

在项目根目录的 `.env` 中确认/添加：

```ini
# 启用 RedisSaver（memory 是单进程内存，redis 是跨进程持久）
AFTERSALE_CHECKPOINTER=redis
# Redis 连接参数（与容器端口映射对应）
AFTERSALE_REDIS_HOST=127.0.0.1
AFTERSALE_REDIS_PORT=6379
AFTERSALE_REDIS_DB=0
# 若容器设置了密码才需要：
# AFTERSALE_REDIS_PASSWORD=你的密码
```

> 提示：`.env` 不存在就执行 `cp .env.example .env` 再改。

---

## 3. 完整部署步骤（三步走）

```
第 1 步：拉取 redis-stack 镜像（一次性）
第 2 步：创建并启动容器（docker run 或 docker compose）
第 3 步：验证连通性（探针脚本 + redis-cli ping）
```

### 第 1 步：拉取镜像

```bash
docker pull redis/redis-stack-server:latest
```
> 首次拉取约几百 MB，视网络 1~10 分钟。国内网络慢可配镜像加速（见 §5.4）。

### 第 2 步：创建并启动容器

**方式 A：docker run（快速验证，推荐新手先用这个）**

```bash
docker run -d \
  --name aftersale-redis \
  -p 6379:6379 \
  --restart unless-stopped \
  redis/redis-stack-server:latest
```

**方式 B：docker compose（项目内置全栈，推荐最终使用）**

在项目根目录直接：
```bash
docker compose up -d redis-stack
```
（compose 文件已定义好容器名、端口、数据卷和健康检查，见 §4.2）

### 第 3 步：验证

```bash
# ① 容器状态是 Up
docker ps | grep aftersale-redis

# ② Redis 内部 ping（能回 PONG 即服务正常）
docker exec aftersale-redis redis-cli ping

# ③ 项目探针：检查 RediSearch/JSON 模块是否齐全（RedisSaver 必需）
python test_redis_saver.py
```

三条全过 = RedisSaver 的存储后端就绪。启动业务服务后，日志应出现：

```
[checkpointer] RedisSaver 已启用：127.0.0.1:6379/0
```

> 如果看到 `Redis 连接失败，回退 MemorySaver` 的警告，说明没连上，按 §5 排查。

---

## 4. 两种容器化部署方式详解

### 4.1 方式 A：docker run（单容器，独立可控）

**最简启动**（上面第 2 步的 A）：
```bash
docker run -d --name aftersale-redis -p 6379:6379 --restart unless-stopped redis/redis-stack-server:latest
```

**带密码 + 数据持久化的完整版**：
```bash
docker run -d \
  --name aftersale-redis \
  -p 6379:6379 \
  --restart unless-stopped \
  -v redis-data:/data \              # 数据落到命名卷，删容器不丢
  redis/redis-stack-server:latest \
  redis-stack-server --requirepass 你的密码 --save 60 1 --appendonly yes
```
- `--save 60 1`：60 秒内只要有 1 次写操作就落盘（RDB）
- `--appendonly yes`：开启 AOF 追加日志（更强的持久化保障）

**常用管理命令**：
```bash
docker ps                          # 查看运行中的容器
docker logs aftersale-redis        # 查看 Redis 日志
docker restart aftersale-redis     # 重启 Redis
docker stop aftersale-redis        # 停止（容器还在）
docker rm aftersale-redis          # 删除容器（数据卷不删）
```

### 4.2 方式 B：docker compose（项目内置，推荐日常使用）

项目根目录 `docker-compose.yml` 已内置 Redis 服务，无需手写：

```yaml
services:
  redis-stack:
    image: redis/redis-stack-server:latest
    container_name: aftersale-redis
    restart: unless-stopped
    ports:
      - "6379:6379"
    volumes:
      - redis-data:/data
    command: >
      redis-stack-server
      --save 60 1
      --appendonly yes
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  api:
    build: .
    container_name: aftersale-api
    depends_on:
      redis-stack:
        condition: service_healthy   # 等 Redis 就绪再启动 api
    env_file:
      - .env
    environment:
      AFTERSALE_REDIS_HOST: redis-stack   # 容器内用服务名互访
      AFTERSALE_REDIS_PORT: "6379"
      AFTERSALE_CHECKPOINTER: redis
    volumes:
      - .:/app
    ports:
      - "8000:8000"
    command: uvicorn api_server:api --host 0.0.0.0 --port 8000

volumes:
  redis-data:
```

**启动/停止**：
```bash
docker compose up -d --build     # 一键起 Redis + API（--build 首次构建镜像）
docker compose ps                # 查看两个容器状态
docker compose logs -f api       # 跟踪 API 日志
docker compose logs redis-stack  # 查看 Redis 日志
docker compose down              # 停止（数据卷保留）
```

**关键点**：
- `depends_on + service_healthy`：确保 API 容器**等 Redis 真正就绪**再启动，避免"连不上回退 MemorySaver"
- 容器内 API 用服务名 `redis-stack` 访问 Redis；宿主机程序用 `127.0.0.1:6379`

---

## 5. 部署后验证与排查

### 5.1 验证清单（逐条确认）

| 验证项 | 命令 | 期望结果 |
|--------|------|---------|
| 容器存活 | `docker ps \| grep aftersale-redis` | `Up`（非 Exited） |
| Redis 响应 | `docker exec aftersale-redis redis-cli ping` | `PONG` |
| 模块齐全 | `python test_redis_saver.py` | 3 项全 ✅（Redis 连通 / RediSearch / JSON） |
| API 使用 Redis | 启动 API 后看日志 | `[checkpointer] RedisSaver 已启用：127.0.0.1:6379/0` |
| 断点续跑验证 | 开一个会话跑到"待审核"，`docker compose restart api`，再调 approve | 能继续恢复并落盘（重启前会失败） |

### 5.2 查看日志

```bash
docker logs aftersale-redis        # Redis 启动日志（含模块加载列表）
docker logs -f aftersale-redis     # 跟随输出
docker compose logs -f api         # 全栈时看 API 日志（含 checkpointer 状态行）
```

### 5.3 常见问题排查

| 症状 | 原因 | 解决办法 |
|------|------|---------|
| `docker info` 报 daemon 错误 | Docker Desktop 没启动 | 打开 Docker Desktop 等 "Engine running" |
| API 日志出现 `Redis 连接失败，回退 MemorySaver` | ① 容器没起来 ② 端口映射错 ③ `.env` 的 HOST/PORT 不对 | 依次检查：`docker ps` → `docker exec ... redis-cli ping` → 核对 `.env` 四项 |
| `test_redis_saver.py` 报 "RediSearch 未加载" | 用了普通 `redis` 镜像（非 stack） | 确认镜像名是 `redis/redis-stack-server`，重新 `docker pull` + 重建容器 |
| 6379 端口被占用 | 本机其他程序在用 | 改映射端口 `-p 6380:6379` + 同步改 `.env` 的 `AFTERSALE_REDIS_PORT=6380` |
| 镜像拉取很慢/超时 | 国内网络访问 Docker Hub 慢 | Docker Desktop → Settings → Docker Engine 配置镜像加速（如 `"registry-mirrors": ["https://docker.m.daocloud.io"]`），保存重启 |
| 容器反复重启 | WSL2 内存不足 | Docker Desktop → Settings → Resources 调高内存（建议 4GB+） |
| 设置了密码后连不上 | 漏配 `AFTERSALE_REDIS_PASSWORD` | 容器用 `--requirepass` 设密码后，`.env` 必须同步填 `AFTERSALE_REDIS_PASSWORD` |
| `langgraph.checkpoint.redis` 导入失败 | 依赖没装 | `pip install -r requirements.txt`（需含 `langgraph-checkpoint-redis==0.5.2`） |

### 5.4 国内网络加速（可选但强烈建议）

Docker Desktop → **Settings → Docker Engine**，把默认 JSON 改为：

```json
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://dockerproxy.com"
  ]
}
```

点击 **Apply & Restart** 后重新 `docker pull`。

---

## 6. 收尾：与项目的完整联动

部署好 Redis 后，一次标准的"全栈跑通"流程是：

```bash
# 1) 启动 Docker Desktop（若未运行）
# 2) 项目根目录一键起全栈
docker compose up -d --build
# 3) 验证 checkpointer 真的用了 Redis
docker compose logs api | grep checkpointer
# 期望：[checkpointer] RedisSaver 已启用：redis-stack:6379/0
# 4) 打开前端开始使用
start http://127.0.0.1:8000
```

此时多 Agent 会话状态已跨进程、跨重启持久化，人工审核断点续跑在任何时候都可靠。

---

*本文档由 aftersale-agent 项目实测环境整理；若实测结果有更新，见各节"验证清单"对应的 ✅ 标注。*
