# Hermes Multi-Agent System — 详细设计文档

**版本**：v2.11
**最后更新**：2026-05-15
**状态**：生产就绪（持续迭代）

> 本文档描述系统的**实际实现**，而非规划草稿。每章内容与代码保持一致。

---

## 目录

1. [系统架构概览](#1-系统架构概览)
2. [核心编排引擎 HermesEngine](#2-核心编排引擎-hermesengine)
3. [路由与 Pipeline 编排](#3-路由与-pipeline-编排)
4. [RAG 检索增强管道](#4-rag-检索增强管道)
5. [工具系统](#5-工具系统)
6. [危险操作授权系统](#6-危险操作授权系统)
7. [记忆管理系统](#7-记忆管理系统)
8. [定时任务调度系统](#8-定时任务调度系统)
9. [安全沙箱](#9-安全沙箱)
10. [日志系统](#10-日志系统)
11. [数据库设计](#11-数据库设计)
12. [API 接口设计](#12-api-接口设计)
13. [配置管理](#13-配置管理)
14. [关键流程时序图](#14-关键流程时序图)

---

## 1. 系统架构概览

### 1.1 分层架构

```
┌─────────────────────────────────────────────────────────────────┐
│                  接入层 (FastAPI)                                │
│  /auth  /models  /chat  /agents  /tools  /scheduler             │
│  /decisions  /files                                             │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│                编排层 (HermesEngine)                             │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │ RouterAgent │  │AgentEventLoop│  │   RagPipeline        │  │
│  │ 意图识别    │  │ 工具构建门控 │  │ 三层检索+预取缓存     │  │
│  │ 任务分解    │  │ ReAct Agent  │  └──────────────────────┘  │
│  │ 模式规划    │  └──────────────┘                             │
│  └─────────────┘                                               │
│  ┌──────────────────────┐  ┌──────────────────────────────┐   │
│  │   工具系统 (Tools)   │  │   ConsentManager             │   │
│  │ registry/loader      │  │ 危险操作授权 (v2.9)           │   │
│  │ dangerous_ops 声明   │  │ asyncio.Future 暂停/恢复      │   │
│  └──────────────────────┘  └──────────────────────────────┘   │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│                   数据存储层                                      │
│  Redis  — L1 近期对话 / 预取缓存 / 委托链 / 通知队列 / 锁       │
│  MySQL  — 用户/模型/Agent/工具/记忆事实/调度任务/授权配置        │
│  ES     — 聊天历史（全文）+ 向量索引（KNN/script_score）         │
└──────────────────────────────────────────────────────────────────┘
```

### 1.2 技术栈

| 层次 | 技术 | 说明 |
|------|------|------|
| Web 框架 | FastAPI 0.111 | 异步路由 + SSE 流式响应 |
| Agent 编排 | LangGraph `create_react_agent` | ReAct 模式，支持工具调用循环 |
| LLM 接口 | LangChain `init_chat_model` | 统一多 Provider（OpenAI/Anthropic/Ollama） |
| 数据库 ORM | 原生 aiomysql + 自建异步连接池 | 支持 PooledConnection 状态机 |
| 向量索引 | Elasticsearch 7/8/9 自动适配 | BM25 全文 + KNN 向量混合检索 |
| 缓存/锁 | Redis 6+ | asyncio-compatible aioredis |
| Embedding | Ollama bge-m3 / OpenAI 兼容 | 支持切换，三层 NaN 容错 |
| 任务调度 | 自研 30s tick 异步调度 | 7 种类型 + cron 5 字段解析器 |
| 安全沙箱 | subprocess + resource 限制 | Linux CPU/内存隔离 |
| 前端 | Vue 3 + TypeScript + Pinia | SSE over fetch（非 WebSocket） |

### 1.3 设计原则

- **多租户隔离**：所有数据库查询强制注入 `user_id` 过滤器
- **原地暂停/恢复**：危险操作授权通过 `asyncio.Future` 实现，不重跑 pipeline
- **三层降级**：关键路径（LLM 加载、记忆检索、授权查询）均有降级策略
- **彩色结构化日志**：`log_bus.py` 按事件类型着色，便于扫描；可接 ELK/Loki
- **配置外置**：Prompt 模板全在 `config/templates/`，Agent 行为无需改代码

---

## 2. 核心编排引擎 HermesEngine

### 2.1 职责

`app/core/hermes_engine.py` — 系统唯一入口，接收用户消息后协调所有子系统完成一次对话轮次。

### 2.2 两条处理路径

#### 同步路径（`process_user_input`）

```
process_user_input(user_id, user_input, context)
  → LLMInfo.load()      # 加载用户 LLM 配置
  → _get_or_build_rag() # RAG 上下文组装
  → RouterAgent.route() # 意图识别 + 任务分解 + 模式规划
  → _execute_pipeline() # 执行 Agent 流水线
  → _generate_llm_response_stream() # 最终 LLM 汇总
  → _save_turn_async()  # 后台异步保存 ES + MemoryManager
```

#### 流式路径（`process_user_input_stream`）

流式路径在后台任务 `_run()` 中执行，通过 `asyncio.Queue` 与 SSE 生成器解耦：

```
process_user_input_stream(user_id, user_input, context)
  → 创建 asyncio.Queue
  → asyncio.create_task(_run())   # 后台异步执行
  → yield SSE events from queue   # 前台消费队列
```

`_run()` 内部流程：

```
_start_turn(turn_id)
→ bus.user_message()              # 日志
→ RagPipeline.build_context()     # RAG 上下文
→ RouterAgent                     # 路由，推送 routing/planning 事件
→ 注入 consent_hook + turn_id    # 危险操作授权 Hook（v2.9）
→ _execute_pipeline()             # 执行 Agent 流水线
→ 最终 LLM stream_aiter           # 推送 token 事件
→ bus.conversation_turn()         # 轮次摘要日志（v2.10）
→ _save_turn_async()              # 后台保存
→ q.put_nowait(done/error)
→ _end_turn(turn_id)
```

### 2.3 LLM 加载机制

每用户独立 LLM 配置，按 `user_id` 缓存实例：

```python
# _llm_cache: Dict[str, BaseChatModel]

LLMInfo (dataclass):
  url        → str
  api_key    → str
  model_name → str
  model_type → str   # "chat" / "embedding"
  temperature→ float

load(user_id) → 查 MySQL llms 表 → 找不到时 fallback user_id="0"
```

`/models/change` 成功后调用 `clear_llm_cache(user_id)` 使缓存立即失效。

### 2.4 取消机制

每个用户维护一个 `asyncio.Event`（`_cancel_signals[user_id]`），`POST /chat/cancel` 置位后：

- `_run()` 在 pipeline 执行前后的关键检查点检测到信号
- 推送 `cancelled` SSE 事件并返回
- `_save_turn_async()` 检测到 `turn_id in _cancelled_turns` 则跳过保存

---

## 3. 路由与 Pipeline 编排

### 3.1 RouterAgent 三步 LLM 推理

`app/agents/router.py` — 每次请求执行三次 LLM 调用：

| 步骤 | 输出 | 用途 |
|------|------|------|
| `identify_intent()` | intent 字符串 | 语义分类，如 "数据分析" / "代码调试" |
| `decompose_tasks()` | `[{step, agent_name, description}]` | 任务分解，形成 pipeline 步骤列表 |
| `plan_execution()` | mode + target_agent | 执行模式选择 |

### 3.2 三种执行模式

| 模式 | 触发条件 | 实现 |
|------|----------|------|
| `single` | 单 agent，无需流水线 | 直接调用目标 agent |
| `serial` | 多 agent 顺序依赖 | 前步 `prev_result` 传给后步 context |
| `parallel` | 多 agent 独立任务 | `asyncio.gather()` 并行执行 |

### 3.3 重规划（Replan）

执行失败或结果不满足质量检查时，`RouterAgent` 可触发 replan：

- 推送 `router_replan` SSE 事件
- 已完成 agents 状态保留，重新规划剩余步骤
- `routerIteration` 计数器递增，最多 3 次

### 3.4 SSE Pipeline 事件序列

```
routing → planning → agent_start → step_start → tool_call →
tool_result → step_done → agent_done → token(×N) → done
```

`consent_required` 事件可在 `tool_call` 前插入，暂停整个序列。

---

## 4. RAG 检索增强管道

### 4.1 模块结构（`app/rag/`）

| 文件 | 职责 |
|------|------|
| `pipeline.py` | `RagPipeline` 门面类，统一入口 |
| `retriever.py` | `HybridRetriever`：预取 → 向量 → 全文 三步检索 |
| `indexer.py` | `TurnIndexer`：ES 向量写入、全量重建 |
| `chunker.py` | `TurnChunker`：对话切片（question/answer/agent_output 独立块） |
| `formatter.py` | 记忆内容格式化，构建 `<memory-context>` XML 块 |
| `types.py` | `RagContext` 数据类，`to_prompt_context()` 转换 |

### 4.2 混合检索策略（`HybridRetriever`）

```
Step 0: 预取缓存（Redis GETDEL memory:{user_id}:prefetch_result）
  ↓ Miss
Step 1: 向量 KNN 检索（ES knn / script_score，余弦相似度 ≥ 0.7）
  ↓
Step 2: BM25 全文检索（score ≥ max(relative_min, text_abs_floor)）
  ↓
Step 3: 按 turn_id 合并去重，取最高分，截取 top_k
```

**双阈值策略**：向量结果用绝对阈值（0.7），全文结果用相对+绝对混合阈值，避免语料稀疏导致低质量结果通过。

**同会话评分加权**：同 `session_id` 的历史 turn 分数额外加权，提升上下文连贯性。

### 4.3 预取缓存机制

每轮对话结束后，`store_turn()` 后台调用 `queue_prefetch(user_id, user_input)`:

1. 以当前 `user_input` 为 query 预取下一轮可能用到的记忆
2. 结果写入 `memory:{user_id}:prefetch_result`（TTL 5min）
3. 下一轮请求 Step 0 原子 `GETDEL` 命中则跳过完整检索

**ES 未就绪降级**：`retrieve_memory()` 返回空时，fallback 读取 Redis 近期对话写入预取缓存。

### 4.4 切片策略

每轮对话生成三类独立向量块：

| 块类型 | 来源 | 最短长度 |
|--------|------|---------|
| `question` | 用户输入 | 10 字符 |
| `answer` | LLM 最终回复 | 10 字符 |
| `agent_output` | 各 agent 独立输出 | 10 字符 |

串行 pipeline 的每步 agent 输出均独立向量化（而非只存最终结果）。

### 4.5 ES 版本兼容

- ES 7.x：使用 `script_score` + `cosineSimilarity`
- ES 8.x/9.x：使用原生 `knn` 查询
- 版本在启动时通过 `_es_major_version` 检测并缓存

---

## 5. 工具系统

### 5.1 工具来源与可见性

| 来源 (`source`) | 创建方式 | 可见性选项 |
|----------------|---------|-----------|
| `code` | 继承 `BaseTool`，代码中定义 | public / private / exclusive |
| `user` | 用户通过 API 创建 | public / private |
| `agent` | 运行时由 Agent 动态生成 | exclusive（仅归属 Agent） |

### 5.2 执行位置

| `exec_location` | 说明 |
|----------------|------|
| `server` | 在服务端 Python 进程中执行 |
| `client` | 推送给客户端执行，服务端不运行 |

### 5.3 BaseTool 执行流程

```python
BaseTool._wrapped_execute(params, context):
  for op in self.dangerous_ops:
    ok = await consent_manager.check_consented(tool_name, op, user_id, ...)
    if not ok:
      hook = get_consent_hook()
      if hook:
        decision = await hook(ConsentRequiredException(...))
        if decision == "deny":
          return {result: "用户拒绝", success: False}
        # allow/conversation → continue
      else:
        raise ConsentRequiredException(...)
  result = await self.execute(params, context)
  return result
```

### 5.4 危险操作类型（`DANGEROUS_OPS`）

| 类型 | 说明 |
|------|------|
| `modify` | 修改文件/数据 |
| `delete` | 删除操作 |
| `execute` | 执行命令 |
| `network` | 网络请求 |
| `privilege` | 权限提升 |
| `sensitive_read` | 读取敏感信息 |

工具在定义时声明，例如：`dangerous_ops = ["modify", "delete"]`

### 5.5 工具注册与加载

- **启动时**：`builtin/__init__.py` 统一导入所有内置工具并注册
- **运行时**：`ToolLoader` 按 source 动态从 MySQL 加载 user/agent 工具
- **Agent 绑定**：`_registry_tools_for_agent(agent_name, user_id)` 返回 public + exclusive 工具集，按 `user_id + agent_name` 缓存 LangGraph 实例

---

## 6. 危险操作授权系统

> v2.9 新增，v2.10 优化（全量放行 + 性能缓存）

### 6.1 设计目标

工具执行危险操作时，**原地暂停**工具执行（不重跑 pipeline），向前端推送 SSE 授权事件，等待用户决策后恢复，整个过程对 Agent 调用链透明。

### 6.2 授权级别优先级链

```
op_disabled（用户已关闭该操作类型授权）
  ↓ 未关闭
once（当次调用临时放行）
  ↓ 无
conversation-blanket（本轮全量放行，v2.10 新增）
  ↓ 无
conversation（本轮特定 tool+op 放行）
  ↓ 无
session（本会话内放行，内存缓存）
  ↓ 无
project / always（持久化到 MySQL）
  ↓ 无
→ 需要弹窗授权
```

### 6.3 核心数据结构（`app/tools/permission.py`）

```python
# 模块级内存缓存
_session_cache:        Dict[(tool, op, session_id), bool]
_once_granted:         Set[(tool, op, user_id)]
_conversation_cache:   Dict[(tool, op, turn_id), bool]
_conversation_blanket: Set[turn_id]        # v2.10：全量放行集合
_op_enabled_cache:     Dict[(user_id, op), (bool, timestamp)]  # v2.10：60s TTL

# ContextVar（流式场景由 HermesEngine 注入）
_CONSENT_HOOK:    ContextVar[Optional[Callable]]   # async hook
_CONSENT_TURN_ID: ContextVar[str]                  # 当前 turn_id
```

### 6.4 asyncio.Future 暂停机制

```python
# HermesEngine._run() 中注入的 _consent_hook 闭包

async def _consent_hook(exc: ConsentRequiredException) -> str:
    async with _consent_lock:                     # 串行化并发请求
        already = await consent_manager.check_consented(...)
        if already:
            return "allow"                        # 并发已授权，跳过

        fut = asyncio.get_event_loop().create_future()
        self._consent_futures[exc.request_id] = fut
        q.put_nowait({"event": "consent_required", "data": exc.to_dict()})

        try:
            decision = await asyncio.wait_for(
                asyncio.shield(fut), timeout=300  # 5 分钟超时
            )
        except asyncio.TimeoutError:
            decision = "deny"
        finally:
            self._consent_futures.pop(exc.request_id, None)

        if decision == "conversation":
            consent_manager.grant_conversation_all(turn_id)  # v2.10

        bus.consent_decision(user_id, tool, op, decision)   # v2.10
        return decision

# POST /chat/consent 触发
def consent_respond(request_id, decision) -> bool:
    fut = self._consent_futures.get(request_id)
    if fut and not fut.done():
        fut.set_result(decision)   # 恢复工具执行
        return True
    return False
```

### 6.5 全量放行（v2.10 `_conversation_blanket`）

用户选择「当前对话允许」时：
- 调用 `grant_conversation_all(turn_id)` → `_conversation_blanket.add(turn_id)`
- `check_consented()` 检测 `turn_id in _conversation_blanket` 直接返回 `True`
- 本轮任意工具的任意危险操作均不弹窗
- 轮次结束或清除时调用 `revoke_conversation(turn_id)` 同步清理

### 6.6 性能缓存（v2.10 `_op_enabled_cache`）

`_is_op_enabled()` 每次工具调用都查询 MySQL `dangerous_op_configs`，缓存解决频繁查询问题：

```python
_OP_ENABLED_TTL = 60.0  # 秒
_op_enabled_cache: Dict[(user_id, op), (bool, timestamp)]

# PATCH /tools/dangerous-ops/{op_type} 成功后：
_invalidate_op_cache(user_id, op_type)  # 立即失效，无需等 TTL
```

### 6.7 用户级开关（`dangerous_op_configs` 表）

用户可在设置页关闭某类操作的授权要求。关闭后 `_is_op_enabled()` 返回 `False`，`check_consented()` 优先级链第一步即放行。

---

## 7. 记忆管理系统

### 7.1 三层架构

| 层 | 存储 | 容量/TTL | 作用 |
|----|------|---------|------|
| L1 | Redis List | 最近 10 轮，无 TTL | 快速加载近期对话 |
| L2 | MySQL `memory_facts` | 持久化 | 结构化事实，引用计数 |
| L3 | Elasticsearch | 永久 | 全文 + 向量语义检索 |

### 7.2 L1→L3 同步机制

- 触发条件：Redis List 长度 ≥ `recent_turns × es_sync_threshold_pct`（默认 10 × 0.6 = 6）
- 同步以 `turn_id` 为幂等键，防重复写入
- `refresh=False`（ES 默认 1s 刷新周期），与预取并发时有窗口，预取降级到 Redis 近期对话兜底

### 7.3 记忆压缩（`MemoryArchiverAgent`）

**触发条件**：累计轮次计数器 ≥ `max_total_turns`（默认 30）

**Saga 三步**：
1. `LLM 生成摘要`（Summarizer Agent，8 节结构）
2. `ES checkpoint`（写入摘要记录，`is_summary=true`）
3. `删除原始 turn`（Redis + ES 清除压缩区间的对话）

**8 节摘要结构**：事件概要 / 用户意图 / 决策与结论 / Agent 调用记录 / 待办与跟进 / 用户偏好与习惯 / 知识积累 / 情感与背景

**月度/年度归档**（独立 4 节提示词）：
- 月度：每月月底，归档 > 365 天的 turn，`memory_monthly_jobs` 作业表（`UNIQUE KEY uq_user_ym`，防重复）
- 年度：每年 12 月 31 日，归档 > 3 年的月度摘要，`memory_yearly_jobs` 作业表

### 7.4 用户画像

`MemoryManager` 在每轮对话前加载用户画像（`_user_profile`），注入系统提示 `<user-profile>` XML 块，使模型感知用户偏好、专业背景等。

程序关闭时 `_flush_all_profiles_on_shutdown()` 批量固化到 MySQL。

---

## 8. 定时任务调度系统

### 8.1 架构（`app/scheduler/`）

| 文件 | 职责 |
|------|------|
| `runner.py` | 异步调度主循环（30s tick），handler 注册表 |
| `store.py` | MySQL 持久化，upsert/查询/更新 |
| `notifier.py` | Redis 通知队列 + SSE 推送（3s 轮询） |
| `models.py` | TaskType / ActionType / TaskStatus 枚举 |
| `holiday.py` | 中国法定节假日（含调休补班） |
| `system_tasks.py` | 月度/年度归档的幂等 cron 注册 |

### 8.2 任务类型

`once` / `daily` / `weekly` / `monthly` / `workday` / `weekend` / `cron`

时间存储 UTC，API 层接收 CST（UTC+8）自动转换 `_cst_hour_to_utc()`。

### 8.3 Cron 解析器

自实现 5 字段解析器（`分 时 日 月 周`），支持 `*` / 数值 / 逗号列表，不依赖第三方库。

### 8.4 系统级 Cron

| 任务 | Cron 表达式 | 说明 |
|------|------------|------|
| 月度归档 | `0 23 28,29,30,31 * *` | 月底 UTC 23:00，幂等（作业表防重） |
| 年度归档 | `0 1 31 12 *` | 12 月 31 日 UTC 01:00 |
| 文件清理 | `0 2 * * *` | 每日 UTC 02:00，清理 `cleanup_days` 前的生成文件 |

---

## 9. 安全沙箱

### 9.1 架构（`app/sandbox/`）

| 文件 | 职责 |
|------|------|
| `executor.py` | subprocess 隔离执行，强制超时，Linux resource 限制 |
| `scanner.py` | 静态扫描高危调用模式 |
| `file_handler.py` | 沙箱文件管理，隔离临时目录 |

### 9.2 执行约束

- **CPU**：通过 `resource.RLIMIT_CPU` 限制 CPU 时间
- **内存**：`resource.RLIMIT_AS` 限制进程地址空间
- **超时**：subprocess 强制 `timeout` 参数
- **隔离目录**：每次执行创建独立临时目录，执行后清理

### 9.3 静态扫描规则

`scanner.py` 拦截以下高危模式：`os.system` / `subprocess` / `eval` / `exec` / `__import__` / 网络 socket 创建等。

---

## 10. 日志系统

### 10.1 架构（`app/utils/log_bus.py`）

`HermesLogger` 单例，所有事件在 `hermes.bus` logger 命名空间下发射，不影响其他模块的 logging 配置。

### 10.2 事件颜色映射（终端 ANSI）

| 事件类型 | 颜色 | 触发时机 |
|----------|------|---------|
| `user` | 亮青 | 用户消息进入系统 |
| `context` | 蓝 | RAG 上下文组装完成 |
| `routing` | 洋红 | 路由决策结果 |
| `llm_in` | 黄 | 发送给 LLM 的输入 |
| `llm_out` | 亮绿 | LLM 返回内容 |
| `tool` | 白 | 工具调用开始 |
| `tool_ok` | 绿 | 工具调用成功 |
| `tool_err` | 亮红 | 工具调用失败 |
| `turn` | 亮紫 | 完整轮次摘要（v2.10 新增） |
| `consent` | 亮黄 | 危险操作授权决策（v2.10 新增） |
| `system` | 深灰 | 系统/其他 |

### 10.3 关键方法

```python
bus.user_message(user_id, message, client_type)
bus.context_built(user_id, history_count, memory_count, client_type)
bus.routing(user_id, intent, mode, target_agent, pipeline_steps)
bus.llm_input(user_id, agent_name, human_content, system_prompt, tools)
bus.llm_output(user_id, agent_name, response, elapsed_ms)
bus.tool_call(user_id, agent_name, tool_name, args)
bus.tool_result(user_id, agent_name, tool_name, result, elapsed_ms)
bus.tool_error(user_id, agent_name, tool_name, error)

# v2.10 新增
bus.conversation_turn(user_id, turn_id, user_message, intent, mode,
                      agents_used, tools_called, response_len, elapsed_ms, client_type)
bus.consent_decision(user_id, tool_name, operation, decision)
```

`tool_call` / `tool_result` / `tool_error` 同时调用 `progress_bus.push()` 推送 SSE 进度事件。

### 10.4 JSON 文件输出（可选）

`init_log_bus({"json_file": "logs/hermes.json"})` 启用 JSON handler，写入 NDJSON 格式，供 ELK/Loki 采集。

---

## 11. 数据库设计

### 11.1 MySQL 核心表

#### `users` — 用户账户

```sql
CREATE TABLE users (
  id          VARCHAR(36) PRIMARY KEY,
  username    VARCHAR(64) UNIQUE NOT NULL,
  password    VARCHAR(255) NOT NULL,  -- bcrypt hash
  created_at  DATETIME DEFAULT NOW()
);
```

#### `llms` — 用户 LLM 配置

```sql
CREATE TABLE llms (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  user_id     VARCHAR(36) NOT NULL,
  url         VARCHAR(255),
  api_key     VARCHAR(512),
  model_name  VARCHAR(128),
  model_type  VARCHAR(32) DEFAULT 'chat',
  temperature FLOAT DEFAULT 0.7,
  state       TINYINT DEFAULT 1,  -- 1=激活
  created_at  DATETIME DEFAULT NOW()
);
```

#### `agents` — 用户自定义 Agent

```sql
CREATE TABLE agents (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  user_id     VARCHAR(36) NOT NULL,
  name        VARCHAR(64) NOT NULL,
  role        VARCHAR(255),
  background  TEXT,
  is_public   TINYINT DEFAULT 0,
  source      VARCHAR(16) DEFAULT 'db',  -- 'code' | 'db'
  created_at  DATETIME DEFAULT NOW()
);
```

#### `tools` — 工具定义

```sql
CREATE TABLE tools (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  name            VARCHAR(64) NOT NULL,
  description     TEXT,
  code            LONGTEXT,
  exec_location   VARCHAR(16) DEFAULT 'server',  -- 'server' | 'client'
  visibility      VARCHAR(16) DEFAULT 'private',  -- 'public' | 'private' | 'exclusive'
  source          VARCHAR(16) DEFAULT 'user',     -- 'code' | 'user' | 'agent'
  dangerous_ops   JSON,                           -- ["modify", "delete"]
  owner_user_id   VARCHAR(36),
  owner_agent_name VARCHAR(64),
  created_at      DATETIME DEFAULT NOW()
);
```

#### `tool_consent_records` — 工具授权持久化

```sql
CREATE TABLE tool_consent_records (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  tool_name     VARCHAR(64),
  operation     VARCHAR(50),
  user_id       VARCHAR(36),
  consent_level VARCHAR(20),  -- 'project' | 'always'
  session_id    VARCHAR(64),
  project_id    VARCHAR(64),
  granted_at    DATETIME,
  expires_at    DATETIME,
  UNIQUE KEY uq_tool_op_user (tool_name, operation, user_id, consent_level)
);
```

#### `dangerous_op_configs` — 用户级危险操作开关（v2.9）

```sql
CREATE TABLE dangerous_op_configs (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id     VARCHAR(36) NOT NULL,
  op_type     VARCHAR(50) NOT NULL,
  is_enabled  TINYINT(1) NOT NULL DEFAULT 1,  -- 1=需授权，0=自动放行
  created_at  DATETIME DEFAULT NOW(),
  UNIQUE KEY uq_user_op (user_id, op_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

#### `scheduler_tasks` — 定时任务

```sql
CREATE TABLE scheduler_tasks (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  user_id       VARCHAR(36),
  name          VARCHAR(128),
  task_type     VARCHAR(16),  -- once/daily/weekly/monthly/workday/weekend/cron
  action_type   VARCHAR(16),  -- reminder/agent/system
  cron_expr     VARCHAR(64),
  hour          INT,
  minute        INT,
  weekday       INT,
  day_of_month  INT,
  run_at        DATETIME,
  status        VARCHAR(16) DEFAULT 'active',
  last_run_at   DATETIME,
  next_run_at   DATETIME,
  action_data   JSON,
  notify_on_done TINYINT DEFAULT 0,
  created_at    DATETIME DEFAULT NOW()
);
```

#### `memory_monthly_jobs` / `memory_yearly_jobs` — 归档作业

```sql
CREATE TABLE memory_monthly_jobs (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  user_id     VARCHAR(36),
  year_month  VARCHAR(7),   -- "2025-12"
  status      VARCHAR(16),
  created_at  DATETIME DEFAULT NOW(),
  UNIQUE KEY uq_user_ym (user_id, year_month)
);
```

### 11.2 Redis Key 设计

| Key 模式 | 类型 | TTL | 说明 |
|---------|------|-----|------|
| `session:{token}` | Hash | 24h | Token → user_id 映射 |
| `user:{user_id}:llm` | Hash | 1h | LLM 配置缓存 |
| `user:{user_id}:recent_turns` | List | 无 | 近期 10 轮对话 |
| `user:{user_id}:turn_count` | String | 无 | 累计轮次计数 |
| `user:{user_id}:prefetch_result` | String | 5min | 预取记忆缓存 |
| `user:{user_id}:delegation_chain` | List (定长 20) | 无 | on_delegation 委托链 |
| `user:{user_id}:profile` | Hash | 无 | 用户画像 |
| `scheduler:notify:{user_id}` | List | 无 | 通知队列 |
| `es_sync_lock:{user_id}` | String | 30s | ES 同步分布式锁 |

### 11.3 Elasticsearch 索引设计

**Index**：`chat_history_global`（按 `user_id` 字段过滤）

**Mapping**（关键字段）：

```json
{
  "turn_id":        "keyword",
  "user_id":        "keyword",
  "session_id":     "keyword",
  "user_input":     "text + keyword",
  "assistant_response": "text",
  "agent_outputs":  "object",
  "timestamp":      "date",
  "is_summary":     "boolean",
  "message_vector": "dense_vector (dim=1024, similarity=cosine)"
}
```

向量字段支持三种块类型独立存储：`question_vector` / `answer_vector` / `agent_output_vector`（由 `TurnChunker` 生成，各自建立向量索引）。

---

## 12. API 接口设计

### 12.1 接口汇总

#### 认证（`/auth`）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/auth/register` | 用户注册（bcrypt） |
| POST | `/auth/login` | 登录（RSA-OAEP 加密密码），复用有效 session |
| POST | `/auth/logout` | 登出，清除 Redis Token |
| GET  | `/auth/public-key` | 获取 RSA 公钥 + nonce |
| POST | `/auth/change-password` | 修改密码 |

#### 对话（`/chat`）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/chat/send` | 同步发送，返回完整响应 |
| POST | `/chat/stream` | SSE 流式输出 |
| POST | `/chat/cancel` | 取消当前流式对话 |
| GET  | `/chat/history` | 分页查询 ES 历史（turn 粒度） |
| POST | `/chat/upload` | 上传文件后发起对话 |
| POST | `/chat/revectorize` | 全量重建向量索引 |
| POST | `/chat/consent` | **危险操作授权决策响应**（v2.9） |

#### 工具（`/tools`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/tools` | 查询可用工具列表 |
| POST | `/tools` | 创建工具（自然语言或代码） |
| PUT  | `/tools/{tool_id}` | 修改工具属性 |
| DELETE | `/tools/{tool_id}` | 删除工具 |
| GET  | `/tools/dangerous-ops` | **查询危险操作类型开关状态**（v2.9） |
| PATCH | `/tools/dangerous-ops/{op_type}` | **切换危险操作类型开关**（v2.9） |

> **路由顺序**：`/dangerous-ops` 和 `/dangerous-ops/{op_type}` 必须在 `/{tool_id}` 之前注册，防止 FastAPI 将 `dangerous-ops` 误解析为 tool_id。

#### 其他

| 前缀 | 说明 |
|------|------|
| `/models` | LLM 配置 CRUD + 切换 |
| `/agents` | Agent CRUD + 评分 + 热重载 |
| `/scheduler` | 定时任务 CRUD + 通知 SSE |
| `/decisions` | 工具构建决策门控（ask/allow/deny） |
| `/files` | 文件列表 / 下载 / 删除 |

### 12.2 SSE 事件类型

| 事件 | 数据字段 | 说明 |
|------|---------|------|
| `routing` | `intent, mode, target_agent` | 路由完成 |
| `planning` | `pipeline: [{step, agent_name, description}]` | Pipeline 规划 |
| `agent_start` | `agent_name` | Agent 开始执行 |
| `step_start` | `agent_name, step_id, description` | 子步骤开始 |
| `step_done` | `agent_name, step_id, success, result` | 子步骤完成 |
| `tool_call` | `agent_name, tool_name, args` | 工具调用 |
| `tool_result` | `agent_name, tool_name, result, elapsed_ms` | 工具成功 |
| `tool_error` | `agent_name, tool_name, error` | 工具失败 |
| `agent_done` | `agent_name` | Agent 完成 |
| `consent_required` | `request_id, tool_name, operation, description` | **危险操作授权请求**（v2.9） |
| `token` | `text` | 最终输出 token |
| `done` | `turn_id` | 对话完成 |
| `cancelled` | `turn_id` | 已取消 |
| `error` | `message` | 错误 |

### 12.3 鉴权机制

- Header：`Authorization: Bearer <token>`
- Query：`?token=<token>`（兼容 SSE 场景）
- Redis session 查找：`GET session:{token}` → `user_id`
- 降级：Redis 不可用时降级为测试用户（仅开发/测试）

---

## 13. 配置管理

### 13.1 system_config.yaml 结构

```yaml
logging:
  level: "INFO"            # DEBUG / INFO / WARNING / ERROR
  json_file: ""            # 留空则不启用 JSON 文件 handler

database:
  mysql:
    url: "mysql+pymysql://root:pass@localhost/agent_db"
  redis:
    url: "redis://localhost:6379/0"
  elasticsearch:
    url: "http://localhost:9200"

embedding:
  provider: "ollama"       # ollama / openai
  api_url:  "http://localhost:11434"
  model_name: "bge-m3:latest"
  model_dim: 1024

memory:
  recent_turns: 10         # L1 Redis 滚动窗口大小
  max_total_turns: 30      # 触发压缩的累计轮次阈值
  es_sync_threshold_pct: 0.6
  retrieval_top_k: 5
  confidence_threshold: 0.7

sandbox:
  enabled: true
  timeout_seconds: 30
  max_memory_mb: 128

scheduler:
  tick_seconds: 30
```

### 13.2 Prompt 模板（`config/templates/`）

| 文件 | 用途 |
|------|------|
| `router_system.txt` | RouterAgent 系统提示（LangChain 占位符） |
| `default_system.txt` | 默认 Agent 系统提示 |
| `analyst_system.txt` | DataAnalyst Agent 系统提示 |
| `support_system.txt` | CustomerSupport Agent 系统提示 |
| `code_assistant_system.txt` | CodeAssistant Agent 系统提示 |
| `summarizer.txt` | MemoryArchiver 压缩提示（8 节结构） |
| `summarizer_compress.txt` | 对话滚动压缩提示（300 字内） |

---

## 14. 关键流程时序图

### 14.1 流式对话处理完整流程

```
用户
 │ POST /chat/stream
 ▼
FastAPI
 │ asyncio.create_task(_run())
 │ yield SSE from queue ◄──────────────────────────────┐
 ▼                                                      │
_run() (后台任务)                                        │ q.put_nowait(event)
 │ 1. RagPipeline.build_context()                       │
 │    → 预取缓存 / 向量检索 / BM25 检索                  │
 │ 2. RouterAgent.route()                               │
 │    → intent + tasks + mode                           │
 │    → push: routing / planning ─────────────────────► │
 │ 3. set_consent_hook() + set_consent_turn_id()         │
 │ 4. _execute_pipeline(pipeline, mode)                  │
 │    → agent_start / step_start / tool_call ─────────► │
 │    → [危险操作: 推送 consent_required ──────────────► │]
 │    → [等待 Future.set_result() ◄── POST /chat/consent │]
 │    → tool_result / step_done / agent_done ─────────► │
 │ 5. final LLM stream_aiter                             │
 │    → push: token ──────────────────────────────────► │
 │ 6. bus.conversation_turn()                            │
 │ 7. _save_turn_async() (后台)                          │
 │ 8. push: done ─────────────────────────────────────► │
 │ 9. _end_turn()                                        │
 └───────────────────────────────────────────────────────
```

### 14.2 危险操作授权流程

```
工具执行
  │ dangerous_ops = ["modify"]
  │ check_consented() → False
  │ get_consent_hook() → _consent_hook
  ▼
_consent_hook(exc):
  acquire _consent_lock
    check_consented() → still False
    create asyncio.Future
    q.put_nowait({event: "consent_required", data: {request_id, tool_name, operation}})
    await asyncio.wait_for(fut, timeout=300)  ← 工具暂停于此
              │
              │  前端弹出 ConsentDialog
              │  用户点击按钮
              │  POST /chat/consent {request_id, decision}
              │
    consent_respond(request_id, decision)
      fut.set_result(decision)  ← 工具恢复
  ↓
decision == "conversation" → grant_conversation_all(turn_id)
decision == "allow"        → 工具继续执行
decision == "deny"         → return {result: "拒绝", success: False}
```

### 14.3 记忆预取流程

```
轮次 N 结束
  │ _save_turn_async() → MemoryManager.store_turn()
  │ → queue_prefetch(user_id, user_input_N)
  │   后台任务：retrieve_memory(user_input_N) 
  │   写入 Redis memory:{user_id}:prefetch_result (TTL 5min)

轮次 N+1 开始
  │ RagPipeline.build_context()
  │ → HybridRetriever.retrieve()
  │   Step 0: GETDEL memory:{user_id}:prefetch_result
  │   ↓ 命中 → 直接返回，跳过向量+BM25检索
  │   ↓ Miss  → Step 1 向量检索 + Step 2 BM25 检索
```

---

## 附录：功能完成状态（v2.10）

| 模块 | 功能 | 状态 |
|------|------|------|
| 基础设施 | MySQL/Redis/ES 连接池 | ✅ |
| 基础设施 | PooledConnection 状态机 | ✅ |
| 基础设施 | ES 7/8/9 版本自动适配 | ✅ |
| 认证 | 注册/登录/Token/RSA-OAEP | ✅ |
| 认证 | 多租户 user_id 隔离 | ✅ |
| 编排引擎 | 同步/流式双路径 | ✅ |
| 编排引擎 | LLM 动态加载+缓存 | ✅ |
| 编排引擎 | 取消机制 | ✅ |
| 路由 | 意图识别+任务分解+模式规划 | ✅ |
| 路由 | 三种执行模式（single/serial/parallel） | ✅ |
| 路由 | 重规划（replan） | ✅ |
| RAG | 三步混合检索 | ✅ |
| RAG | 预取缓存 | ✅ |
| RAG | 双阈值过滤 | ✅ |
| RAG | 独立向量切片 | ✅ |
| 工具 | 三来源+三可见性框架 | ✅ |
| 工具 | dangerous_ops 声明 | ✅ |
| 工具 | 内置工具（file_reader/file_writer/cli_exec/web_*） | ✅ |
| **工具** | **危险操作授权系统（v2.9）** | ✅ |
| **工具** | **全量放行 + TTL 缓存（v2.10）** | ✅ |
| 记忆 | L1/L2/L3 三层架构 | ✅ |
| 记忆 | 阈值触发压缩+归档 | ✅ |
| 记忆 | 月度/年度归档 | ✅ |
| 记忆 | 用户画像注入 | ✅ |
| 定时任务 | 7 种类型+cron解析器 | ✅ |
| 定时任务 | 中国法定节假日 | ✅ |
| 定时任务 | SSE 通知推送 | ✅ |
| 沙箱 | subprocess 隔离+资源限制 | ✅ |
| 沙箱 | 静态高危扫描 | ✅ |
| 日志 | 彩色结构化终端输出 | ✅ |
| **日志** | **conversation_turn + consent_decision（v2.10）** | ✅ |
| 日志 | JSON 文件输出（ELK/Loki） | ✅ |
| Worker Agent | 框架（data_analyst/support/code_assistant） | ✅ |
| Worker Agent | 工具调用驱动真实业务逻辑 | ⏳ 待完成 |
| 工具评分 | 贝叶斯评分+反作弊 | ⏳ 待完成 |
| 工具版本 | 版本控制（Git-like） | ⏳ 待完成 |
| 可观测性 | LangSmith/Arize Phoenix 接入 | ⏳ 待完成 |
