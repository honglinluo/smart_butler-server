# Hermes Multi-Agent System — 项目完成说明

**更新日期**: 2026-05-09
**版本**: 2.8
**状态**: ✅ 核心系统已就绪，持续迭代中

---

## 一、项目总体概况

Hermes Multi-Agent System 是一套企业级多智能体协作平台，基于 LangChain + LangGraph 实现，
支持多用户隔离、三层记忆管理、语义向量检索、动态 Agent 编排、定时任务调度、安全沙箱执行、
工具构建决策门控与技能记忆积累。

---

## 二、各模块完成状态

### 2.1 接入层 (app/api)

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| 用户注册/登录/Token | ✅ | Bearer Token 认证 |
| LLM 模型配置 CRUD | ✅ | 绑定到 MySQL llms 表 |
| 聊天消息接口（同步） | ✅ | `/chat/send`，支持指定 Agent |
| 聊天消息接口（SSE流式） | ✅ | `/chat/stream`，Server-Sent Events |
| 对话历史查询 | ✅ | `/chat/history`，从 ES 分页返回 |
| 文件/内容上传 | ✅ | `/chat/upload`，多格式，沙箱处理 |
| 重向量化触发 | ✅ | `/chat/revectorize` |
| Agent CRUD | ✅ | DB Agent 无需代码，API 创建 |
| Agent 评分 | ✅ | 低评分告警日志 |
| Agent 热重载 | ✅ | `/agents/reload` 无需重启 |
| 工具查询与创建 | ✅ | `/tools` GET + POST，三来源可见性控制 |
| 工具更新与删除 | ✅ | `/tools/{id}` PUT + DELETE |
| 定时任务管理 | ✅ | `/scheduler` 完整 CRUD + 日志 + 通知 SSE |
| 工具构建决策门控 | ✅ | `/decisions` 挂起/确认/策略配置 |
| 速率限制 | 🔧 | 未实现 |

### 2.2 核心引擎 (app/core)

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| LangChain 多模型支持 | ✅ | OpenAI/Anthropic/Gemini/本地模型 |
| LangGraph ReAct Agent | ✅ | 工具调用链 + 步骤提取 |
| single/serial/parallel 编排 | ✅ | 3 种流水线模式 |
| Redis L1 近期记忆 | ✅ | LPUSH+LTRIM，TTL 30 天 |
| ES L3 全量存档 | ✅ | 触发式批量同步，Redis 锁防并发 |
| 向量检索 + 全文检索混合 | ✅ | 去重合并，top_k=3 |
| 记忆预取 | ✅ | queue_prefetch + get_prefetched_context，ES 空时降级 Redis |
| 用户画像注入 | ✅ | build_system_prompt_block() 生成 XML 块追加系统提示 |
| 委托链记录 | ✅ | on_delegation() 写 Redis 列表，定长 20 条 |
| Embedding 模型变更检测 | ✅ | 启动时对比 MySQL vs 配置，自动重建 |
| Ollama NaN 容错 | ✅ | 三层降级（预处理→原生 API→截半重试） |
| 分 Agent 向量化 | ✅ | 每个 Agent 输出独立 chunk 存储 |
| 上下文相关性过滤 | ✅ | `confidence_threshold` 过滤低分记忆 |
| /models/change 清除 LLM 缓存 | ✅ | 模型切换后立即生效 |
| Prompt 模板外置 | ✅ | 从 config/templates/ 加载 |
| 项目根路径统一管理 | ✅ | PROJECT_ROOT 环境变量 + app/core/paths.py |
| 程序关闭时画像固化 | ✅ | Redis 用户画像批量写入 MySQL |
| 记忆压缩（摘要归档） | ✅ | 8 节 XML 结构，全替换，不保留原始 turn |
| 客户端环境追踪 | ✅ | ClientType 枚举 14 种平台，`<client-env>` 注入子 Agent 系统提示 |
| RAG 模块独立提取 | ✅ | app/rag/ 包：RagPipeline 统一门面，HybridRetriever/TurnIndexer/TurnChunker 独立 |
| revectorize 保留 agent 结构 | 🔧 | 全量重建时历史 turn 无 agent_outputs 字段 |

### 2.3 Agent 系统 (app/agents)

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| BaseAgent 基类 | ✅ | 代码/装饰器/DB 三种创建方式 |
| 技能记忆系统 | ✅ | 自动积累，成功率滚动更新 |
| RouterAgent 意图识别 | ✅ | LLM 驱动，JSON 格式约束 |
| 任务分解 | ✅ | 子任务列表，支持多任务 |
| 串行步骤质量校验 | ✅ | 不通过时中断流水线 |
| Registry 全局注册表 | ✅ | 内存缓存，支持热重载 |
| BaseAgent 背景模板外置 | ✅ | config/templates/{name}.txt |
| MemoryArchiver 系统 Agent | ✅ | 摘要归档、画像提取、向量化 |
| MonthlyArchiverAgent | ✅ | 月度归档，Saga 三步，MySQL 作业防重复 |
| YearlyArchiverAgent | ✅ | 年度归档，Saga 三步，MySQL 作业防重复 |
| AgentEventLoop | ✅ | 事件循环，支持运行时动态构建工具，最多 6 次迭代 |
| UserDecisionGate | ✅ | allow/ask/deny 三策略，ask 时挂起等待用户确认 |
| ToolBuilder | ✅ | Code Agent 运行时生成工具，热加载到 registry |
| LoopLogger | ✅ | 事件循环调用日志（Redis List + 标准日志） |
| Worker Agent 工具调用 | 🔧 | 框架就绪，data_analyst 等需接入真实工具 |

### 2.4 工具系统 (app/tools)

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| BaseTool 基类 | ✅ | 三来源（code/user/agent）+ 可见性 + 危险操作声明 |
| 工具注册表 | ✅ | 全局单例，支持热加载 |
| ConsentManager | ✅ | 授权核查（once/session/project/always） |
| 工具动态加载 | ✅ | loader.py 支持三来源统一加载 |
| file_reader 内置工具 | ✅ | 17 类格式，敏感信息自动遮盖 |
| file_writer 内置工具 | ✅ | 支持 create/append/overwrite 三模式 |
| cli_exec 内置工具 | ✅ | 命令执行 + expected 断言，status/result/log 三字段输出 |
| web_smart_extract 专属工具 | ✅ | Scrapling 自适应 CSS 选择器，VIS_EXCLUSIVE（web_agent） |
| 工具函数实现 | 🔧 | sql_query 等业务工具未实现 |

### 2.5 安全沙箱 (app/sandbox)

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| subprocess 隔离执行 | ✅ | 独立临时目录，执行后自动清理 |
| 强制超时 | ✅ | asyncio.wait_for 控制（默认 10s） |
| 资源限制 | ✅ | Linux resource 模块限制 CPU/内存 |
| 静态高危扫描 | ✅ | scanner.py 拦截 os.system、eval 等 15+ 模式 |
| 多语言支持 | ✅ | Python 实际执行，Shell/Node 仅语法检查 |

### 2.6 定时任务 (app/scheduler)

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| 7 种任务类型 | ✅ | once/daily/weekly/monthly/workday/weekend/cron |
| 中国法定节假日 | ✅ | 2025-2026 静态集合，含调休补班 |
| Cron 自实现解析器 | ✅ | 5 字段，支持 `*` / 范围 / 步长 / 列表 |
| MySQL 持久化 | ✅ | scheduled_tasks + task_run_logs |
| Redis 通知队列 | ✅ | RPUSH/LPOP，TTL 24h |
| SSE 通知流 | ✅ | 3s 轮询，200 轮无通知自动关闭 |
| 系统级不可见任务 | ✅ | user_id="`__system__`"，用户 API 过滤 |
| 系统任务注册 | ✅ | register_system_tasks()，幂等，月度/年度归档 cron |

### 2.7 数据库层 (app/database)

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| MySQL 连接池 | ✅ | SQLAlchemy，心跳检测 |
| Redis 连接池 | ✅ | 列表操作，多 DB，scan_keys |
| ES 连接池 | ✅ | KNN 向量搜索，按条件删除 |
| 连接排队机制 | ✅ | 超限自动排队，超时报错 |

---

## 三、更新历史

### v2.8 — 2026-05-09：Scrapling 集成 + cli_exec 增强 + 冗余代码审查

- `web_agent.py` 集成 Scrapling（curl_cffi / StealthyFetcher / DynamicFetcher），新增 4 层降级链和 `web_smart_extract` 工具
- `cli_exec.py` 新增 `expected` 断言参数，输出 `status/result/log` 三字段
- 全项目冗余代码审查（8 类 REDUNDANT 标注），无破坏性改动

---

### v2.7 — 2026-05-06：客户端环境追踪 + RAG 模块独立提取

#### 1. 客户端环境追踪

新增 `app/core/client_env.py`（`ClientType` 枚举 14 种平台、`normalize_client_type`、`format_env_for_prompt`）。
`auth.py` / `chat.py` 接收 `client_type` / `client_version` 字段，写入 turn_metadata 和 context 字典；
`hermes_engine.py` + `base.py` 将 `<client-env>` 块注入 LLM 系统提示，供工具调用决策使用。

#### 2. RAG 模块独立提取

新增 `app/rag/` 包（7 文件）：`RagPipeline`（门面）/ `HybridRetriever`（三步检索）/ `TurnIndexer`（向量索引）/
`TurnChunker`（切片）/ `RagContext`（数据类）/ `formatter`（记忆格式化）。
`ContextManager` / `EmbeddingService` / `MemoryManager` 重构为薄封装，保持向后兼容。
`main.py` step 5b 创建并注入 `RagPipeline`；`/admin/revectorize` 端点迁移至 `rag_pipeline.revectorize()`；
`hermes_engine` 两条路径（同步/流式）均在 `store_turn()` 后后台触发 `index_turn()`。

---

### v2.6 — 2026-05-05：Bug 修复

#### 1. LLMInfo 导入错误修复（chat.py）

**问题**：`app/api/chat.py` 第 52、116、279 行调用 `engine.LLMInfo(...)` 报
`AttributeError: 'HermesEngine' object has no attribute 'LLMInfo'`。

**根因**：`LLMInfo` 是 `app/core/hermes_engine.py` 的模块级数据类，不是 `HermesEngine` 实例属性。

**修复**：在 `chat.py` 导入区添加 `from app.core.hermes_engine import LLMInfo`，
将三处 `engine.LLMInfo(...)` 改为 `LLMInfo(...)`。

#### 2. 记忆预取空写入修复（memory_manager.py）

**问题**：`memory:{user_id}:prefetch_result` Redis key 始终未被写入。

**根因（双层缺陷）**：

- `_run_prefetch()` 完全依赖 `retrieve_memory()`（ES + 向量检索）。对新用户或早期对话，ES 尚未同步，`retrieve_memory` 返回空列表，`_run_prefetch` 直接 `return`，key 永不写入。
- 即使 ES 有数据，`_sync_recent_to_es` 写入时使用 `refresh=False`，新文档要等约 1 秒才可搜索，与 `_run_prefetch` 存在竞态条件。

**修复**：在 `_run_prefetch()` 中，`retrieve_memory` 返回空时补充 Redis 近期对话（`get_recent_turns`）作为 fallback，确保 ES 未就绪时预取缓存仍能写入。

---

### v2.5 — 2026-04-29：记忆系统深度优化 + 月度 / 年度归档 + 系统级定时任务

- hermes-agent 记忆功能移植（on_delegation / 用户画像注入 / 背景预取）
- Summarizer 完整重写：8 节 XML 结构，全替换上下文
- MonthlyArchiverAgent + YearlyArchiverAgent，Saga 三步归档
- 系统级定时任务框架（ActionType.SYSTEM，register_system_tasks）

### v2.4 — 2026-04-28：定时任务系统 + 文件读取工具 + 敏感信息遮盖

- app/scheduler/ 完整实现（7 种任务类型、法定节假日、SSE 通知）
- file_reader.py（17 类格式 + 三套正则敏感信息遮盖）
- requirements.txt 新增 pypdf / python-docx / openpyxl / python-pptx

### v2.3 — 2026-04-27：项目根路径统一管理

- 新增 app/core/paths.py，PROJECT_ROOT 环境变量统一入口
- 所有内部模块改为 `from app.core.paths import PROJECT_ROOT`

### v2.2 — 2026-04-26：多项功能补全与模板体系建设

- /models/change 后清除 LLM 缓存
- Prompt 模板全面外置至 config/templates/
- 程序关闭时用户画像批量固化

### v2.1 — 2026-04-25：日志统一与文档补全

- 日志级别统一从 system_config.yaml 读取
- 模块文档补全

### v2.0 — 2026-04-24：向量切片升级 + Ollama NaN 修复

- 用户问题/回复/Agent 输出独立 chunk 存储
- Ollama NaN bug 三层容错降级

### v1.0 — 2026-04-21：核心系统初建

- LangGraph ReAct Agent、三层记忆架构、RouterAgent、技能记忆、DB Agent

---

## 四、待实现功能

### 高优先级

| 功能 | 说明 |
| --- | --- |
| memory_monthly_jobs / memory_yearly_jobs 建表 | create_tables.py 中需添加 DDL |
| Worker Agent 工具调用 | data_analyst / customer_support / code_assistant 接入真实工具 |

### 中优先级

| 功能 | 说明 |
| --- | --- |
| revectorize 保留 agent 结构 | 全量重建时历史 turn 无 agent_outputs 字段（TurnIndexer 需从 ES 读取原始结构） |
| RAG 重排序（reranker） | HybridRetriever 当前仅评分过滤，可接入 cross-encoder reranker 提升精度 |
| PII 脱敏完整实现 | _desensitize() 仅清空字段，未接入脱敏库 |
| Agent 间结构化消息协议 | 串行流水线靠 context["prev_result"] 传递，无正式 Schema |

### 低优先级

| 功能 | 说明 |
| --- | --- |
| 速率限制 | API 层缺少限流（slowapi 或自定义中间件） |
| Redis 配置缓存 | 每次请求重新读 YAML |
| Prometheus 指标导出 | 缺少 /metrics 端点 |
| Docker Compose 编排 | 一键启动全服务 |
| LangSmith 全链路追踪 | 未接入 LangSmith / Arize Phoenix |

---

## 五、快速验证

```bash
# 语法检查（核心模块 + RAG 包）
python3 -m py_compile main.py \
  app/core/hermes_engine.py app/core/memory_manager.py \
  app/core/embedding_service.py app/core/vector_store.py \
  app/core/context_manager.py app/core/client_env.py app/core/paths.py \
  app/rag/__init__.py app/rag/pipeline.py app/rag/retriever.py \
  app/rag/indexer.py app/rag/chunker.py app/rag/formatter.py app/rag/types.py \
  app/agents/loop/event_loop.py app/agents/loop/decision_gate.py \
  app/agents/system/monthly_archiver.py app/agents/system/yearly_archiver.py

# 无数据库测试
python3 tests/test_basic.py

# 健康检查（服务启动后）
curl http://localhost:8000/health
```

---

## 六、代码规模统计

| 模块 | 估算行数 |
| --- | --- |
| app/core | ~4,100 行（含 client_env.py） |
| app/agents | ~3,150 行（含 loop/、monthly/yearly archiver） |
| app/api | ~2,500 行（含 tools_api、scheduler_api、decision_api） |
| app/rag | ~650 行（新增，7 文件） |
| app/database | ~2,150 行 |
| app/scheduler | ~1,050 行 |
| app/tools | ~1,600 行（含 file_reader） |
| app/sandbox | ~590 行 |
| main.py | ~460 行 |
| **总计** | **~16,250+ 行** |
