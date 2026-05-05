# FastAPI 依赖注入（Depends）使用指南

## 快速理解

`Depends(get_current_user)` 是 FastAPI 的**依赖注入**机制：

1. **自动调用** `get_current_user()` 函数
2. **验证**用户身份（检查 Token）
3. **注入**返回值到路由函数

```python
@router.post("/chat/send")
async def send_message(
    chat_data: ChatMessage,
    current_user: dict = Depends(get_current_user),   # 自动注入
    user_model     = Depends(get_user_model),          # 自动注入
    engine         = Depends(get_hermes_engine),       # 自动注入
):
    user_id = current_user["user_id"]
    ...
```

---

## 本项目中的依赖函数

### get_current_user

从请求头解析 Bearer Token，验证后返回用户信息 dict：

```python
{"user_id": "user_123", "username": "alice", "token": "..."}
```

**传参方式**（推荐 Authorization Header）：

```bash
curl -H "Authorization: Bearer <token>" http://localhost:8000/chat/send ...
```

### get_user_model

按优先级加载用户 LLM 配置：

```text
Redis（user:{id}:model 缓存）→ MySQL llms 表 → 系统默认用户 "0" 配置
```

返回 dict（未构建的配置）或已构建的 LangChain 模型实例。

`chat.py` 中判断 `isinstance(user_model, dict)` 时，使用 `LLMInfo` 实例化后
调用 `engine._build_llm_from_config(llm_info)` 构建模型：

```python
from app.core.hermes_engine import LLMInfo

if isinstance(user_model, dict):
    llm_info = LLMInfo(
        user_id=user_id,
        url=user_model.get("url", ""),
        api_key=user_model.get("api_key", ""),
        model_name=user_model.get("model_name", ""),
        model_type=user_model.get("model_type", "chat"),
        temperature=float(user_model.get("temperature", 0.7)),
    )
    llm_instance = await engine._build_llm_from_config(llm_info)
```

### require_local_or_auth

本地请求（`127.0.0.1`）直接放行，否则要求 Bearer Token 认证。
用于管理接口（如 `/agents/reload`），允许本地维护操作免认证。

### get_hermes_engine

从 `request.app.state.hermes_engine` 获取全局引擎实例，未初始化则返回 500。

---

## 错误响应

| 情况 | HTTP 状态 | 说明 |
| --- | --- | --- |
| 缺少 Token | 401 | Authorization header 或 query token 均未提供 |
| Token 无效 | 401 | Token 不存在或已过期 |
| 无权限 | 403 | 资源不属于当前用户 |
| 引擎未初始化 | 500 | hermes_engine 未在 app.state 中注册 |

---

## 完整请求示例

```bash
# 1. 注册
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username": "alice", "password": "pass123"}'

# 2. 登录，获取 Token
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "alice", "password": "pass123"}' | jq -r '.token')

# 3. 发送消息（同步）
curl -X POST http://localhost:8000/chat/send \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "你好"}'

# 4. 流式输出（SSE）
curl -N -X POST http://localhost:8000/chat/stream \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "写一篇短文"}'
```
