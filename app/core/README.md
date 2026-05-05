# app/core — 核心模块文档

本目录包含 Hermes Multi-Agent System 的核心业务逻辑，负责编排引擎、记忆管理、向量化、上下文组装等关键能力。

## 目录结构

```text
app/core/
├── __init__.py
├── paths.py              # 项目根路径常量（PROJECT_ROOT 环境变量统一入口）
├── config_loader.py      # YAML 配置加载器
├── redis_keys.py         # Redis Key 模板常量集中管理
├── hermes_engine.py      # 主编排引擎（LangChain + LangGraph）
├── memory_manager.py     # 多层记忆管理（Redis / MySQL / ES）
├── vector_store.py       # ES 向量索引管理
├── embedding_service.py  # OpenAI 兼容 Embedding 服务
├── context_manager.py    # LLM 调用前的上下文组装器
├── content_fetcher.py    # 外部内容抓取
├── crypto.py             # 加解密工具
└── chat_history_store.py # ES 聊天历史存储
```

---

## 模块说明

### paths.py — 项目根路径常量

提供全局 `PROJECT_ROOT: Path` 常量，消除各模块独立推导 `Path(__file__).parent...` 链的问题。

```python
from app.core.paths import PROJECT_ROOT
tpl = PROJECT_ROOT / "config" / "templates" / "router_system.txt"
```

优先读取 `PROJECT_ROOT` 环境变量（由入口文件在最顶部设置），兜底用 `__file__` 三级向上计算。

---

### hermes_engine.py — Hermes 编排引擎

系统核心，协调 LLM、Agent、工具和记忆的全流程。

#### 核心数据类

```python
InputMessage(user_id, content, role)   # 用户/系统输入消息
OutputMessage(user_id, content, role)  # AI 输出消息
LLMInfo(user_id, url, api_key, ...)    # LLM 配置（从 MySQL 加载）
```

`LLMInfo` 是模块级 dataclass，通过 `from app.core.hermes_engine import LLMInfo` 导入使用，
不是 `HermesEngine` 实例属性。

#### HermesEngine 主要流程

```text
用户输入
  → ContextManager.build_context()          # 加载历史 + 检索记忆
  → RouterAgent.process()                   # 意图识别 + 任务分解 + 流水线规划
  → _execute_pipeline()                     # single / serial / parallel
    → AgentEventLoop.run()                  # 事件循环（工具构建门控）
  → chat_history.save_turn()                # 写入 ES
  → memory_manager.store_turn()             # 写入 Redis + 触发向量化
  → memory_manager.queue_prefetch()         # 后台预取下一轮记忆
```

#### 执行模式

| 模式 | 说明 |
| --- | --- |
| `single` | 单 Agent 处理 |
| `serial` | 多 Agent 串行，每步结果传入下步，每步输出独立向量化 |
| `parallel` | 多 Agent 并行，asyncio.gather 合并结果 |

---

### memory_manager.py — 多层记忆管理器

实现三层记忆架构：

| 层级 | 存储 | 作用 |
| --- | --- | --- |
| L1 | Redis | 最近 N 轮对话（默认 10 条），快速加载上下文 |
| L2 | MySQL | `memory_references` 表引用计数，用于长期管理 |
| L3 | ES | 全量对话存档 + 全文/向量检索 |

#### 关键接口

```python
await store_turn(user_id, turn_id, user_input, assistant_response,
                 metadata=None, agent_outputs=None)
# 写入 Redis → 触发 ES 同步（后台）→ 触发向量化（后台）→ MySQL 引用初始化

await retrieve_memory(user_id, query, top_k=3)
# 向量检索 + 全文检索，去重合并，最多返回 3 条

await get_recent_turns(user_id) → List[dict]
# 从 Redis 加载最近 N 轮（按时间升序）

queue_prefetch(user_id, query)
# 后台异步预取：retrieve_memory 有结果则写入 Redis；
# ES 为空时降级到 get_recent_turns 取最近 N 条

await get_prefetched_context(user_id) → List[dict]
# 原子 GETDEL 读取并删除预取结果（读后即清，避免陈旧）

await on_delegation(user_id, agent_name, task, result)
# 记录 Agent 委托链到 Redis 列表（定长 20 条）

await build_system_prompt_block(user_id) → str
# 读取 Redis 用户画像，生成 <user-profile> XML 块注入系统提示
```

#### 触发机制

- **ES 同步**：Redis 列表长度 ≥ `es_sync_threshold_pct × redis_recent_turns`（默认约 6 条）时后台同步
- **压缩触发**：累计轮次 ≥ `compression.trigger_message_count`（默认 30）时触发

#### 预取机制（v2.5 新增，v2.6 修复）

`queue_prefetch` → `_run_prefetch`（背景任务）：

1. 调用 `retrieve_memory(user_id, query)` 搜索 ES + 向量
2. ES 有结果 → 写入 `memory:{user_id}:prefetch_result`（TTL 5 分钟）
3. ES 为空（新用户/未同步）→ **fallback** 到 `get_recent_turns()` 取最后 `retrieval_top_k` 条写入

---

### context_manager.py — 上下文组装器

在每次 LLM 调用前组装完整上下文：

```python
bundle = await context_manager.build_context(user_id, user_input, base_context)
# 返回 ContextBundle:
#   .history      最近 N 轮 Redis 对话
#   .memories     通过相关性过滤的历史检索结果
#   .memory_text  格式化后注入提示词的记忆文本
#   .base_context 调用方原始 context
```

#### 记忆检索优先级（_retrieve_memories）

```text
Step 0: GETDEL 消费 Redis 预取缓存（命中则跳过后续检索）
Step 1: VectorStore.search()（语义向量检索）
Step 2: MemoryManager.retrieve_memory()（BM25 全文检索，补足剩余名额）
```

#### 相关性过滤

通过 `retrieval.confidence_threshold`（默认 0.7）过滤低分记忆。

---

### vector_store.py — ES 向量索引

管理独立于聊天历史的向量索引，索引名格式：`{es_prefix}_v_{user_id}`

#### 索引文档结构

```text
chunk_id        {turn_id}_q0 / _a0 / _{agent}_0
chunk_text      向量化文本片段
chunk_type      question | answer | agent_output
agent_name      产生该块的 Agent 名称
ref_doc_id      关联聊天历史 turn_id
{vector_field}  dense_vector
```

---

### embedding_service.py — Embedding 服务

OpenAI 兼容格式，支持 Ollama 本地和在线服务。

#### 切片策略（chunk_turn）

| 类型 | 触发条件 | chunk_type |
| --- | --- | --- |
| question | 用户输入 ≥ 10 字符 | `question` |
| answer | 无 agent_outputs | `answer` |
| agent_output | 有 agent_outputs | `agent_output`（每 Agent 独立） |

#### NaN 三层容错（Ollama bge-m3 已知 bug）

```text
1. _preprocess_text()            — 清除控制字符 + NFKC 归一化 + 截断 4000 字
2. _call_ollama_native_embed()   — 降级至 /api/embed 原生接口
3. text[:len//2] + retry         — 截半后重试 /v1/embeddings
```

---

### redis_keys.py — Redis Key 常量

所有 Redis Key 模板集中定义，避免散落在各模块中：

| 常量 | Key 格式 | 用途 |
| --- | --- | --- |
| `MEMORY_TURNS` | `memory:{user_id}:turns` | 近期对话列表 |
| `MEMORY_PREFETCH_RESULT` | `memory:{user_id}:prefetch_result` | 预取缓存 |
| `USER_PROFILE` | `user:{user_id}:init` | 用户画像 |
| `DELEGATIONS` | `user:{user_id}:delegations` | 委托链记录 |
| `NOTIFY_PENDING` | `notify:{user_id}:pending` | 任务通知队列 |

---

## 初始化顺序（main.py lifespan）

```text
1.  ConfigLoader.load_system_config()
2.  initialize_pools()                 # 数据库连接池
3.  MemoryManager(config)
4.  HermesEngine(config) + initialize()
5.  hermes_engine.set_memory_manager(mm)
6.  EmbeddingService(config)
7.  VectorStore(embed_svc, config)
8.  validate_embedding_config()        # 模型变更检测 + 必要时重建
9.  memory_manager.set_vector_store(vs)
10. context_manager.set_vector_store(vs)
11. register_system_tasks(task_scheduler)  # 月度/年度归档 cron
```

---

## 配置对应关系

| 配置键 | 默认值 | 对应功能 |
| --- | --- | --- |
| `system.redis_recent_turns` | 10 | Redis 保留轮次上限 |
| `system.es_sync_threshold_pct` | 0.6 | ES 同步触发比例 |
| `system.compression.trigger_message_count` | 30 | 压缩触发阈值 |
| `system.retrieval.top_k` | 3 | 记忆检索最大返回数 |
| `system.retrieval.confidence_threshold` | 0.7 | 记忆相关性阈值 |
| `embedding.chunk_size` | 800 | 单块最大字符数 |
| `logging.level` | `INFO` | 全局日志级别 |
