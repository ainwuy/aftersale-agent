# 模块 10 + 11 说明文档：观测评估与综合实战

> 日期：2026-08-27
> 状态：✅ 代码完成，全链路测试通过（桩 LLM 无需 API key）

## 1. 两个模块的定位

- **模块 10（观测评估）** = 给 aftersale-agent 装"仪表盘"：采集指标、记录日志、评估会话质量、生成报告
- **模块 11（综合实战）** = 把模块 1~11 所有能力串成一个可演示的闭环，跑测试集 + 模拟真实用户会话

**衔接关系**：模块 11（practice）每跑一个会话，就调模块 10（monitoring）打点 + 评估 + 出报告。**观测不是事后看日志，而是实战中实时采集**。

## 2. 目录结构

```
aftersale-cs/
├── monitoring/                  # ── 模块 10：观测评估包 ──
│   ├── __init__.py
│   ├── metrics.py               # 指标采集器：counter/timing/gauge + p50/p95/p99
│   ├── logger.py                # 结构化 JSON 日志（文件+控制台，一行一个 JSON）
│   ├── evaluator.py             # 会话评估：规则质检 + LLM 三维度评分（可回退）
│   └── reporter.py              # 报告生成：JSON / Markdown / HTML
├── practice/                    # ── 模块 11：综合实战包 ──
│   ├── __init__.py
│   ├── benchmark.py             # 测试集批量跑：逐条跑图 + 打点 + 评估
│   ├── orchestrator.py          # 端到端编排：3 个模拟会话（含 reject 分支）
│   └── run_demo.py              # 一键入口：python -m practice.run_demo
└── reports/                     # 生成的报告（JSON/MD/HTML）
```

## 3. 模块 10 核心设计（monitoring）

### 3.1 metrics.py —— 指标采集器

**三个打点方法**：
- `counter(name, delta)`：累计计数（请求数、错误数、token 数）
- `timing(name, seconds)`：记录耗时，自动维护分布
- `gauge(name, value)`：瞬时值（当前连接数）

**核心输出**：
- `snapshot()`：全部指标快照
- `throughput()`：派生指标（QPS、错误率、p50/p95/p99）

设计要点：线程安全（Lock）+ 零第三方依赖。生产可换 Prometheus client，接口对齐改动小。

### 3.2 logger.py —— 结构化 JSON 日志

一行一个 JSON（机器可读，可被 ELK/Loki 采集）+ 人类可读控制台双输出。用法：

```python
log = setup_logger("aftersale")          # 自动建 logs/ 目录
ev = event_logger(log)
ev("session_start", session_id="s1", category="退货")   # → JSON 行
```

### 3.3 evaluator.py —— 会话质量评估

**两层评估**：
1. **规则层（零成本）**：复用 `quality_inspector.inspect_session`（响应时间/检索命中/禁用词/驳回次数）
2. **LLM 层（可选）**：对成稿打三维度分 相关性/忠实度/帮助度（0~10）
   - 有 key → LLM 真实打分，综合分 = 规则 80% + LLM 20%
   - 无 key / 失败 → 回退为中性占位（不崩）

### 3.4 reporter.py —— 报告生成

`build()` 组装 → `save()` 输出 JSON/Markdown/HTML 三格式。HTML 双击浏览器可看，适合演示。

## 4. 模块 11 核心设计（practice）

### 4.1 benchmark.py —— 测试集批量跑

- `DEFAULT_CASES`：5 条与 FAQ 主题对齐的测试用例（可扩展）
- `_run_one()`：跑一条用例 → 自动 approve → 打点 → 评估 → 返回结果
- `run_all()`：逐条跑 + 实时打印（✅/❌、耗时、类别匹配、分数）
- `report()`：生成三格式报告

**关键**：`llm=None` 时自动注入桩 LLM，**无 API key 也能验证全链路**。

### 4.2 orchestrator.py —— 端到端编排

跑 3 个代表性会话：
1. FAQ 咨询（approve）
2. 订单查询（approve）
3. 投诉（**reject_then_approve**：先驳回一次再批准，验证 HITL 全分支）

每个会话：打点 → 评估 → 知识库补全尝试 → 记录。最后生成综合报告。

### 4.3 run_demo.py —— 一键入口

```bash
python -m practice.run_demo          # 桩 LLM（无需 key，验证流程）
python -m practice.run_demo --real   # 真实 qwen3.7-plus（需 .env QWEN key）
```

## 5. 全链路测试结果（桩 LLM）

```
[benchmark] 5/5 用例全部通过，成功率 1.0，全部分数 100
[orchestrator] 3 个模拟会话完成（含 reject_then_approve 分支）
[reporter] 报告生成：
  reports/benchmark-20260827-164416.json / .md / .html
  reports/practice.html / practice.json
```

注意：桩 LLM 返回固定模板，分数恒为 100；**真实 qwen3.7-plus 下分数会有区分度**（这才是评估的意义）。

## 6. 与之前模块的衔接

| 模块 | 能力 | 在本项目的位置 |
|------|------|--------------|
| 模块 1-3 | LangChain 基础/RAG | cs_supervisor.py 的 llm/lookup_faq |
| 模块 4 | 向量数据库 | vector_store.py (ChromaDB) |
| 模块 5 | LangGraph 图编排 | cs_supervisor.py StateGraph |
| 模块 6 | Tool Calling | tools.py (订单/物流) |
| 模块 7 | 框架横评 | 07_框架横评学习笔记.md |
| 模块 8 | 微调（了解） | 08_模型微调操作指南.md |
| 模块 9 | 部署（了解） | 09_模型部署指南.md |
| **模块 10** | **观测评估** | **monitoring/（新）** |
| **模块 11** | **综合实战** | **practice/（新）** |

## 7. 常见问题

| 问题 | 答案 |
|------|------|
| 为什么桩 LLM 分数都是 100？ | 桩返回固定模板，规则质检全部满足；真实 LLM 才有区分度 |
| 报告里 HumanMessage 序列化报错？ | 已修复：序列化前把 LangChain 对象转成 {type, content} |
| 想跑真实 LLM？ | `python -m practice.run_demo --real`，需 .env 配 QWEN key |
| 怎么扩展测试集？ | 改 `benchmark.py` 的 `DEFAULT_CASES` 列表 |
| 报告在哪看？ | HTML 浏览器打开；JSON 可喂 ELK；MD 可贴文档 |
