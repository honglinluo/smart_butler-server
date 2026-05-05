# Hermes 多智能体系统 — 优化与迭代总结

**最后更新**: 2026-05-05
**版本**: 2.6
**状态**: ✅ 核心系统就绪，持续迭代中

---

## 优化记录（按时间倒序）

---

### v2.6 — 2026-05-05：Bug 修复

#### 1. LLMInfo AttributeError 修复

**问题**：`engine.LLMInfo(...)` 抛出 `AttributeError`，`/chat/send`、`/chat/stream`、
`/chat/upload` 三个接口在用户传入 dict 形式模型配置时全部崩溃。

**根因**：`LLMInfo` 是 `hermes_engine.py` 的模块级 dataclass，调用者误以为是引擎实例属性。

**修复**（`app/api/chat.py`）：新增 `from app.core.hermes_engine import LLMInfo`，
将三处 `engine.LLMInfo(...)` 改为 `LLMInfo(...)`。

#### 2. 记忆预取 key 始终未写入修复

**问题**：`memory:{user_id}:prefetch_result` 从未写入，预取机制完全无效。

**根因（双层）**：

- `_run_prefetch()` 依赖 `retrieve_memory()`，但 ES 只有在对话达到 `es_sync_threshold`
  条后才同步，新用户 ES 为空，`retrieve_memory` 返回空列表，函数直接 return。
- 即使 ES 有数据，`_sync_recent_to_es` 使用 `refresh=False`，存在约 1 秒的搜索不可见窗口，
  与 `_run_prefetch` 并发时仍可能读到空结果。

**修复**（`app/core/memory_manager.py`，`_run_prefetch`）：ES 搜索返回空时，
fallback 到 `get_recent_turns()` 取 Redis 近期对话写入预取缓存。

---

### v2.5 — 2026-04-29：记忆系统深度优化 + 月度/年度归档 + 系统级定时任务

#### 1. hermes-agent 记忆功能移植

对标 hermes-agent 项目，补充 5 项高价值记忆功能：

| 功能 | 涉及文件 | 核心实现 |
| --- | --- | --- |
| on_delegation 钩子 | hermes_engine.py | 每步 Agent 执行后异步记录委托链（Redis，定长 20 条） |
| 用户画像注入 | hermes_engine.py | 系统提示词末尾追加 `<user-profile>` XML 块 |
| 背景预取 | hermes_engine.py + memory_manager.py | store_turn() 后 queue_prefetch()，后台异步预热，原子 GETDEL 消费 |
| 预取消费 | context_manager.py | _retrieve_memories() Step 0 命中预取缓存则跳过完整检索 |
| memory_manager 扩展 | memory_manager.py | 新增 4 个方法：on_delegation / queue_prefetch / get_prefetched_context / build_system_prompt_block |

#### 2. 记忆压缩重构

`app/agents/workers/summarizer.py` 完整重写，三项核心变更：

**① 不保留上下文**：压缩完成后 Redis 原始 turn 全部替换为摘要 turn，无残留。

**② Agent 调用一行化**：

```text
{agent_name}：{task≤40字} → {result≤60字}
```

**③ 固定 8 节摘要结构**：

```text
事件概要 / 用户意图 / 决策与结论 / Agent 调用记录 /
待办与跟进 / 用户偏好与习惯 / 知识积累 / 情感与背景
```

月度/年度摘要使用独立 4 节提示词，不包含用户画像提取。

#### 3. 月度归档 Agent

新增 `app/agents/system/monthly_archiver.py`：

- 归档超过 365 天的 turn，按自然月分组
- Saga 三步（生成 → ES checkpoint → 删除原始），任意步失败可续接
- MySQL 作业表 `memory_monthly_jobs`（`UNIQUE KEY uq_user_ym`，防重复）

#### 4. 年度归档 Agent

新增 `app/agents/system/yearly_archiver.py`：

- 归档超过 3 年的月度摘要，按自然年分组
- Saga 三步，MySQL 作业表 `memory_yearly_jobs`（`UNIQUE KEY uq_user_year`）

#### 5. 系统级定时任务框架

- `models.py`：ActionType 新增 `SYSTEM`
- `runner.py`：模块级 handler 注册表，SYSTEM 分支，通知守卫
- `system_tasks.py`（新增）：幂等注册 handler + MySQL upsert 两条 cron 任务
- Cron：月度 `0 23 28,29,30,31 * *` / 年度 `0 1 31 12 *`

---

### v2.4 — 2026-04-28：定时任务系统 + 文件读取工具 + 敏感信息遮盖

#### 1. 定时任务系统（app/scheduler/）

7 种任务类型，接入中国法定节假日，SSE 实时通知，自实现 Cron 解析器。

#### 2. 文件读取工具（app/tools/builtin/file_reader.py）

17 类格式，Content Parts 多模态输出，路径安全约束，软依赖机制。

#### 3. 敏感信息自动遮盖

三套正则策略，覆盖 JSON / YAML / Word / Excel / .env，零误判优化
（`auth` 裸词不触发，`token_count: 123` 不触发，`auth: production` 不触发）。

---

### v2.3 — 2026-04-27：项目根路径统一管理

新建 `app/core/paths.py`，全局 `PROJECT_ROOT` 常量，优先读环境变量，兜底 `__file__` 推导。
生产部署时只需设置 `PROJECT_ROOT` 环境变量，无需改代码。

---

### v2.2 — 2026-04-26：多项功能补全与模板体系建设

#### 1. /models/change 后立即生效

切换成功后调用 `hermes_engine.clear_llm_cache(user_id)`，新模型立即生效。

#### 2. Prompt 模板全面外置

`config/templates/` 目录，12 个模板文件：

- `*_system.txt` — HermesEngine 系统提示（含 LangChain 占位符）
- `{agent_name}.txt` — BaseAgent 背景描述（纯文本）

#### 3. 程序关闭时用户画像批量固化

`_flush_all_profiles_on_shutdown()`：SCAN 扫描 `user:*:init`，逐个固化到 MySQL。

---

### v2.1 — 2026-04-25：日志统一与文档补全

`main.py` 等 7 个文件硬编码 `logging.INFO` → 从 `system_config.yaml` 动态读取。
修改配置后重启即生效，无需改代码。

---

### v2.0 — 2026-04-24：向量切片升级 + Ollama NaN 修复

#### 1. 向量切片策略升级

| 旧行为 | 新行为 |
| --- | --- |
| Q+A 合并为 qa_combined 块 | 用户问题独立 question 块，回复独立 answer 块 |
| 所有 Agent 输出合并存储 | 每个 Agent 输出独立 agent_output 块（含 agent_name） |
| 短输入也生成块 | < 10 字符跳过 question 块 |

串行流水线从「只存最终结果」改为「每步结果均独立向量化」。

#### 2. Ollama NaN Bug 三层容错

三层降级：`_preprocess_text()` → `_call_ollama_native_embed()` → `text[:len//2] + retry`

---

### v1.0 — 2026-04-21：核心系统初建

- LangGraph `create_react_agent` 替代旧 AgentExecutor
- 三层记忆架构（Redis L1 + MySQL L2 + ES L3）
- 混合检索：向量 + 全文，confidence_threshold 过滤
- RouterAgent 三次 LLM 调用（意图识别 + 任务分解 + 模式规划）
- Agent 技能记忆系统（成功率滚动加权，最多 10 条）
- DB Agent：API 创建，热重载，评分告警

---

## 当前系统测试状态

| 测试类型 | 状态 | 说明 |
| --- | --- | --- |
| 语法检查（py_compile） | ✅ | 全部核心模块 |
| chunk_turn 冒烟测试 | ✅ | 3 种场景验证 |
| Ollama NaN 修复验证 | ✅ | 失败→降级→成功路径 |
| FastAPI 应用启动 | ✅（需数据库） | 健康检查 /health 响应正常 |
| 端到端聊天流程 | 需全服务 | 需 MySQL + Redis + ES |
| 月度/年度归档 | 待集成测试 | 需建表后验证 |

---

## 下一步建议（优先级排序）

### 立即可做

1. **建表**：在 `create_tables.py` 添加 `memory_monthly_jobs` / `memory_yearly_jobs` DDL
2. **启动验证**：修改 `logging.level: "DEBUG"` 重启，确认 DEBUG 日志输出
3. **Embedding 初始化**：运行 `python scripts/setup_embedding.py`

### 近期工程

1. **Worker Agent 工具接入**：`data_analyst` / `customer_support` / `code_assistant`
2. **对话历史接口**：`GET /chat/history` 已实现，验证分页与 ES 查询

### 架构优化

1. **revectorize 保留 agent 结构**：全量重建时从 ES 读取 turn 后重构 agent_outputs
2. **PII 脱敏接入**：`vector_store._desensitize()` 接入正式脱敏库
3. **API 速率限制**：FastAPI 中间件层添加 slowapi
