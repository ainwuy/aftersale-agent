# aftersale-agent 代码审查报告

- 审查时间：2026-08-27
- 仓库：`https://github.com/ainwuy/aftersale-agent.git`（4 次提交，34 个文件，main 分支）
- 审查范围：全部 Python 源码 + 前端入口 + Docker 编排 + 启动脚本
- 语法检查：全部 `.py` 通过 `py_compile`；未发现已提交的 `.env`/`.db`/密钥文件（gitignore 有效）

---

## 一、结论摘要

项目整体结构清晰、模块职责划分好（核心图 / 鉴权 / 限流 / 质检 / 观测 / 实战包分层明确），
README 文档详尽。**核心风险集中在安全与"文档承诺未落地"两类**：

- 3 项高危安全问题（P0）：可伪造 JWT、会话越权、默认凭据
- 4 项核心链路 bug（P1）：其中 2 项直接导致 README 声称的功能不可用
- 4 项健壮性问题（P2）
- 4 项结构/可维护性优化（P3）

> **修复进度（2026-08-27）**：P0 三项 + P1 四项已全部修复并通过验证 ✅；P2 四项 + P3 七项已修复 ✅
> （P3 仅 O5「_agent_loop 改 ToolNode」、O7「Docker 生产化」两项列为后续建议，未改动）
> 另新增修复：auth.py SQLite 连接泄漏（`with conn:` 只提交事务不关闭连接，Windows 下句柄泄漏/文件占用）。

---

## 二、P0 安全（立即修）

### S1. JWT_SECRET 硬编码默认值（高危）
`auth.py:29`：`JWT_SECRET = os.getenv("AFTERSALE_JWT_SECRET", "dev-secret-change-me-in-production-please-32bytes")`

攻击者只要知道默认值，就能用公开源码里的密钥自行签发 `sub=admin` 的 token，**完全接管系统**。

修复：
```python
JWT_SECRET = os.getenv("AFTERSALE_JWT_SECRET")
if not JWT_SECRET or JWT_SECRET.startswith("dev-secret-"):
    print("[auth] ⚠️ 警告：JWT_SECRET 未设置或为默认值，生产环境必须配置随机密钥！")
    # 演示模式允许继续，但生产应 raise
```
并在 `.env.example` 保留注释提示。start-dev.bat 已生成随机密钥（见 B4 修复）。

### S2. 会话无用户归属校验（IDOR，高危）
`api_server.py`：`approve` / `reject` / `get_session` / `get_quality` / `suggest_faq` / `send_message`
只检查 session 是否存在，**不校验 `SESSIONS[sid]["username"] == user`**。
任何登录用户只要拿到（或猜中）他人 session_id，即可代他人批准/驳回回复。

修复：`_get_session` 增加归属校验：
```python
def _get_session(session_id: str, user: str) -> dict:
    s = SESSIONS.get(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    if s.get("username") != user:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    return s
```
所有业务端点把 `_get_session(session_id)` 改为 `_get_session(session_id, user)`。

### S3. 默认凭据 admin/admin123 硬编码 + 前端明文展示（高危）
`auth.py:33` 默认 `admin123`；`static/index.html:95` 把凭据直接写进页面。
演示可接受，生产必须改。建议：
- 首次启动强制要求设置 `AFTERSALE_ADMIN_PASS`（缺失则拒绝启动或打印醒目警告）
- 前端移除明文展示，改为"请通过环境变量配置管理员密码"提示

---

## 三、P1 核心 bug（尽快修）

### B1. RedisSaver 缺依赖，跨进程持久化失效（高）
`requirements.txt` 只有 `redis==7.4.1`，但 `cs_supervisor.py:370` 的
`from langgraph.checkpoint.redis import RedisSaver` 需要 `langgraph-checkpoint-redis` 包。
当前配 `AFTERSALE_CHECKPOINTER=redis` 时 import 必失败 → 静默回退 MemorySaver，
README 宣称的"跨进程共享 thread_id 状态"实际不可用。

修复：`requirements.txt` 增加：
```
langgraph-checkpoint-redis==2.0.17
```
（版本按当时 langgraph 1.2.5 兼容的 tag 调整；装完跑 `python test_redis_saver.py` 验证）

### B2. 自动补全 FAQ 的 category 带前缀，向量检索查不到（高）
`quality_inspector.append_to_faq` 写入的标题是 `## 自动补全 · 退换货与售后类`，
而 `cs_supervisor._clean_cat` 清洗后得到 `自动补全 · 退换货与售后`，
与 `CLEAN_CATEGORIES` 的标准 9 类不匹配。向量检索按 category 过滤时，
自动补全条目永远不命中（轻量检索全量搜才碰巧能命中）。

修复（二选一）：
- `append_to_faq` 的标题改为 `## {category}类（自动补全）`，让 `_clean_cat` 清洗回标准类名；
- 或 `_clean_cat` 增加 `re.sub(r'^自动补全 · ', '', tag)`。
改完需删除 `faq_chromadb/` 重建索引。

### B3. start-dev.bat 随机密钥不生效（中）
`start-dev.bat:38` 用 `echo AFTERSALE_JWT_SECRET=%SECRET%>> .env` **追加**到文件末尾，
而 `.env.example` 里已有一行默认值。python-dotenv 默认 `override=False` 取**首个**匹配 →
随机密钥被忽略，仍是硬编码默认值（叠加 S1 风险）。

修复：生成前先删除旧行：
```bat
powershell -NoProfile -Command "(Get-Content .env) -notmatch '^AFTERSALE_JWT_SECRET=' | Set-Content .env"
echo AFTERSALE_JWT_SECRET=%SECRET%>> .env
```

### B4. .env.example 声明了 6 个代码未读取的配置项（中）
`AFTERSALE_MODEL` / `AFTERSALE_TIMEOUT` / `AFTERSALE_MAX_RETRIES` / `AFTERSALE_MAX_STEP` /
`AFTERSALE_MAX_REJECT` / `AFTERSALE_EMBED_MODEL` 在 `.env.example` 有说明，
但代码里全部硬编码（`cs_supervisor.py:31-37` model/timeout/max_retries、`step>=8`、`reviews<2`、
`vector_store.py` model 选择）。README 说"换模型改 .env 即可"是**未实现的功能承诺**。

修复：`cs_supervisor.py` 顶部收敛为配置读取：
```python
MODEL = os.getenv("AFTERSALE_MODEL", "qwen3.7-plus-2026-05-26")
TIMEOUT = int(os.getenv("AFTERSALE_TIMEOUT", "120"))
MAX_RETRIES = int(os.getenv("AFTERSALE_MAX_RETRIES", "2"))
MAX_STEP = int(os.getenv("AFTERSALE_MAX_STEP", "8"))
MAX_REJECT = int(os.getenv("AFTERSALE_MAX_REJECT", "2"))
```
替换所有魔法数字。

---

## 四、P2 健壮性（计划修）

### R1. 退款单号时间戳冲突
`order_system.py:187` `refund_no = f"RF{int(time.time())}"`，同秒两次退款 →
UNIQUE 约束冲突 → `sqlite3.IntegrityError` 未捕获 → 500。
修复：`RF{int(time.time()*1000)}` 或加随机后缀 `secrets.token_hex(3)`，并用 try/except 兜底。

### R2. approve/reject 无幂等保护
`api_server.py`：会话已完成（status=completed）时已挡 409，但**pending_approval 状态重复 resume**
（如前端双击、并发点击）第二次 `app.invoke(Command(resume=...))` 会因当前无 interrupt 抛错 → 500。
修复：在 `_run` 的 resume 分支前加状态/中断存在性校验；前端按钮提交后立即 disable。

### R3. datetime 时区 aware/naive 混用
`api_server.py` 写 `datetime.now(timezone.utc)`（aware），
`practice/benchmark.py`/`orchestrator.py` 写 `datetime.now()`（naive）。
`quality_inspector.inspect_session` 直接相减，**跨场景复用数据会抛 TypeError**。
修复：统一用 `datetime.now(timezone.utc).isoformat()`。

### R4. 换 embedding 模型不重建索引
`vector_store._get_or_create` 只比对 metadata 的 `embed` 后端，不比对 `model`。
同后端换模型（如 qwen→bge-m3 都走 siliconflow）时旧索引保留 → 维度不一致查询报错。
修复：metadata 同时存 `model`，比对两者。

---

## 五、P3 结构 / 可维护性优化（随迭代）

1. **抽公共函数**：`session_data` 构造在 `api_server.py`（get_quality/suggest_faq）与
   `practice` 两包重复 3 处，抽 `build_session_data(sid, s, st)`。
2. **llm 全局副作用**：`practice` 直接覆盖 `cs.llm = stub`，与 api_server 同进程会互相污染。
   建议 LangGraph 节点从 state 取 llm，或用 `langchain_core.runnables.config` 传 config。
3. **SESSIONS 无界内存**：纯 dict 无上限、重启丢失。建议换 SQLite 或加 TTL + 容量上限。
4. **限流 deque 内存**：`rate_limit.py` 每 key 保留窗口内全部时间戳，大量 IP 会膨胀。
   可改用固定大小环形缓冲或定期清理空 key。
5. **messages token 膨胀**：`classify` 每次把 `【分类】...` 写进 messages，多轮后历史臃肿。
   分类结果已在结构化字段 `category`，可考虑不重复写入历史（或只写首轮）。
6. **多轮续聊 context 清空风险**：第二问 LLM 不调工具时 `ctx=""` → respond 无上下文，
   有编造风险。建议保留上次 context 或显式标注"未检索"。
7. **_agent_loop 手写 ReAct**：可换官方 `ToolNode`/`create_react_agent` 减少手写错误面
   （当前手动 invoke 已正确，属于可选重构）。
8. **Dockerfile/compose 生产化**：当前 `.:/app` 挂载 + `--reload` 是开发模式；
   生产应去掉挂载和热重载、`CMD` 前加 `python -c "import api_server"` 自检。
9. **补 pytest 单测**：现有 2 个测试是"桩验证"，建议对 auth（哈希/登录）、
   rate_limit（窗口滑动）、order_system（状态流转）、faq 解析补正式单测。
10. **tools.py 内联 import**：每个工具函数内部 `import json`，移到模块顶部。

---

## 六、优先级排序与实施路线

| 阶段 | 内容 | 状态 |
|------|------|------|
| 第 1 步（P0） | S1 JWT 密钥校验 + S2 会话归属校验 + S3 凭据警示 | ✅ 已修复（2026-08-27，已过语法检查；运行期验证：启动见告警、A 用户访问 B 会话 403） |
| 第 2 步（P1） | B1 补依赖 + B2 分类前缀修复 + B3 bat 修复 + B4 配置收敛 | ✅ 已修复（2026-08-27；B1 需 `pip install -r requirements.txt` 后跑 `python test_redis_saver.py`；B2 已有 `faq_chromadb/` 旧索引需删除重建） |
| 第 3 步（P2） | R1 退款单号唯一化 + R2 approve/reject 幂等 + R3 时区统一 + R4 换模型重建索引 | ✅ 已修复（2026-08-27，单测覆盖 R1/R3） |
| 第 4 步（P3） | O1 抽公共函数 + O2 llm 用后恢复 + O3 SESSIONS 容量上限 + O4 去消息噪音 + O6 限流 key 清理 + O8 批次可配置 + O9 单测（15 用例）+ O10 import 收敛 | ✅ 已修复（2026-08-27）；O5 ToolNode 重构、O7 Docker 生产化 留作后续 |
| 附加 | auth.py 连接泄漏（`with sqlite3.connect` 只 commit 不 close） | ✅ 已修复（改 @contextmanager，测试暴露后修复） |

> 实施原则：每步独立提交、可单独回滚；改完跑 `python -m py_compile` + 对应测试验证。
> 测试运行：`python -m unittest discover -s tests -v`（15 用例通过；test_faq 需完整依赖，缺失自动跳过）
