# aftersale-agent · 售后智能客服多 Agent 系统

基于 **LangGraph（Supervisor 多 Agent 编排）** 的售后客服中枢：先**分类** → 再**检索**企业 FAQ 标准答案 → 再**生成成稿** → 最后经**人工确认**才对外发送。已集成 **JWT 用户鉴权 + 双层接口限流（IP/用户）** 与 **前端交互 UI**。

> 配套文档：[业务需求分析报告.md](./业务需求分析报告.md) · [鉴权与限流实现说明.md](./鉴权与限流实现说明.md)

---

## 1. 架构总览

```
                   ┌──────────────────────────────┐
   用户提问 ───────▶│   Supervisor（主管·决策清单） │
                   └──────────┬───────────────────┘
        ┌────────────┬────────┴────────┬────────────┐
        ▼            ▼                 ▼            ▼
   classify      retrieve          respond     human_approve
   意图分类      知识检索(FAQ)      应答生成      人工审核(HITL)
        │            │                 │            │
        └────────────┴────────┬────────┴────────────┘
                    回主管，直到成稿通过 → END（落盘 reply.md）
```

- **核心引擎**：`cs_supervisor.py`（LangGraph `StateGraph` + `MemorySaver/RedisSaver` 断点续跑 + `interrupt` 人工介入）
- **业务系统**：`order_system.py`（SQLite 模拟订单/物流/退款，5 个种子订单覆盖主流状态）
- **业务工具**：`tools.py`（@tool 包装 lookup_order/list_user_orders/track_logistics/request_return/request_refund）
- **质检与知识库自补全**：`quality_inspector.py`（响应时间/检索命中/禁用词/驳回次数评分 + 未命中 FAQ 自动追加到 客服FAQ.md）
- **知识库**：`客服FAQ.md`（Markdown，启动时解析为问答对）
- **检索**：`lookup_faq` 优先走 **ChromaDB 向量检索**（默认复用 QWEN key 的百炼 qwen3.7-text-embedding，可切 bge-m3/本地），不可用时自动回退轻量关键词检索
- **服务层**：`api_server.py`（FastAPI）＝ **鉴权（auth.py）+ 限流（rate_limit.py）+ 多 Agent 业务 + 质检/补全端点 + 前端静态页**
- **前端**：`static/index.html`（飞书风格聊天 UI + 登录/注册 + 人工审核 + 限流配额）
- **会话持久化**：默认 `MemorySaver`（单进程），配 `AFTERSALE_CHECKPOINTER=redis` + Redis Stack → 跨进程共享 thread_id 状态

---

## 2. 目录结构

```
aftersale-cs/
├── cs_supervisor.py        # 核心：多 Agent 图（分类/检索/回答/人工审核）+ 本地 CLI
├── vector_store.py         # 向量检索：ChromaDB 客户端 + embedding（dashscope/bge-m3/local）
├── order_system.py         # 订单+物流+退款 系统（SQLite，含种子数据）
├── tools.py                # 业务工具集：@tool 包装 订单/物流/退款操作
├── quality_inspector.py    # 质检规则 + 知识库自动补全
├── auth.py                 # 鉴权：SQLite 用户表 + pbkdf2 密码哈希 + JWT + get_current_user
├── rate_limit.py           # 限流：滑动窗口 + IP 级中间件 + 用户级依赖
├── api_server.py           # FastAPI 服务层（鉴权/限流/业务端点/质检/知识库补全/前端挂载）
├── static/index.html       # 飞书风格前端 UI
├── 客服FAQ.md              # 知识库（按 ## 大类 + **Q：/A：** 维护）
├── 业务需求分析报告.md      # 端到端业务需求分析
├── 鉴权与限流实现说明.md    # 请求执行链路逐行剖析
├── test_redis_saver.py     # RedisSaver 连通性探针（host port db password 参数）
├── test_business_tools.py  # 业务工具闭环自测
├── requirements.txt        # 依赖清单
├── .env.example            # 环境变量模板（复制为 .env 填 Key）
├── .gitignore
├── faq_chromadb/           # 向量索引持久化目录（运行生成，已 gitignore）
├── users.db                # 用户库（运行生成，已 gitignore）
└── reply.md                # 人工批准后生成的成稿（运行产物，已 gitignore）
```

---

## 3. 环境准备

```bash
# 1) 用项目 venv（已含 langgraph / fastapi 等）
E:\python\code\.venv\Scripts\Activate.ps1

# 2) 安装依赖（首次或增删依赖时）
pip install -r requirements.txt

# 3) 配置密钥：复制模板并填入 Key
cp .env.example .env
#   编辑 .env：
#     QWEN=你的DashScope_API_Key        # 对话/路由/向量检索 三用（必需，默认向量后端就是 dashscope）
#     # 若想用 bge-m3 而非 qwen3.7-text-embedding：AFTERSALE_EMBED_BACKEND=siliconflow 并填 SILICONFLOW_API_KEY
#   若本项目无 .env，cs_supervisor.py 会自动复用 E:/python/code/supervisor_demo/.env（仅补 QWEN）
```

---

## 4. 运行方式

### 4.1 本地命令行（交互式人工审核）

```bash
python cs_supervisor.py
```
终端会打印每轮主管决策 / 工具调用 / 成稿预览，并在人工审核处让你输入 `approve` / `reject`。

### 4.2 Web 服务（REST API + 前端 UI）

```bash
uvicorn api_server:api --host 0.0.0.0 --port 8000
# 或： python api_server.py
```

**① 前端 UI（推荐）**：浏览器打开 **http://127.0.0.1:8000** → 登录（内置 `admin` / `admin123`，或注册新账号）→ 输入售后问题 → 消息流实时展示分类/检索/成稿 → 待审核卡片上点"批准发送"或"驳回重写"。顶栏实时显示限流剩余配额。

**② Swagger 接口文档**：**http://127.0.0.1:8000/docs**（业务端点需先登录拿 token）。

**③ curl 流程（需先登录拿 token）**：

```bash
# 0) 登录拿 token
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# 1) 创建会话并发送首个问题（未带 token 会 401）
SID=$(curl -s -X POST http://127.0.0.1:8000/api/sessions \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
  -d '{"message":"我买的鞋子尺码不合适想退货，怎么操作？"}' | python -c "import sys,json;print(json.load(sys.stdin)['session_id'])")

# 2) 查看状态（status=pending_approval，含 pending_draft）
curl -s http://127.0.0.1:8000/api/sessions/$SID -H "Authorization: Bearer $TOKEN" | python -m json.tool

# 3) 人工批准发送（落盘 reply.md，status=completed）
curl -s -X POST http://127.0.0.1:8000/api/sessions/$SID/approve \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool

# 4) 限流：连续超频调用会返回 429 + Retry-After（默认每用户 30 次/分钟）
```

### 4.3 启用真向量检索（ChromaDB + 多 embedding 后端）

启动时会自动尝试构建/加载向量索引，**默认 `dashscope` 后端直接复用你的 QWEN key，无需注册任何新平台**：

```bash
# .env 后端三选一：
AFTERSALE_EMBED_BACKEND=dashscope     # 默认：百炼 qwen3.7-text-embedding，复用 QWEN key（免费额度 100 万 token）
# AFTERSALE_EMBED_BACKEND=siliconflow  # SiliconFlow BAAI/bge-m3（需 SILICONFLOW_API_KEY）
# AFTERSALE_EMBED_BACKEND=local        # 本地加载模型（免 key，需 pip install sentence-transformers）
```

- 首次启动：解析 `客服FAQ.md` 33 条 → embedding 转向量 → 写入 `faq_chromadb/`（cosine 距离 + category 元数据）。
- 之后启动：直接加载已有索引，秒级就绪。
- 强制重建索引：删除 `faq_chromadb/` 目录或改 `AFTERSALE_EMBED_BACKEND` 后重启（后端变更会自动重建）。
- 无 key / 调用失败时自动回退轻量检索，日志会打印 `[向量检索] ⚠️ ...`。

---

## 5. 关键规则（与需求报告一致）

- **决策清单防跳步**：主管严格按 分类→检索→回答→人工→FINISH 派单。
- **防死循环**：`step` 达到 8 轮强制 FINISH。
- **人工护栏**：成稿未批准绝不落盘/发送；驳回最多 2 次后强制发送。
- **零编造**：检索未命中时显式提示，成稿据实标注。

---

## 6. 扩展指南

| 想做什么 | 改哪里 |
|----------|--------|
| 新增业务线 | `CLEAN_CATEGORIES` 加一项 + `客服FAQ.md` 加一节，**零代码改动**（重启自动增量/重建索引） |
| 换向量检索后端 | `.env` 的 `AFTERSALE_EMBED_BACKEND`（siliconflow / local / off）；向量逻辑集中在 `vector_store.py` |
| 跨进程持久 | `MemorySaver` → `RedisSaver`（一行切换） |
| 换模型/供应商 | 改 `cs_supervisor.py` 顶部 LLM 配置或 `.env` 的 `AFTERSALE_MODEL` |
| 接前端 UI | 直接用 `/docs` 联调，或在前端按上面的 REST 流程调用（已开 CORS） |

---

## 7. 注意事项

- `.env` 含密钥，**已加入 `.gitignore`，切勿提交**。
- LLM 调用受网络与配额影响，已设 `timeout=120`、`max_retries=2` 兜底。
- `reply.md` 为运行产物，落盘路径限定项目目录。
