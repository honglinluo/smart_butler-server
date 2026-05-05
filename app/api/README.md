# app/api — API 模块文档

本目录提供 Hermes Multi-Agent System 的 RESTful API 接入层，基于 FastAPI 实现。

## 目录结构

```text
app/api/
├── __init__.py
├── auth.py           # 用户认证（登录/注册/Token 校验/登出）
├── models.py         # LLM 模型配置管理
├── chat.py           # 聊天接口（同步/SSE流式/历史/上传/重向量化）
├── agents_api.py     # Agent 管理（CRUD、评分、重载）
├── tools_api.py      # 工具管理（查询、创建、更新、删除）
├── scheduler_api.py  # 定时任务管理 + 通知 SSE
├── decision_api.py   # 工具构建决策门控 + 策略配置
├── dependencies.py   # FastAPI 依赖注入（认证、模型加载）
└── routes/
    └── __init__.py
```

---

## 接口总览

| 方法 | 路径 | 模块 | 说明 |
| --- | --- | --- | --- |
| GET | `/health` | main.py | 系统健康检查 |
| POST | `/auth/register` | auth.py | 用户注册 |
| POST | `/auth/login` | auth.py | 用户登录（返回 Token） |
| POST | `/auth/logout` | auth.py | 登出（固化画像） |
| GET/POST/PUT/DELETE | `/models/...` | models.py | LLM 模型配置管理 |
| POST | `/chat/send` | chat.py | 发送聊天消息（同步） |
| POST | `/chat/stream` | chat.py | 发送聊天消息（SSE 流式） |
| GET | `/chat/history` | chat.py | 查询对话历史 |
| POST | `/chat/upload` | chat.py | 上传文件/内容，沙箱处理后发送 |
| POST | `/chat/revectorize` | chat.py | 触发重向量化 |
| GET/POST/PUT/DELETE | `/agents/...` | agents_api.py | Agent 管理 |
| POST | `/agents/{name}/rate` | agents_api.py | 为 Agent 评分 |
| POST | `/agents/reload` | agents_api.py | 热重载 DB Agent |
| GET | `/tools/` | tools_api.py | 查询可用工具列表 |
| POST | `/tools/` | tools_api.py | 创建工具 |
| PUT | `/tools/{tool_id}` | tools_api.py | 更新工具 |
| DELETE | `/tools/{tool_id}` | tools_api.py | 删除工具 |
| GET | `/scheduler/tasks` | scheduler_api.py | 查询任务列表 |
| POST | `/scheduler/tasks` | scheduler_api.py | 创建定时任务 |
| GET | `/scheduler/tasks/{id}` | scheduler_api.py | 查询任务详情 |
| DELETE | `/scheduler/tasks/{id}` | scheduler_api.py | 删除任务 |
| GET | `/scheduler/tasks/{id}/logs` | scheduler_api.py | 查询运行日志 |
| GET | `/scheduler/notify/stream` | scheduler_api.py | SSE 通知流 |
| GET | `/decisions/pending` | decision_api.py | 查询挂起决策 |
| POST | `/decisions/{id}/resolve` | decision_api.py | 确认/拒绝决策 |
| GET | `/decisions/logs/{session_id}` | decision_api.py | 查询事件循环日志 |
| GET/PUT | `/users/{id}/decision-policy` | decision_api.py | 获取/设置工具构建策略 |

---

## 模块详解

### auth.py — 用户认证

处理用户注册、登录和 Token 校验。

- 登录成功返回 Bearer Token（存储在 Redis，带 TTL）
- 登出时固化 Redis 用户画像到 MySQL
- 所有需要认证的接口通过 `Depends(get_current_user)` 注入用户信息

### models.py — 模型管理

管理用户绑定的 LLM 配置（存储于 MySQL `llms` 表）：

```json
{
  "url": "https://api.openai.com/v1",
  "api_key": "sk-...",
  "model_name": "gpt-4o",
  "model_type": "chat",
  "temperature": 0.7
}
```

- 系统用户（`user_id = "0"`）的配置作为全局默认模型
- `/models/change` 成功后调用 `hermes_engine.clear_llm_cache(user_id)`，新模型立即生效

### chat.py — 聊天接口

核心业务接口，支持同步和流式两种响应模式。

#### POST /chat/send — 同步响应

```json
{ "message": "用户消息", "context": {}, "agent_name": "可选" }
```

返回 `{ "response": "...", "user_id": "..." }`

#### POST /chat/stream — SSE 流式响应

返回 Server-Sent Events 流，事件类型：

| 事件 | 说明 |
| --- | --- |
| `routing` | 路由决策完成（含 intent/mode/agent） |
| `token` | LLM 输出 token 块（data.text） |
| `done` | 完成（data.turn_id） |
| `error` | 发生错误（data.message） |

#### GET /chat/history — 对话历史

从 ES 分页返回历史 turn，支持 `limit` / `offset` 参数。

#### POST /chat/upload — 文件/内容上传

接受 multipart/form-data，将文件通过沙箱处理后附加到对话上下文。

#### POST /chat/revectorize — 重向量化

触发当前用户全量历史重向量化（Embedding 模型变更后使用）。

### agents_api.py — Agent 管理

支持通过 API 动态创建和管理 DB Agent，无需修改代码。

- 完整 CRUD + 评分（低于 3.0 分且评分次数 ≥ 5 触发告警）
- `is_public = true` 的 Agent 所有用户均可调用
- `/agents/reload` 无需重启服务即可热更新

### tools_api.py — 工具管理

管理三来源（code/user/agent）工具。

**可见性控制**：

| 值 | 可调用范围 |
| --- | --- |
| `public` | 所有用户和 Agent |
| `private` | 仅创建者 |
| `exclusive` | 仅归属 Agent（agent 来源强制） |

**危险操作声明**：`dangerous_ops` 字段声明工具包含的危险操作类型，
框架自动通过 `ConsentManager` 核查用户授权级别。

### scheduler_api.py — 定时任务

支持 `once` / `daily` / `weekly` / `monthly` / `workday` / `weekend` / `cron` 七种类型。

- API 接收 CST（UTC+8）时间，内部转换为 UTC 存储
- SSE 通知流：3s 轮询，200 轮无新通知自动关闭，每轮发送 `: ping` keepalive

### decision_api.py — 工具构建决策门控

控制 `AgentEventLoop` 中运行时工具构建的授权机制。

**三种策略**：

| 策略 | 行为 |
| --- | --- |
| `allow` | 所有工具构建自动放行 |
| `ask` | 每次构建前挂起等待用户通过 API 确认（默认） |
| `deny` | 拒绝所有工具构建请求 |

挂起超时（默认 5 分钟）自动转为 DENIED。

### dependencies.py — 依赖注入

| 依赖函数 | 说明 |
| --- | --- |
| `get_current_user` | 从请求头解析 Bearer Token，返回用户信息 dict |
| `get_user_model` | 加载用户 LLM 配置（Redis → MySQL → 默认） |
| `require_local_or_auth` | 本地请求直接放行，否则要求认证 |

---

## 认证机制

所有需要认证的接口需在请求头中携带：

```text
Authorization: Bearer <token>
```

---

## 错误响应

| HTTP 状态码 | 说明 |
| --- | --- |
| 401 | 未认证或 Token 无效 |
| 403 | 无权限 |
| 404 | Agent / 工具 / 任务不存在 |
| 500 | 引擎处理失败（数据库或 LLM 异常） |

---

## 快速测试

```bash
# 健康检查
curl http://localhost:8000/health

# 登录
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "user1", "password": "pass"}' | jq -r '.token')

# 发送消息（同步）
curl -X POST http://localhost:8000/chat/send \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "你好，请介绍一下自己"}'

# 流式输出
curl -N -X POST http://localhost:8000/chat/stream \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "写一首关于 AI 的诗"}'

# 查询对话历史
curl "http://localhost:8000/chat/history?limit=10" \
  -H "Authorization: Bearer $TOKEN"

# 查询工具列表
curl http://localhost:8000/tools/ \
  -H "Authorization: Bearer $TOKEN"

# 创建定时任务
curl -X POST http://localhost:8000/scheduler/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"task_type": "daily", "hour": 9, "message": "早安提醒"}'

# 查看 API 文档
open http://localhost:8000/docs
```
