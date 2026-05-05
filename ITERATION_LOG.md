# Hermes Multi-Agent System — 迭代进度日志

**项目**：smart_butler-server（`/home/seven/smart_butler-server`）
**语言**：Python 3 / FastAPI / LangChain + LangGraph
**最后更新**：2026-05-05
**当前版本**：v2.6

---

## v2.6 — 2026-05-05：Bug 修复

### 1. LLMInfo 导入错误（chat.py）

**问题**：`/chat/send`、`/chat/stream`、`/chat/upload` 三个接口在 `user_model` 为 dict 时，
调用 `engine.LLMInfo(...)` 抛出 `AttributeError: 'HermesEngine' object has no attribute 'LLMInfo'`。

**根因**：`LLMInfo` 是 `app/core/hermes_engine.py` 的模块级 dataclass，不是 `HermesEngine`
实例属性，不能通过 `engine.LLMInfo` 访问。

**修复**（`app/api/chat.py`）：

- 新增导入：`from app.core.hermes_engine import LLMInfo`
- 将三处 `engine.LLMInfo(...)` 替换为 `LLMInfo(...)`

---

### 2. 记忆预取 key 始终未写入（memory_manager.py）

**问题**：`memory:{user_id}:prefetch_result` Redis key 从未被写入，
导致 `ContextManager._retrieve_memories()` Step 0 永远 miss，预取机制形同虚设。

**根因（双层）**：

**缺陷一 — ES 数据缺失**：`_run_prefetch()` 直接调用 `retrieve_memory()`（ES + 向量搜索）。
ES 数据只有在 Redis 列表长度达到 `es_sync_threshold`（默认 60% × recent_turns ≈ 6 条）时才会触发同步。
新用户或早期对话，ES 索引为空，`retrieve_memory` 返回 `[]`，`_run_prefetch` 命中 `if not results: return`，
**永不写入**。

**缺陷二 — 竞态条件**：即使 ES 有数据，`_sync_recent_to_es` 写入时使用 `refresh=False`（ES 默认 1s 刷新周期），
与 `_run_prefetch` 作为背景任务并发执行时，搜索极可能在文档可见前完成，仍返回空。

**修复**（`app/core/memory_manager.py`，`_run_prefetch` 方法）：
在 `retrieve_memory` 返回空时，补充读取 `get_recent_turns()` Redis 近期对话作为 fallback，
取最后 `retrieval_top_k` 条写入预取缓存。

```python
results = await self.retrieve_memory(user_id, query, top_k=self.retrieval_top_k)
if not results:
    recent = await self.get_recent_turns(user_id)
    if not recent:
        return
    results = recent[-self.retrieval_top_k:]
```

**效果**：ES 未就绪时自动降级到 Redis 近期对话；ES 就绪后行为不变。

---

## v2.5 — 2026-04-29：记忆系统深度优化 + 月度/年度归档 + 系统级定时任务

### 1. hermes-agent 记忆功能移植（5 项）

| 功能 | 涉及文件 | 核心实现 |
| --- | --- | --- |
| on_delegation 钩子 | hermes_engine.py | 每步 Agent 执行后异步记录委托链到 Redis（定长 20 条） |
| 用户画像注入 | hermes_engine.py | 系统提示词末尾追加 `<user-profile>` XML 块 |
| 背景预取 | hermes_engine.py + memory_manager.py | store_turn() 后 queue_prefetch()，后台异步预热 |
| 预取消费 | context_manager.py | _retrieve_memories() Step 0 原子 GETDEL，命中跳过完整检索 |
| memory_manager 扩展 | memory_manager.py | on_delegation / queue_prefetch / get_prefetched_context / build_system_prompt_block |

### 2. 记忆压缩重构

`app/agents/workers/summarizer.py` 完整重写：

- **不保留上下文**：压缩完成后 Redis 原始 turn 全部替换为摘要 turn
- **Agent 调用一行化**：`{agent_name}：{task≤40字} → {result≤60字}`
- **固定 8 节摘要结构**：事件概要 / 用户意图 / 决策与结论 / Agent 调用记录 / 待办与跟进 / 用户偏好与习惯 / 知识积累 / 情感与背景
- 新增 `summarize_monthly()` / `summarize_yearly()`，独立 4 节提示词

### 3. 月度归档（app/agents/system/monthly_archiver.py）

- 归档超过 365 天的历史 turn，按自然月分组
- Saga 三步：LLM 生成摘要 → ES checkpoint → 删除原始 turn
- MySQL 作业表 `memory_monthly_jobs`（`UNIQUE KEY uq_user_ym`，防重复）

### 4. 年度归档（app/agents/system/yearly_archiver.py）

- 归档超过 3×365 天的月度摘要，按自然年分组
- Saga 三步，MySQL 作业表 `memory_yearly_jobs`（`UNIQUE KEY uq_user_year`）

### 5. 系统级定时任务框架

- `models.py`：ActionType 新增 `SYSTEM`
- `runner.py`：模块级 handler 注册表 + SYSTEM 分支 + 通知守卫
- `system_tasks.py`（新增）：monthly_archive_handler / yearly_archive_handler / register_system_tasks
- `main.py`：lifespan step 10 注册系统任务
- Cron：月度 `0 23 28,29,30,31 * *` / 年度 `0 1 31 12 *`

---

## v2.4 — 2026-04-28：定时任务系统 + 文件读取工具 + 敏感信息遮盖

### 1. 定时任务系统（app/scheduler/）

新增 6 个文件：`models.py` / `holiday.py` / `store.py` / `notifier.py` / `runner.py` / `system_tasks.py`

**支持类型**：once / daily / weekly / monthly / workday / weekend / cron

**关键设计**：

- 时间存储 UTC，API 层 `_cst_hour_to_utc()` 转换 CST
- 中国法定节假日（含调休补班）
- SSE 流：3s 轮询，200 轮无新通知自动关闭
- Cron 自实现 5 字段解析器

### 2. 文件读取工具（app/tools/builtin/file_reader.py，约 650 行）

17 类格式：纯文本、结构化文本、Excel、Word、PowerPoint、PDF、图片、数据帧

输出 Content Parts（Anthropic/OpenAI 多模态消息格式），路径安全约束在 `DATA_ROOT/{user_id}/`

### 3. 敏感信息自动遮盖

三套正则策略（`_SENSITIVE_RE` / `_SENSITIVE_RE_ENV` / `_SENSITIVE_HEADER_RE`），
覆盖 JSON、YAML、Word、Excel、.env、配置文件，零误判（auth: production 等不触发）

---

## v2.3 — 2026-04-27：项目根路径统一管理

**问题**：11 处代码用各自独立的 `Path(__file__).parent...` 链式调用（1~4 级不等）。

**方案**：`app/core/paths.py` 提供全局 `PROJECT_ROOT`，入口文件顶部
`os.environ.setdefault("PROJECT_ROOT", ...)` 设置一次，内部模块统一导入。

---

## v2.2 — 2026-04-26：多项功能补全与模板体系建设

1. `/models/change` 后清除 LLM 缓存（`clear_llm_cache(user_id)`）
2. Prompt 模板全面外置至 `config/templates/`（12 个文件）
3. 程序关闭时用户画像批量固化（`_flush_all_profiles_on_shutdown`）

---

## v2.1 — 2026-04-25：日志统一与文档补全

- `main.py` 等 7 个文件硬编码 `logging.INFO` 改为从 YAML 读取
- 新增各模块 README / COMPLETION_REPORT
- requirements.txt 补充 langchain-core / langchain-openai / python-jose

---

## v2.0 — 2026-04-24：向量切片升级 + Ollama NaN 修复

| 变更 | 旧行为 | 新行为 |
| --- | --- | --- |
| 切片策略 | Q+A 合并为 qa_combined | question / answer / agent_output 独立块 |
| 短输入 | 全部生成 question 块 | < 10 字符跳过 |
| 调用链 | 只存最终结果 | 串行流水线每步独立向量化 |

Ollama NaN bug 三层降级：预处理 → /api/embed → 截半重试

---

## v1.0 — 2026-04-21：核心系统初建

- LangGraph `create_react_agent`，替代旧 AgentExecutor
- 三层记忆架构（Redis L1 + MySQL L2 + ES L3）
- 触发式 ES 同步（Redis 锁防并发）
- 混合检索：向量 + 全文，confidence_threshold 过滤
- RouterAgent（意图识别 + 任务分解 + 模式规划）
- Agent 技能记忆系统（成功率滚动加权，最多 10 条）
- DB Agent 动态管理（API 创建，热重载，评分告警）

---

## 代码规模（截至 v2.6）

| 模块 | 估算行数 |
| --- | --- |
| app/core | ~4,050 行 |
| app/agents | ~3,150 行 |
| app/api | ~2,500 行 |
| app/database | ~2,150 行 |
| app/tools | ~1,600 行 |
| app/scheduler | ~1,050 行 |
| app/sandbox | ~590 行 |
| main.py | ~450 行 |
| **总计** | **~15,500+ 行** |

---

## 待实现功能

### 高优先级

| 功能 | 说明 |
| --- | --- |
| 归档作业表建表 | memory_monthly_jobs / memory_yearly_jobs 需在 create_tables.py 添加 DDL |
| Worker Agent 工具调用 | data_analyst / customer_support / code_assistant 接入真实工具 |

### 中优先级

| 功能 | 说明 |
| --- | --- |
| revectorize 保留 agent 结构 | 全量重建时历史 turn 无 agent_outputs 字段 |
| PII 脱敏接入 | _desensitize() 仅清空字段 |

### 低优先级

| 功能 | 说明 |
| --- | --- |
| API 速率限制 | slowapi 或自定义中间件 |
| Docker Compose 编排 | 一键启动全服务 |
| Prometheus 指标导出 | /metrics 端点 |
