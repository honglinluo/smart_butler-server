# app/api — 完成说明

**更新日期**: 2026-05-05
**版本**: 2.6
**状态**: ✅ 核心接口已实现

---

## 实现状态

| 文件 | 状态 | 说明 |
| --- | --- | --- |
| `auth.py` | ✅ | 注册/登录/Token 校验/登出（画像固化） |
| `models.py` | ✅ | LLM 模型配置 CRUD + 切换后清除 LLM 缓存 |
| `chat.py` | ✅ | 同步/SSE流式/历史查询/文件上传/重向量化 |
| `agents_api.py` | ✅ | Agent CRUD + 评分 + 热重载 |
| `tools_api.py` | ✅ | 工具 CRUD，三来源可见性控制，危险操作声明 |
| `scheduler_api.py` | ✅ | 7 种任务类型 + 运行日志 + SSE 通知流 |
| `decision_api.py` | ✅ | 工具构建挂起/确认/策略配置 |
| `dependencies.py` | ✅ | JWT 认证 + LLM 配置加载（含降级） |

---

## 已实现功能

### 聊天接口（chat.py）

- ✅ POST `/chat/send` — 同步 HTTP，接收消息调用 HermesEngine，返回回复
- ✅ POST `/chat/stream` — SSE 流式输出（routing / token / done / error 四种事件）
- ✅ GET `/chat/history` — 从 ES 分页查询历史 turn
- ✅ POST `/chat/upload` — 多格式文件上传，通过沙箱处理后注入对话上下文
- ✅ POST `/chat/revectorize` — 触发全量历史重向量化
- ✅ 支持 `agent_name` 参数绕过路由决策，直接调用指定 Agent
- ✅ LLM 实例按优先级（Redis → MySQL → 默认）加载

### Agent 管理（agents_api.py）

- ✅ 完整 CRUD（创建/查询/更新/删除）
- ✅ DB Agent 启动时自动注册到 registry
- ✅ 热重载接口（`/agents/reload`），无需重启服务
- ✅ 评分接口，低评分触发日志告警（阈值 3.0 分，最少 5 次评分）
- ✅ `is_public` 控制 Agent 可见范围

### 工具管理（tools_api.py）

- ✅ GET `/tools/` — 查询当前用户可用工具（过滤可见性）
- ✅ POST `/tools/` — 创建工具，支持 user/agent 来源
- ✅ PUT `/tools/{id}` — 更新工具（限制 agent 来源字段修改）
- ✅ DELETE `/tools/{id}` — 删除工具（权限校验）

### 定时任务（scheduler_api.py）

- ✅ 完整任务 CRUD
- ✅ 7 种任务类型（once/daily/weekly/monthly/workday/weekend/cron）
- ✅ 运行日志查询（按任务 ID）
- ✅ SSE 通知流（`/scheduler/notify/stream`）
- ✅ 任务统计接口

### 工具构建决策（decision_api.py）

- ✅ GET `/decisions/pending` — 查询当前挂起的工具构建请求
- ✅ POST `/decisions/{id}/resolve` — 放行或拒绝（allow/deny）
- ✅ GET `/decisions/logs/{session_id}` — 查询 AgentEventLoop 调用日志
- ✅ GET/PUT `/users/{id}/decision-policy` — 获取/设置授权策略

---

## 关键设计

### 无状态 API

所有接口均无状态，状态通过以下方式传递：

- **用户身份**：JWT Token → `get_current_user` 解析
- **对话上下文**：由 `ContextManager` 从 Redis/ES 实时加载，不在 API 层维护
- **LLM 实例**：每次请求按需创建（带缓存）

### LLMInfo 实例化

`user_model` 为 dict 时通过直接导入的 `LLMInfo`（`from app.core.hermes_engine import LLMInfo`）
实例化，再调用 `engine._build_llm_from_config(llm_info)` 构建 LangChain 模型。

### SSE 流式输出

`/chat/stream` 返回 `StreamingResponse`，内容通过 `async generator` 实时推送，
每个事件格式为：

```text
event: <type>
data: <json>

```

---

## 近期变更

### v2.6 — 2026-05-05

- **`chat.py` — LLMInfo 导入修复**：新增 `from app.core.hermes_engine import LLMInfo`，
  修复三处 `engine.LLMInfo(...)` 导致的 AttributeError

### v2.4 — 2026-04-28

- **新增 `tools_api.py`**：工具 CRUD，可见性/危险操作/来源约束
- **新增 `scheduler_api.py`**：7 种定时任务类型 + SSE 通知
- **新增 `decision_api.py`**：工具构建授权门控

### v2.2 — 2026-04-26

- **`models.py` — `/models/change` 清除 LLM 缓存**：切换成功后立即生效

---

## 待完善功能

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| 速率限制 | 🔧 | 缺少 slowapi 或自定义中间件限流 |
| OpenAPI Schema 完善 | 🔧 | response_model 可进一步补充 |
