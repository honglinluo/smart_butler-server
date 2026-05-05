# app/agents — Agent 模块文档

本目录定义 Hermes 系统中所有 Agent 的基类、注册机制、路由逻辑、工作 Agent 实现，
以及 Agent 内部事件循环（工具构建门控）。

## 目录结构

```text
app/agents/
├── __init__.py
├── base.py                  # Agent 基类（BaseAgent + AgentSkill + 技能记忆系统）
├── router.py                # 路由 Agent（意图识别、任务分解、流水线规划）
├── registry.py              # Agent 注册表（全局单例）
├── decorators.py            # @agent 装饰器
├── loop/                    # Agent 内部事件循环
│   ├── event_loop.py        # AgentEventLoop 主控（最多 6 次迭代）
│   ├── decision_gate.py     # UserDecisionGate（allow/ask/deny 三策略）
│   ├── tool_builder.py      # 运行时工具构建（Code Agent 生成 + 热加载）
│   ├── events.py            # 事件数据类（ToolCodeRequest / BuiltTool / LoopLogEntry）
│   └── loop_logger.py       # 调用日志（Redis List + 标准日志双写）
├── workers/                 # 业务 Worker Agent
│   ├── general_assistant.py
│   ├── data_analyst.py
│   ├── customer_support.py
│   ├── code_assistant.py
│   ├── summarizer.py        # 对话压缩（8 节 XML 结构）
│   └── skill_builder.py
└── system/                  # 系统内置 Agent（不注册到 registry）
    ├── memory_archiver.py   # 日常摘要归档 + 画像提取
    ├── monthly_archiver.py  # 月度归档（Saga 三步）
    └── yearly_archiver.py   # 年度归档（Saga 三步）
```

---

## 核心概念

### Agent 的三种创建方式

#### 方式一：代码继承（最灵活）

```python
from app.agents.base import BaseAgent

class DataAnalystAgent(BaseAgent):
    name       = "data_analyst"
    role       = "数据分析工程师"
    background = "你擅长处理结构化数据..."
    tools      = ["sql_query", "chart_gen"]

    async def execute(self, task, context, llm) -> dict:
        result = await llm.ainvoke([...])
        return {"result": result.content, "success": True, "metadata": {}}
```

#### 方式二：装饰器（简化）

```python
from app.agents.decorators import agent

@agent(name="data_analyst", role="数据分析工程师", background="...")
class DataAnalystAgent(BaseAgent):
    async def execute(self, task, context, llm):
        ...
```

#### 方式三：DB Agent（API 创建，无需代码）

```bash
curl -X POST /agents/ -d '{"name": "analyst", "role": "分析师", "background": "..."}'
```

DB Agent 使用 `BaseAgent.execute()` 默认实现（LLM + 技能记忆注入提示词）。

---

## 模块详解

### base.py — Agent 基类

#### 技能记忆系统（AgentSkill）

每个 Agent 维护技能库（MySQL `agent_skills` 表），自动积累成功工作模式：

- **相似匹配**（前 30 字）→ 更新 success_rate 和 pattern
- **无相似且未满**（默认上限 10 条）→ 新增
- **无相似且已满** → 替换成功率最低的（仅当新技能更优时）

技能格式化后注入系统提示词，效果随使用次数积累。

#### 背景模板外置

`source=="code"` 的 Agent 初始化时自动从 `config/templates/{name}.txt` 加载背景描述，
未找到则使用代码内置默认并输出 WARNING。

---

### router.py — 路由 Agent

```text
RouterAgent.process(user_input, context, llm)
  → _identify_intent()    — LLM 从意图列表中选择
  → _decompose_tasks()    — LLM 拆解为子任务 JSON 列表
  → _assign_agents()      — 按 intent-agent 映射分配
  → _plan_mode()          — LLM 判断 single/serial/parallel
  → 返回 {intent, mode, pipeline, tasks, target_agent}
```

---

### loop/event_loop.py — AgentEventLoop

每个 Agent 执行内嵌一个事件循环，支持运行时动态构建工具。

#### 执行流程

```text
1. 调用目标 Agent（首次迭代注入工具请求说明）
2. 检测响应中的 ToolCodeRequest JSON
3. 通过 UserDecisionGate 核查授权策略（allow/ask/deny）
   - ask → 挂起协程，等待 POST /decisions/{id}/resolve
4. ToolBuilder.build() 生成工具代码 → 写入 MySQL → 热加载到 registry
5. 更新任务描述（通知 Agent 工具已就绪），重新调用
6. 重复直到完成 or 达最大迭代次数（默认 6 次）
```

#### 调用日志

每步同时写入标准日志（INFO）和 Redis List（SSE 可消费），
通过 `GET /decisions/logs/{session_id}` 查询。

---

### loop/decision_gate.py — UserDecisionGate

读取 Redis key `user:{user_id}:decision_policy` 的策略：

| 策略 | 行为 |
| --- | --- |
| `allow` | 所有工具构建自动放行 |
| `ask` | 挂起协程（asyncio.Event），等待 API 确认（默认策略） |
| `deny` | 中止并返回拒绝提示 |

挂起超时默认 5 分钟，超时自动转为 DENIED。

---

### workers/summarizer.py — 对话压缩

#### 正式压缩（compress_messages）

**8 节 XML 结构**：

```text
## 1. 事件概要      ## 2. 用户意图      ## 3. 决策与结论
## 4. Agent 调用记录  ## 5. 待办与跟进   ## 6. 用户偏好与习惯
## 7. 知识积累      ## 8. 情感与背景
```

- 压缩后 Redis 原始 turn 全部替换为摘要 turn，不保留原始对话
- Agent 调用记录一行化：`{agent_name}：{task≤40字} → {result≤60字}`

#### 月度/年度摘要

`summarize_monthly()` 和 `summarize_yearly()` 使用独立 4 节提示词，不提取用户画像。

---

### system/ — 系统内置 Agent

#### memory_archiver.py

日常摘要归档流程：LLM 压缩 → 写入 ES → 删除 Redis 原始 turn → 提取用户画像标签。

#### monthly_archiver.py

- 归档超过 365 天的历史 turn，按自然月分组
- Saga 三步：① LLM 生成月度摘要 → ② ES checkpoint → ③ 删除原始 turn
- MySQL 作业表 `memory_monthly_jobs`（`UNIQUE KEY uq_user_ym`，防重复执行）

#### yearly_archiver.py

- 归档超过 3 年的月度摘要，按自然年分组
- Saga 三步，MySQL 作业表 `memory_yearly_jobs`（`UNIQUE KEY uq_user_year`）

---

## Agent 执行的三级回退

`HermesEngine._get_or_load_agent(agent_name, user_id)` 查找顺序：

```text
1. registry（内存缓存）
   ↓ 未找到
2. MySQL agents 表（公开或属于当前用户）
   ↓ 未找到
3. YAML workers 配置（agents_config.yaml）
   ↓ 未找到
4. 返回 None → 告警
```

---

## MySQL 数据库表

| 表名 | 用途 |
| --- | --- |
| `agents` | DB Agent 定义 |
| `agent_skills` | Agent 技能记忆 |
| `agent_call_stats` | 调用统计 |
| `memory_monthly_jobs` | 月度归档作业记录（防重复） |
| `memory_yearly_jobs` | 年度归档作业记录（防重复） |
