# app/agents — 完成说明

**更新日期**: 2026-05-05
**版本**: 2.6
**状态**: ✅ 核心功能已实现

---

## 实现状态

| 文件 | 状态 | 说明 |
| --- | --- | --- |
| `base.py` | ✅ | BaseAgent + 技能记忆系统 + 背景模板外置 |
| `router.py` | ✅ | 意图识别 + 任务分解 + 流水线规划 |
| `registry.py` | ✅ | 全局 Agent 注册表 |
| `decorators.py` | ✅ | `@agent` 装饰器 |
| `loop/event_loop.py` | ✅ | AgentEventLoop，最多 6 次迭代，工具构建门控 |
| `loop/decision_gate.py` | ✅ | allow/ask/deny 三策略，ask 时挂起等待 API 确认 |
| `loop/tool_builder.py` | ✅ | Code Agent 运行时生成工具，热加载到 registry |
| `loop/events.py` | ✅ | ToolCodeRequest / BuiltTool / LoopLogEntry 数据类 |
| `loop/loop_logger.py` | ✅ | 调用日志双写（Redis List + 标准日志） |
| `system/memory_archiver.py` | ✅ | 摘要归档 + 画像提取 + 向量化 |
| `system/monthly_archiver.py` | ✅ | 月度归档，Saga 三步，MySQL 防重复 |
| `system/yearly_archiver.py` | ✅ | 年度归档，Saga 三步，MySQL 防重复 |
| `workers/summarizer.py` | ✅ | 8 节 XML 压缩 + 月度/年度摘要 |
| `workers/skill_builder.py` | ✅ | 技能文件生成与优化 |
| `workers/general_assistant.py` | ✅ | 通用问答（框架实现） |
| `workers/data_analyst.py` | 🔧 | 框架就绪，SQL 查询/图表工具待实现 |
| `workers/customer_support.py` | 🔧 | 框架就绪，工单工具待实现 |
| `workers/code_assistant.py` | 🔧 | 框架就绪，代码生成工具待实现 |

---

## 已实现功能

### AgentEventLoop（loop/event_loop.py）

- ✅ 最多 6 次迭代的工具构建循环
- ✅ 检测 Agent 响应中的 `ToolCodeRequest` JSON
- ✅ 通过 `UserDecisionGate` 核查授权（allow/ask/deny）
- ✅ `ask` 策略下挂起协程，5 分钟超时自动转 DENIED
- ✅ `ToolBuilder` 生成工具代码并热加载到 registry
- ✅ 每步写入 Redis List 和标准日志（SSE 可消费）

### 记忆归档体系

- ✅ `summarizer.py`：8 节 XML 结构正式压缩 + 月度/年度 4 节摘要
- ✅ `memory_archiver.py`：日常 Saga 归档 + 用户画像提取
- ✅ `monthly_archiver.py`：超 365 天 turn 按月归档，MySQL 防重复
- ✅ `yearly_archiver.py`：超 3 年月度摘要按年归档，MySQL 防重复

---

## 近期变更

### v2.5 — 2026-04-29

- 新增 `loop/` 子目录（event_loop / decision_gate / tool_builder / events / loop_logger）
- 新增 `system/monthly_archiver.py` 和 `system/yearly_archiver.py`
- `workers/summarizer.py` 完整重写（8 节结构，全替换，月度/年度方法）

---

## 待实现功能

| 功能 | 优先级 | 说明 |
| --- | --- | --- |
| Worker Agent 工具调用实现 | 高 | data_analyst / customer_support / code_assistant 需接入真实工具 |
| memory_monthly_jobs / memory_yearly_jobs 建表 | 高 | create_tables.py 中需添加 DDL |
| Agent 间通信协议 | 中 | 目前通过 context["prev_result"] 传递，可设计更结构化格式 |
| 技能库版本管理 | 低 | 当前覆盖写，无历史回溯 |
