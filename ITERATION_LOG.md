# Hermes Multi-Agent System — 迭代进度日志

**项目**：smart_butler-server（`/home/seven/smart_butler-server`）
**语言**：Python 3 / FastAPI / LangChain + LangGraph
**最后更新**：2026-05-21
**当前版本**：v2.12

---

## v2.12 — 2026-05-21：架构分层重组 + 评分系统 + Task 模型强化

### 1. app/core/ 精简 — 模块归位

将 `app/core/` 中职责不清的文件迁移到更合理的目录，减少核心层的耦合：

| 原路径 | 新路径 | 说明 |
| --- | --- | --- |
| `app/core/paths.py` | `app/utils/paths.py` | `PROJECT_ROOT` 是工具类常量，不属于业务核心 |
| `app/core/redis_keys.py` | `app/database/redis_keys.py` | Redis key 与数据库层同属基础设施 |
| `app/core/embedding_service.py` | `app/rag/embedding_service.py` | Embedding 是 RAG 组件 |
| `app/core/memory_manager.py` | `app/memory/backends/vectordb/` | 向量数据库后端实现迁入记忆系统分层 |
| `app/core/chat_history_store.py` | `app/memory/backends/` | 对话存储后端实现 |
| `app/core/context_manager.py` | — | 功能已完全整合至 `app/rag/` 和 `app/memory/` |

全项目 25+ 个文件的导入路径同步更新，无向后兼容垫片。

`app/core/file_storage.py`：`_read_config()` 改为调用 `ConfigLoader(str(PROJECT_ROOT / "config")).get_system_config()`，不再直接 `with open` 读取 YAML。

---

### 2. app/memory/ — 记忆系统抽象层

**新增文件**：

| 文件 | 职责 |
| --- | --- |
| `base.py` | `MemoryBackend` + `RagBackend` 两个 ABC；hermes_engine / RagPipeline 依赖接口而非实现 |
| `factory.py` | 根据环境变量 `MEMORY_BACKEND`（`filesystem` / `vectordb`）选择并实例化后端 |
| `backends/vectordb/memory_manager.py` | 原 `app/core/memory_manager.py` 迁移，实现 `MemoryBackend` |
| `backends/vectordb/chat_history_store.py` | 原 `app/core/chat_history_store.py` 迁移，实现 `ChatHistoryBackend` |
| `backends/filesystem/backend.py` | 纯文件系统存储后端（无 Redis/MySQL/ES 依赖） |

`MemoryBackend` 必须实现：`store_turn` / `get_recent_turns` / `retrieve_memory`，其余方法提供无操作默认实现。

---

### 3. app/scoring/ — 新评分模块

对 Agent 和 Tool 的调用效果进行持续追踪，量化质量评估：

**新增文件**：

| 文件 | 职责 |
| --- | --- |
| `models.py` | `AgentStats` / `ToolStats` / `ScoreWeights` / `AgentScore` / `ToolScore` 数据模型 |
| `algorithm.py` | `compute_agent_score` / `compute_tool_score`：成功率 × 延迟 × 质量 × 调用频率加权计算 |
| `store.py` | `ScoringStore`：基于文件系统的评分数据持久化（`data/scoring/`） |
| `manager.py` | `ScoringManager`：进程级单例；内存写缓冲 + per-key asyncio.Lock + fire-and-forget 持久化 |

**新增 API**（`app/api/scoring_api.py`，路由前缀 `/scoring`）：

| 端点 | 说明 |
| --- | --- |
| `GET  /scoring/agents` | Top-N Agent 综合评分（`?top=10`） |
| `GET  /scoring/agents/{name}` | 指定 Agent 评分详情 + 原始统计 |
| `GET  /scoring/tools` | Top-N Tool 综合评分 |
| `GET  /scoring/tools/{name}` | 指定 Tool 评分详情 + 原始统计 |
| `GET  /scoring/weights` | 查询当前评分权重配置 |
| `PUT  /scoring/weights` | 更新评分权重（支持部分更新） |
| `DELETE /scoring/agents/{name}` | 重置指定 Agent 统计数据 |
| `DELETE /scoring/tools/{name}` | 重置指定 Tool 统计数据 |

---

### 4. app/core/task_planner.py — TaskItem 模型强化 + 向后兼容层清除

#### 4.1 TaskItem 属性私有化 + property 访问器

将 `task_id` / `status` / `priority` / `created_at` / `updated_at` 改为私有属性（`_task_id` 等），通过 property 暴露：

| property | setter 约束 |
| --- | --- |
| `task_id` | 只读，由 `_new_id()` 服务端生成 |
| `status` | 已完成/取消状态不可逆（COMPLETED/CANCELLED → 仅允许取消），自动更新 `_updated_at` |
| `priority` | 已完成/取消状态或负数值时拒绝更新 |

新增 `update(content, priority, status, tags, depends_on)` 方法，统一字段更新入口。

#### 4.2 `to_dict()` / `from_dict()` 修正

旧版用 `asdict(self)` 产生带下划线前缀的键（`_task_id`, `_status` 等），`from_dict` 再用 `cls(**d)` 调用——键名不匹配，导致反序列化崩溃。

修正方案：

- `to_dict()` 手动构造公开键名的 dict（`task_id`, `status`, `priority`, `created_at`, `updated_at`）
- `from_dict()` 先调用 `cls(content, priority, tags, depends_on)` 创建对象，再逐一赋值私有属性

同时移除不再需要的 `asdict` 导入及 `from re import L` / `from turtle import st` 误导入。

#### 4.3 向后兼容层清除

| 删除项 | 说明 |
| --- | --- |
| `TASK_LIST_KEY` / `TASK_LIST_TTL` | 旧单级任务 Redis key，已被 L1/L2 双级键替代 |
| `DECOMPOSE_SYSTEM` / `DECOMPOSE_USER_TMPL` | 通用任务分解提示词，已被 `L1_DECOMPOSE_SYSTEM` / `L2_DECOMPOSE_SYSTEM` 替代 |
| `TaskDecomposer.decompose()` | 旧单级分解方法，使用已删除的提示词常量；`L1Decomposer.decompose` / `L2Decomposer.decompose` 是正式替代 |
| `make_task_planner()` | 已被 `make_l1_store()` / `make_l2_store()` 替代 |
| REDUNDANT[TP01] 注释块 | 三套提示词重叠说明注释，随旧提示词一并删除 |

`TaskStore.__init__` 的 `redis_key` 参数改为必填（不再有默认值），调用方必须通过 `make_l1_store` / `make_l2_store` 工厂函数获取，强制使用分级架构。

---

### 5. 前端：stream_id 精准取消

**修改文件**（`smart_butler-web`）：

| 文件 | 变更 |
| --- | --- |
| `src/types/index.ts` | `SseEventType` 新增 `stream_start`；`SseEvent` 新增 `stream_id?: string` |
| `src/utils/stream.ts` | 模块级 `_activeStreamId`，接收 `stream_start` 事件时赋值；新增 `getActiveStreamId()` 导出 |
| `src/api/chat.ts` | `cancelStream(streamId)` 接受 `stream_id` 参数，POST body 带 `stream_id` |
| `src/views/chat/ChatView.vue` | 取消时调用 `cancelStream(getActiveStreamId())` 精准停止对应流 |

**效果**：同一用户多端并发对话时，「停止」按钮只停止当前客户端的流，不影响其他标签页或设备。

---

## v2.11 — 2026-05-15：API 层精化 + LLM 重试与故障恢复

### 1. Agent 创建/更新：工具可用性校验

**问题**：`create_agent` / `update_agent` 保存工具列表时不验证工具是否存在，导致配置了不存在或无权限工具的 Agent 在运行时报错。

**修复**（`app/api/agents_api.py`）：

- 新增 `_validate_tools(tools, user_id)` 函数，对工具列表中的每个 `tool_name` 查询注册表，不存在或当前用户无权限则统一报错 `HTTP 400`
- 将 `BaseAgent`、`registry`、`tool_registry` 从方法内懒导入提升为模块级导入

---

### 2. 多平台登录：画像缓存保护

**问题**：同一用户从第二台设备登录时，`_load_user_init_data()` 无条件从 MySQL 加载并覆盖 Redis 中已有的画像，导致第一台设备的运行时画像更新丢失。

**修复**（`app/api/auth.py`）：

- `_load_user_init_data()` 先读取 Redis，已有画像则只续期 TTL 跳过 MySQL 加载
- `phonenumbers` 导入提升到模块级（避免高频请求时重复导入开销）

---

### 3. 消息接口：Agent 可用性校验 + LLM 构建简化

**修复**（`app/api/chat.py`）：

| 变更项 | 说明 |
| --- | --- |
| `agent_name` 校验 | `send_message` / `stream_message` 均在处理前验证 `agent_name` 对当前用户可用，不可用返回 `HTTP 400` |
| LLM 构建简化 | 删除 `isinstance(user_model, dict)` 分支，`get_user_model` 已保证返回 `LLMInfo`，统一调用 `engine._build_llm_from_config(user_model)` |
| `cancel_stream` 精确取消 | 引入 `CancelRequest(stream_id)` 模型，按 `stream_id` 而非 `user_id` 发送取消信号，支持同一用户多端并发对话时精准停止指定流 |

---

### 4. 模型加载路径统一

**修复**（`app/api/dependencies.py`）：

- `get_user_model` 重构：删除内联 MySQL 查询，改为调用 `LLMInfo.load(user_id)`，与 hermes_engine 内部逻辑保持一致
- `LLMInfo` 提升为模块级导入

---

### 5. 模型 URL 校验去重 + 版本后缀兼容

**修复**（`app/api/models.py`）：

| 变更项 | 说明 |
| --- | --- |
| 删除 `_assert_valid_url()` | 与 `hermes_engine._validate_llm_url()` 逻辑完全重复；Pydantic validator 改为懒加载导入后者，避免启动时引入 hermes_engine 的重量级依赖 |
| `_test_model` 版本兼容 | 原代码硬编码 `/v1/chat/completions`；改用 `re.search(r"/v\d+(\.\d+)?$", base_url)` 检测 URL 末尾版本号，有则直接拼 `/chat/completions`，否则默认补 `/v1/`；兼容 GLM `/v4`、DeepSeek `/v3` 等非 v1 接口 |

---

### 6. LLM 重试与故障恢复

**背景**：LLM 非 200 响应时引擎直接跳到下一步，既浪费已完成的 pipeline 工作，也无法让用户从中断点续跑。

**修改**（`app/core/hermes_engine.py`）：

#### 6.1 可重试错误类型

模块顶部新增 `_LLM_RETRYABLE_ERRORS` 元组，覆盖：

- `httpx`：`ConnectError` / `ConnectTimeout` / `TimeoutException` / `HTTPStatusError`
- `openai`：`APIConnectionError` / `APITimeoutError` / `APIStatusError`

新增 `_LLMRetryExhausted(RuntimeError)` 异常，携带 `llm_message: str` 属性。

#### 6.2 三处重试循环

| 方法 | 重试策略 |
| --- | --- |
| `_generate_llm_response` | 3 次，失败间隔 2s / 4s，全部失败抛 `_LLMRetryExhausted` |
| `_generate_llm_response_stream` | 同上（非 200 在首个 chunk 前发生，重试安全） |
| `_execute_worker_with_tools` | 同上（`graph.ainvoke` 外层） |

#### 6.3 新增辅助方法

| 方法 | 说明 |
| --- | --- |
| `_fmt_llm_error(exc)` | 解析 HTTP 响应体 JSON（`error.message` → `message` → 原始文本），限 300 字 |
| `_save_llm_failure(user_id, pending_input, completed_content, llm_message)` | 将失败状态写入 Redis key `llm:failure:{user_id}`，TTL 30 分钟 |
| `_load_llm_failure(user_id)` | 一次性读取并删除该 key（防止重复使用） |

#### 6.4 流式路径故障处理

`process_user_input_stream._run()` 三处修改：

1. **开头「继续」检测**：检测到 `user_input in ("继续", "continue")` 且 Redis 有故障状态时，恢复原始输入并注入 `context["_resume_hint"]`（含已完成内容提示）
2. **Pipeline 异常**：`_execute_pipeline()` 用 `try/except _LLMRetryExhausted` + `finally`（确保 consent hook 重置），失败后推送 `llm_failure` SSE 事件并异步保存故障状态
3. **流式 LLM 异常**：stream 生成器内同样捕获 `_LLMRetryExhausted`，保存状态并推送 `llm_failure` 事件后 return

#### 6.5 非流式路径

`process_user_input` 在通用 `except Exception` 之前加 `except _LLMRetryExhausted`，返回 `"LLM 访问失败，{llm_message}"`。

#### 6.6 故障恢复流程

```text
[用户请求] → LLM 3次重试均失败
    ↓
推送 SSE llm_failure { message, hint:"输入「继续」可恢复" }
    ↓
Redis llm:failure:{user_id} 保存 { pending_input, completed_content, failed_reason }
    ↓
用户发送「继续」
    ↓
_load_llm_failure() 读取并删除 key
    ↓
恢复原始 user_input + _resume_hint 注入 context
    ↓
正常走 pipeline 流程，LLM 续跑未完成工作
```

---

## v2.10 — 2026-05-12：工程质量优化（Phase 4）

### 1. Bug 修复：对话授权弹窗重复出现

**问题**：用户在授权弹窗中选择「当前对话允许」后，同一轮对话内后续触发的危险操作仍弹窗。

**根因**：`grant_conversation()` 以 `(tool_name, operation, turn_id)` 为 key 仅授权特定工具的特定操作，未能覆盖本轮全部危险操作。

**修复**（`app/tools/permission.py`）：

- 新增 `_conversation_blanket: Set[str]` 集合，以 `turn_id` 为 key
- 新增 `grant_conversation_all(turn_id)` 方法，一次性放行本轮所有危险操作
- `check_consented()` 在逐 op 检查前优先检测 `turn_id in _conversation_blanket`
- `revoke_conversation()` 同步清除 blanket 集合

**修复**（`app/core/hermes_engine.py`）：`_consent_hook` 中 decision 为 `"conversation"` 时调用 `grant_conversation_all(turn_id)` 而非逐 op 授权。

---

### 2. Bug 修复：对话结束后执行过程不可见

**问题**：消息记录在对话完成后无法展开查看执行过程（Pipeline 面板为空）。

**根因**：`commitStream()` 使用 `{ ...this.pipeline }` 浅拷贝，Pinia 响应式代理中嵌套的 `agents` / `steps` / `tools` 数组在 `this.pipeline = emptyPipeline()` 重置后与拷贝体共享引用而失效。

**修复**（`src/stores/chat.ts`）：`commitStream()` 和 `commitCancelled()` 改用 `JSON.parse(JSON.stringify(...))` 深拷贝，确保历史消息快照独立于 store 状态。

---

### 3. Import 优化

| 位置 | 问题 | 修复 |
| --- | --- | --- |
| `app/utils/log_bus.py` | 3 个方法内各有一行 `from app.utils.progress_bus import push as _pb` | 合并为文件顶部单条导入 |
| `app/core/hermes_engine.py` | `finally` 块重复 `from app.tools.permission import _CONSENT_HOOK, _CONSENT_TURN_ID` | 删除重复导入，统一在 Hook 定义处一次性导入 |

---

### 4. 性能优化：`_is_op_enabled()` 缓存

**问题**：每次工具调用都触发 `_is_op_enabled()` 查询 `dangerous_op_configs` 表，高频工具调用场景下 MySQL 压力明显。

**修复**（`app/tools/permission.py`）：

- 新增 `_op_enabled_cache: Dict[Tuple[str,str], Tuple[bool,float]]` 模块级缓存，TTL 60 秒
- 新增 `_invalidate_op_cache(user_id, operation)` 供外部失效

**修复**（`app/api/tools_api.py`）：`PATCH /tools/dangerous-ops/{op_type}` 成功后调用 `_invalidate_op_cache()`，切换立即生效无需等待 TTL。

---

### 5. 日志增强

**新增方法**（`app/utils/log_bus.py`）：

| 方法 | 颜色 | 说明 |
| --- | --- | --- |
| `conversation_turn()` | 亮紫 (`turn`) | 轮次结束时记录完整摘要：intent、mode、agents 列表、tools 列表、响应长度、总耗时 |
| `consent_decision()` | 亮黄 (`consent`) | 用户授权弹窗决策记录（tool、operation、decision） |

**接入**（`app/core/hermes_engine.py`）：

- `_run()` 起始记录 `_turn_start = time.monotonic()`
- 保存 turn 前调用 `_bus.conversation_turn(...)` 输出轮次摘要
- `_consent_hook` 决策后调用 `_bus.consent_decision(...)`

---

## v2.9 — 2026-05-12：危险操作授权系统

### 1. 设计目标

工具执行危险操作（修改文件、删除数据、网络请求等）时，在 SSE 流式场景下**暂停工具执行**，向前端推送授权事件，等待用户决策后恢复，避免危险操作在用户未确认时静默执行。

### 2. 数据库变更（`app/database/agent_db.sql`）

新增 `dangerous_op_configs` 表，用于用户级别危险操作类型开关：

```sql
CREATE TABLE IF NOT EXISTS `dangerous_op_configs` (
  `id`         BIGINT AUTO_INCREMENT PRIMARY KEY,
  `user_id`    VARCHAR(36) NOT NULL,
  `op_type`    VARCHAR(50) NOT NULL,
  `is_enabled` TINYINT(1)  NOT NULL DEFAULT 1,  -- 1=需授权 0=跳过授权
  `created_at` DATETIME    DEFAULT NOW(),
  UNIQUE KEY uq_user_op (`user_id`, `op_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 3. 后端核心修改

#### `app/tools/permission.py` — ConsentManager 扩展

| 变更项 | 说明 |
| --- | --- |
| 新增 `CONSENT_CONVERSATION` 常量 | 当前轮次全部允许 |
| `_conversation_cache` | `(tool_name, op, turn_id)` 键，轮内缓存 |
| `ContextVar _CONSENT_HOOK` / `_CONSENT_TURN_ID` | 由 HermesEngine 注入，流式场景下提供 hook 和 turn_id |
| `_is_op_enabled()` | 查询 `dangerous_op_configs`，用户关闭则直接放行 |
| `grant_conversation()` / `revoke_conversation()` | 轮次粒度授权管理 |
| `check_consented()` 优先级链 | op_disabled > once > conversation > session > project/always |

#### `app/tools/base.py` — `_wrapped_execute` 集成 Hook

- 检测到未授权操作时，若 `consent_hook` 存在则调用 hook（SSE 流式：推送事件 + await Future）
- hook 返回 `"allow"` / `"deny"` / `"conversation"` 后继续执行或返回拒绝结果

#### `app/core/hermes_engine.py`

| 变更项 | 说明 |
| --- | --- |
| `self._consent_futures` | `Dict[str, asyncio.Future]`，暂存等待中的授权 Future |
| `_consent_lock` | 串行化并发授权请求，同一时刻只有一个弹窗 |
| `_consent_hook` 闭包 | 推送 `consent_required` SSE 事件 + `asyncio.wait_for`（300s 超时） |
| `consent_respond()` | `request_id` → resolve Future，恢复工具执行 |

#### 新增 API 端点

| 端点 | 说明 |
| --- | --- |
| `GET  /tools/dangerous-ops` | 查询用户危险操作类型开关状态（含标签描述） |
| `PATCH /tools/dangerous-ops/{op_type}` | 开启/关闭指定危险操作类型授权 |
| `POST /chat/consent` | 用户决策提交（`request_id` + `decision`） |

### 4. 前端核心修改

#### 新增组件

| 文件 | 说明 |
| --- | --- |
| `src/components/ConsentDialog.vue` | 授权弹窗：显示操作详情，提供拒绝 / 允许 / 当前对话允许三个选项，调用 `POST /chat/consent` |
| `src/views/tools/DangerousOpsSettings.vue` | 危险操作配置页：列出全部操作类型，toggle 开启/关闭授权 |

#### 现有组件修改

| 文件 | 变更 |
| --- | --- |
| `src/views/tools/ToolsView.vue` | 添加 Tab 切换（工具列表 / 危险操作配置），嵌入 `DangerousOpsSettings` |
| `src/views/chat/ChatView.vue` | 接收 `consent_required` SSE 事件，弹出 `ConsentDialog` |
| `src/utils/stream.ts` | 新增 `ConsentRequestData` 接口，解析 `consent_required` 事件 |
| `src/api/tools.ts` | 新增 `listDangerousOps()` / `toggleDangerousOp()` |
| `src/api/chat.ts` | 新增 `respondConsent()` |
| `src/types/index.ts` | 新增 `DangerousOpStatus` 接口；`SseEventType` 加入 `consent_required` |

### 5. 流程图

```text
工具触发危险操作
    ↓
check_consented() → 未授权
    ↓
consent_hook 推送 SSE "consent_required"
    ↓
前端弹出 ConsentDialog（暂停工具执行）
    ↓
用户选择 ────────────────────────────┐
  允许（allow）                       │
  拒绝（deny）                        │
  当前对话允许（conversation）         │
    ↓                                 │
POST /chat/consent ───────────────────┘
    ↓
consent_respond() resolve Future
    ↓
工具恢复执行 / 返回拒绝结果
```

---

## v2.8 — 2026-05-09：Scrapling 集成 + cli_exec 增强 + 冗余代码审查

### 1. Scrapling 集成（web_agent）

**目标**：为 WebAgent 引入 Scrapling 框架，提升网络检索的速度、成功率和数据精准性。

**修改文件**：`app/agents/workers/web_agent.py`（+301 行，1019 → 1320）

| 变更项 | 详情 |
| --- | --- |
| 新增 Scrapling 导入 | `try/except` 优雅降级，`_SCRAPLING` 标志控制路径切换 |
| `_scrap_l1()` | curl_cffi + TLS 指纹伪装，速度最快，适合无 Cloudflare 的站点 |
| `_scrap_l2()` | Chromium + Cloudflare bypass（StealthyFetcher），适合强防护站点 |
| `_scrap_l3()` | Playwright JS 渲染（DynamicFetcher），适合 SPA/动态页面 |
| `_fetch()` 4 层降级 | scrapling-basic → scrapling-stealth → scrapling-dynamic → playwright(legacy) |
| `web_fetch` / `web_batch_fetch` | 新增 `stealth` 参数，透传给 `_fetch()` |
| 新工具 `web_smart_extract` | 基于 Scrapling 自适应 CSS 选择器（`auto_save=True` + `adaptive=True`），精准提取结构化数据，VIS_EXCLUSIVE |
| 系统提示词 | 新增决策树：首选 `web_smart_extract`，降级 `web_fetch`，stealth 场景自动判断 |

**requirements.txt**：添加 `scrapling[all]`。

---

### 2. cli_exec 工具增强

**目标**：支持结果断言，方便在 Agent 流水线中做自动化测试和验证。

**修改文件**：`app/tools/builtin/cli_exec.py`

| 变更项 | 详情 |
| --- | --- |
| 新增输入参数 `expected` | 可选字符串，默认 `None`；非空时对 stdout 执行子串断言 |
| 新增输出字段 `status` | `"pass"` / `"fail"`，`expected=None` 时恒为 `"pass"` |
| 新增输出字段 `result` | 原始 stdout 内容 |
| 新增输出字段 `log` | 执行信息（OS / Shell / exit_code / 耗时 / stderr / 断言结果） |
| 断言格式 | `[断言] 期望包含: 'xxx' → 匹配 ✓ / 不匹配 ✗` |

---

### 3. 冗余代码审查（Karpathy 视角）

对全项目进行系统性审查，在源码中用 `# REDUNDANT[xx]:` 标记注释标注关键冗余点，保持代码可运行（只标注、不重构）。

**标注汇总**：

| 编码 | 位置 | 问题描述 |
| --- | --- | --- |
| WA01 | `workers/data_analyst.py` 等 5 个 Worker | `execute()` 骨架相同（~70%），绕过 `BaseAgent.execute()` 的 `collect_tools / _invoke_with_tools / L2 拆分` |
| WA02 | 同上 + `skill_builder.py` + `summarizer.py` | `hasattr(result, "content")` 为死代码，LangChain ChatModel 始终返回 `AIMessage.content` |
| WA03 | `skill_builder.py` `_do_generate/_do_optimize` | 两个方法骨架相同（~60%），LLM 调用 + 文件写入 + 返回结构重复 |
| RA01 | `router.py` 7 个方法 | `ainvoke → _strip_fence → json.loads` 三行模式重复 7 次 |
| RA02 | `router.py` 同上 | `getattr(resp, "content", str(resp))` 同 WA02，fallback 为死代码 |
| HE01 | `hermes_engine.py` `RegistryToolAdapter` | 与 `base.py` 的 `_RegistryToolAdapter` 功能重复，且缺少 log_bus 事件集成 |
| DG01 | `decision_gate.py` 模块级 dict | `_pending_events/_pending_results` 单进程内存，多进程部署或重启后挂起状态丢失 |
| TP01 | `task_planner.py` 提示词区 | 3 套任务分解提示词（`DECOMPOSE_SYSTEM` / `_DECOMPOSE_SYSTEM` in router / `L1_DECOMPOSE_SYSTEM`）语义重叠，随版本独立漂移 |

**修复建议**（待后续实施）：

- WA01：`BaseAgent` 增加 `_build_context_messages(context)` 钩子，Worker 只需实现该钩子
- RA01：`RouterAgent` 提取 `_invoke_json(messages, fallback, llm)` 辅助方法
- HE01：`hermes_engine.py` 直接 import 并复用 `_RegistryToolAdapter`
- DG01：以 Redis Streams / pub/sub 替换模块级 dict，key = `decision:{id}`，TTL = 330s

---

## v2.7 — 2026-05-06：客户端环境追踪 + RAG 模块独立提取

### 1. 客户端环境追踪（Task 1）

**目标**：记录客户端类型/版本，将其随上下文传递给子 Agent，注入模型系统提示，供工具调用路由使用。

**新增文件**：

- `app/core/client_env.py`：`ClientType` 枚举（14 种平台），`normalize_client_type()`（别名归一化：feishu→lark / mac→macos），`format_env_for_prompt()` 生成 `<client-env>` XML 块

**修改文件**：

| 文件 | 变更要点 |
| --- | --- |
| `app/api/auth.py` | `UserLogin` 新增 `client_type` / `client_version`，登录时写入 Redis session |
| `app/api/chat.py` | `ChatMessage` 新增 `client_type` / `client_version`，注入 `context["_client_type"]` / `context["_client_version"]`（同步+流式路径） |
| `app/core/hermes_engine.py` | `process_user_input/stream` 预加载 `_user_profile`；`turn_metadata` 记录 `client_type/version`；`_generate_llm_response/stream` 将 `<client-env>` 追加到基础系统提示 |
| `app/agents/base.py` | `_build_system_prompt(context)` 从 context 读取 `_client_type` / `_user_profile`，追加 `<client-env>` 和用户画像到子 Agent 系统提示 |

**数据流**：前端登录/请求携带 `client_type` → `context["_client_type"]` → `hermes_engine` 预加载画像 → `_build_system_prompt()` 拼入系统提示 → LLM 感知客户端环境

---

### 2. RAG 模块独立提取（Task 2）

**目标**：将散落在 `context_manager.py` / `embedding_service.py` / `memory_manager.py` / `vector_store.py` 中的 RAG 逻辑集中到独立的 `app/rag/` 包，对外暴露单一入口 `RagPipeline`，方便后续独立优化。

**新增文件（`app/rag/` 包，7 个）**：

| 文件 | 职责 |
| --- | --- |
| `__init__.py` | 暴露 `RagPipeline`、`RagContext` |
| `types.py` | `RagContext` 数据类（替代 `ContextBundle`），含 `to_prompt_context()` |
| `chunker.py` | `Chunk` 数据类 + `TurnChunker`（从 `EmbeddingService` 提取的切片逻辑） |
| `formatter.py` | `sanitize_memory_content` / `build_memory_context_block` / `format_memories`（从 `ContextManager` 提取） |
| `retriever.py` | `HybridRetriever`：预取缓存（GETDEL）→ 向量 KNN → BM25 全文三步检索 + 相关性过滤 |
| `indexer.py` | `TurnIndexer`：`index_turn` / `revectorize` / `delete_all_indices`（封装 `VectorStore`） |
| `pipeline.py` | `RagPipeline`：统一门面，`build_context` / `index_turn` / `revectorize` / `delete_all_vector_indices` / `queue_prefetch` |

**修改文件**：

| 文件 | 变更要点 |
| --- | --- |
| `app/core/embedding_service.py` | 移除 `Chunk`/`TurnChunker` 实现，`chunk_turn()` 保留为向后兼容委托；新增 `from app.rag.chunker import Chunk, TurnChunker` |
| `app/core/context_manager.py` | 重写为薄封装层；`ContextBundle = RagContext` 别名；`build_context()` 委托 `RagPipeline`，未注入时用 `HybridRetriever` 内联兜底 |
| `app/core/memory_manager.py` | 移除 `store_turn()` 内的 `vector_store.store_turn_vectors()` 调用（由 `HermesEngine` 通过 `RagPipeline.index_turn()` 触发） |
| `app/core/hermes_engine.py` | 新增 `rag_pipeline` 字段和 `set_rag_pipeline()`；两条处理路径均用 `_rag_source = rag_pipeline or context_manager`；`store_turn()` 后后台 `index_turn()`；预取改走 `rag_pipeline.queue_prefetch()` |
| `app/api/chat.py` | `/admin/revectorize` 改用 `rag_pipeline.revectorize()`，从 `app.state.rag_pipeline` 获取 |
| `main.py` | step 5b 创建 `RagPipeline` 并注入 `hermes_engine`；`validate_embedding_config` 新增 `rag` 参数，优先调用 `rag.revectorize()`；`app.state.rag_pipeline` 挂载；移除冗余的 `context_manager.set_vector_store()` |

**向后兼容保证**：`ContextBundle`、`EmbeddingService.chunk_turn()`、`ContextManager.build_context()` 均保留，现有调用方无需修改。

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

## 代码规模（截至 v2.10）

| 模块 | 估算行数 | 备注 |
| --- | --- | --- |
| app/core | ~4,200 行 | hermes_engine 新增 consent 逻辑 |
| app/agents | ~3,150 行 | |
| app/api | ~2,700 行 | chat + tools_api 新增 3 个端点 |
| app/rag | ~650 行 | |
| app/database | ~2,150 行 | |
| app/tools | ~1,900 行 | permission.py 大幅扩展 |
| app/scheduler | ~1,050 行 | |
| app/sandbox | ~590 行 | |
| app/utils | ~350 行 | log_bus 新增 2 个方法 |
| main.py | ~460 行 | |
| **总计** | **~17,200+ 行** | |

---

## 待实现功能

### 高优先级

| 功能 | 说明 |
| --- | --- |
| 归档作业表建表 | memory_monthly_jobs / memory_yearly_jobs 需在 create_tables.py 添加 DDL |
| dangerous_op_configs 建表 | 需在 create_tables.py 添加 DDL（v2.9 新增表） |
| Worker Agent 工具调用 | data_analyst / customer_support / code_assistant 接入真实工具 |

### 中优先级

| 功能 | 说明 |
| --- | --- |
| revectorize 保留 agent 结构 | 全量重建时历史 turn 无 agent_outputs 字段（TurnIndexer 尚未从 ES 读取原始 agent_outputs） |
| RAG 重排序（reranker） | HybridRetriever 当前仅评分过滤，可接入 cross-encoder reranker 提升召回精度 |
| PII 脱敏接入 | _desensitize() 仅清空字段 |
| Agent 间结构化消息协议 | 串行流水线靠 context["prev_result"] 传递，无正式 Schema |

### 低优先级

| 功能 | 说明 |
| --- | --- |
| API 速率限制 | slowapi 或自定义中间件 |
| Docker Compose 编排 | 一键启动全服务 |
| Prometheus 指标导出 | /metrics 端点 |
| LangSmith 全链路追踪 | 未接入 LangSmith / Arize Phoenix |
