# aftersale-agent 完整迁移方案（开箱即用版）

> 目标：把代码 + 环境一次性搬到另一台电脑，**clone 后一条命令直接进入开发状态**，无需手动装 Python / Redis / 依赖。
> 策略：**Docker 容器化**（环境随镜像走）+ **数据自动初始化**（代码天然幂等）+ **.env 托管密钥**（敏感信息不入库）。

---

## 1. 迁移方案总览

| 要素 | 迁移方式 | 目标电脑行为 |
|------|---------|-------------|
| **源代码** | Git 仓库（已 init）或整目录拷贝 | clone / 拷贝后即得全部代码 |
| **Python 依赖** | `Dockerfile` 里 `pip install -r requirements.txt` | 镜像构建时自动装，无需手动 pip |
| **Redis（RedisSaver 必需）** | `docker-compose.yml` 里 `redis-stack-server` 镜像 | `docker compose up` 自动拉镜像启动 |
| **运行环境（Python/包/系统库）** | Docker 镜像 | 无需安装任何开发环境 |
| **数据库** | 首次启动自动初始化（幂等） | 自动建 admin 用户 / 种子订单 / 向量索引 |
| **密钥（QWEN / JWT）** | `.env`（gitignore 不入库）+ 启动脚本自动生成 | 首次运行自动生成 `.env` + 随机 JWT_SECRET |
| **数据持久化** | Docker 命名卷 + 源码卷挂载 | 容器删了数据不丢 |

---

## 2. 目标电脑前置要求（唯一需要手动做的一步）

**安装 Docker Desktop**（约 5 分钟）：

1. 下载：https://www.docker.com/products/docker-desktop/
2. 安装后启动 Docker Desktop（Windows 会提示启用 WSL2，一路同意即可）
3. 验证：
   ```bash
   docker --version    # ✅ 输出版本号
   docker info         # ✅ 不报错（守护进程在跑）
   ```
4. ⚠️ **无需**安装 Python、Redis、MySQL 等任何其他软件——全部容器化

---

## 3. 迁移步骤（两条路任选）

### 路线 A：通过 Git 仓库（推荐，含版本历史）

**A1. 本机推送到 GitHub**（一次）：
```bash
cd aftersale-cs
git branch -M main
git remote add origin https://github.com/ainwuy/aftersale-agent.git  # 若未加
git push -u origin main   # 需 PAT（见下）
```

**A2. 目标电脑克隆**：
```bash
git clone https://github.com/ainwuy/aftersale-agent.git
cd aftersale-agent
./start-dev.sh          # Mac/Linux
# 或 Windows: start-dev.bat
```

### 路线 B：整目录拷贝（无 Git 也可）

把整个 `aftersale-cs/` 目录压缩（zip）→ 传到目标电脑 → 解压 → 运行启动脚本。

> ⚠️ 拷贝时**不要带**这些（会被 .gitignore 排除，且是运行产物）：
> `.env`（含密钥，需在目标机重新生成）、`logs/`、`reports/`、`*.db`、`faq_chromadb/`
> 数据会在目标机首次启动时**自动重建**（见 §5）。

---

## 4. 一键启动说明

```bash
# Mac / Linux / Git Bash
./start-dev.sh            # 首次自动建 .env + 随机密钥 + 起服务
./start-dev.sh rebuild    # 强制重建镜像（改了 Dockerfile 后）
./start-dev.sh stop       # 停止

# Windows（双击或命令行）
start-dev.bat
```

**脚本自动完成 4 件事**：
1. 检测 Docker 就绪（未装则明确报错）
2. `.env` 不存在 → 从 `.env.example` 复制 + **自动生成随机 JWT_SECRET**（48 字符）
3. 校验 `QWEN=` 是否已填（缺失只警告不阻塞——API 能起，但对话需 key）
4. `docker compose up -d --build` → 起 Redis + API → 等待健康检查 → 打印访问地址

**启动后**：
```
前端界面   : http://127.0.0.1:8000
默认账号   : admin / admin123
热重载     : 改源码自动生效（--reload，开发模式）
日志查看   : docker compose logs -f api
```

---

## 5. 数据自动初始化（无需手动迁移数据）

现有代码**天然幂等**，首次启动自动完成：

| 数据 | 初始化逻辑 | 位置 |
|------|-----------|------|
| **admin 用户** | `auth.py` 的 `_init_db()`：建表 + 首次运行创建 admin | 自动 |
| **订单种子** | `order_system.py` 的 `_init_db()`：空表时插入 5 个测试订单 | 自动 |
| **向量索引** | `cs_supervisor.py` 的 `_init_vector_store()`：索引空时从 客服FAQ.md 重建 | 自动 |
| **客服FAQ.md** | 已入库（源代码），无需迁移 | 随代码走 |
| **Redis 数据** | Docker 命名卷 `redis-data` 持久化 | 自动 |

**数据持久化设计**（docker-compose 关键）：
```yaml
volumes:
  - .:/app          # api 容器挂载源码 → users.db/orders.db/faq_chromadb 落在宿主机项目目录
  - redis-data:/data # Redis 的 RDB/AOF 持久化
```
→ 容器删除/重建后数据不丢；换电脑时若想**带历史数据**，把 `*.db` + `faq_chromadb/` 一起拷走即可（不拷则自动重建空库）。

---

## 6. 敏感信息与密钥管理（安全）

| 密钥 | 处理方式 | 是否入库 |
|------|---------|---------|
| **QWEN（DashScope LLM key）** | `.env` 手动填；`QWEN=` 留空则启动警告 | ❌ |
| **AFTERSALE_JWT_SECRET** | 启动脚本自动生成随机 48 字符 | ❌ |
| **AFTERSALE_ADMIN_PASS** | 默认 admin123，可改 `.env` | ❌ |
| **SILICONFLOW_API_KEY** | 需要时填 `.env`（embedding 备选后端） | ❌ |
| **Redis 密码** | 需要时填 `AFTERSALE_REDIS_PASSWORD` | ❌ |

**.env 生命周期**：
```
本机 .env ──(gitignore 不入库)──▶ 目标机首次启动时由脚本从 .env.example 生成
                                    ├── JWT_SECRET 自动随机化 ✅
                                    └── QWEN 需手动填入（唯一手动步骤）
```

> ⚠️ **QWEN key 不随代码迁移**（安全红线：密钥永不进 Git）。目标机首次启动后打开 `.env` 填入你的 DashScope key 即可，之后**再无任何手动配置**。

---

## 7. 常见问题

| 问题 | 处理 |
|------|------|
| `docker: command not found` | 未装 Docker Desktop，或未加到 PATH |
| `Cannot connect to the Docker daemon` | Docker Desktop 没启动，先启动它 |
| 启动后对话报 key 错误 | `.env` 里 `QWEN=` 为空，填入真实 DashScope key 后 `./start-dev.sh rebuild` |
| 改代码不生效 | 确认用的是 `--reload` 模式（默认），或 `./start-dev.sh rebuild` 重建镜像 |
| 端口 8000 被占用 | 改 `.env` 的 `AFTERSALE_PORT`（或 compose 里 ports 映射） |
| 想带历史订单/用户数据 | 拷贝 `*.db` 文件 + `faq_chromadb/` 目录到项目根目录再启动 |
| GitHub push 需要密码 | GitHub 禁用密码，用 PAT（settings → tokens → 勾 repo） |
| 内存不足 | Docker Desktop Settings → Resources 调高内存（建议 4GB+） |

---

## 8. 迁移验收清单（目标电脑跑通即算成功）

- [ ] `docker --version` ✅
- [ ] clone / 拷贝代码 ✅
- [ ] `./start-dev.sh` 或 `start-dev.bat` ✅（自动生成 .env）
- [ ] `.env` 填入 QWEN key ✅
- [ ] `http://127.0.0.1:8000` 打开前端 ✅
- [ ] `admin / admin123` 登录成功 ✅
- [ ] 发一条"我订单 CS20260820001 是什么状态？"得到回复 ✅
- [ ] 查看 `docker compose ps`：redis-stack + api 都是 Up ✅
- [ ] 改一行 `static/index.html` 刷新页面看到变化（热重载）✅

全部 ✅ = **迁移完成，目标电脑进入开箱即用开发状态** 🎉
