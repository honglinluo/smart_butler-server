# app/core — 完成说明

**更新日期**: 2026-05-05
**版本**: 2.6
**状态**: ✅ 核心功能已实现并验证

---

## 模块清单与实现状态

| 文件 | 状态 | 核心特性 |
| --- | --- | --- |
| `paths.py` | ✅ | PROJECT_ROOT 环境变量统一入口 |
| `config_loader.py` | ✅ | YAML 双配置文件加载，懒缓存 |
| `redis_keys.py` | ✅ | Redis Key 模板常量集中定义 |
| `hermes_engine.py` | ✅ | LangChain + LangGraph 编排，3 种执行模式，Prompt 模板外置 |
| `memory_manager.py` | ✅ | 三层记忆架构，预取（含 Redis fallback），画像注入，委托链 |
| `vector_store.py` | ✅ | ES 向量索引，全量重向量化，跨模型检测重建 |
| `embedding_service.py` | ✅ | Ollama/OpenAI 双模式，NaN 三层容错，分 Agent 切片 |
| `context_manager.py` | ✅ | 历史加载 + 预取消费 + 记忆检索 + 相关性过滤 |
| `chat_history_store.py` | ✅ | ES 聊天历史存取 |

---

## 已完成的主要功能

### 1. 多模式 Agent 编排（hermes_engine.py）

- ✅ single / serial / parallel 三种模式
- ✅ LangGraph ReAct Agent，工具调用链
- ✅ 三级 Agent 回退：registry → MySQL → YAML
- ✅ 串行步骤校验（validate_step_result）
- ✅ 用户画像注入系统提示词
- ✅ on_delegation 委托链记录（后台，定长 20 条）
- ✅ Prompt 模板外置（config/templates/）

### 2. 三层记忆架构（memory_manager.py）

- ✅ L1 Redis：LPUSH + LTRIM，近期 N 轮
- ✅ L2 MySQL：memory_references 引用计数
- ✅ L3 ES：触发式批量同步（Redis 锁防并发）
- ✅ 混合检索：向量 + 全文，去重合并，top_k=3
- ✅ 记忆预取（queue_prefetch / get_prefetched_context）
- ✅ ES 空时 Redis fallback（v2.6 修复）
- ✅ 用户画像 XML 块生成（build_system_prompt_block）

### 3. 向量化系统（embedding_service.py + vector_store.py）

- ✅ question / answer / agent_output 独立切片
- ✅ 短输入过滤（< 10 字符跳过 question 块）
- ✅ Ollama NaN 三层容错
- ✅ 跨模型检测：启动时对比 MySQL 与配置，不一致则全量重建
- ✅ ES bulk 批量写入

### 4. 上下文组装（context_manager.py）

- ✅ Step 0 消费预取缓存（原子 GETDEL，命中跳过后续检索）
- ✅ Step 1 向量检索
- ✅ Step 2 全文检索补足
- ✅ confidence_threshold 相关性过滤

---

## 近期变更

### v2.6 — 2026-05-05

**`memory_manager.py` — 预取 fallback 修复**：

`_run_prefetch()` 在 `retrieve_memory()` 返回空时（ES 未就绪或竞态），
fallback 到 `get_recent_turns()` 取 Redis 近期对话写入预取缓存，
确保新用户也能正常使用预取机制。

### v2.5 — 2026-04-29

- `hermes_engine.py`：on_delegation 钩子、用户画像注入、queue_prefetch 调用点（两处）
- `memory_manager.py`：新增 on_delegation / queue_prefetch / get_prefetched_context / build_system_prompt_block
- `context_manager.py`：_retrieve_memories Step 0 消费预取缓存

### v2.3 — 2026-04-27

- 新增 `paths.py`，所有内部模块改为 `from app.core.paths import PROJECT_ROOT`

### v2.2 — 2026-04-26

- `hermes_engine.py`：Prompt 模板外置，_load_agent_prompts()，clear_llm_cache()

---

## 待实现功能

| 功能 | 文件 | 状态 |
| --- | --- | --- |
| PII 脱敏完整逻辑 | vector_store.py:_desensitize() | 🔧 字段清空占位 |
| revectorize 携带 agent_outputs | vector_store.py:revectorize_filtered() | 🔧 重建时仅处理合并文本 |

---

## 代码规模

| 文件 | 行数 |
| --- | --- |
| hermes_engine.py | ~1,854 |
| memory_manager.py | ~978 |
| vector_store.py | ~472 |
| embedding_service.py | ~367 |
| context_manager.py | ~368 |
| chat_history_store.py | ~309 |
| 其余 | ~70 |
| **合计** | **~4,418** |
