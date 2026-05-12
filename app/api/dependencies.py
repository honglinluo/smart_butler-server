"""
【模块说明】公共依赖 — 登录验证与模型加载

这个文件提供两个核心"前置检查"，在处理每个请求前先执行：

1. 验证用户是否已登录（get_current_user）
   检查请求携带的 Token 是否有效，有效则继续处理，无效则拒绝并返回"请重新登录"。

2. 加载用户当前使用的 AI 模型（get_user_model）
   从数据库中查询该用户当前激活的模型配置（模型地址、API Key 等），
   找不到时提示用户先去添加模型。

其他 API 文件通过 `Depends(get_current_user)` 等方式引用这里的函数，
相当于给每个接口自动加上了"登录门禁"和"模型准备"步骤。
"""


import asyncio
import json
import logging
from typing import Optional
from fastapi import Depends, HTTPException, Request, status, Header, Query
from app.database.pool import get_connection, release_connection
from app.core.redis_keys import SESSION_TOKEN, USER_INIT, INIT_TTL_WARN

logger = logging.getLogger(__name__)


async def get_current_user(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None)
) -> dict:
    """
    【登录验证】检查当前请求是否携带有效的登录令牌（Token）。

    Token 可以通过两种方式传入：
      方式1 — 请求头：Authorization: Bearer <token>
      方式2 — URL 参数：?token=<token>

    验证过程：去 Redis 缓存中查找该 Token 对应的用户信息，
    找到则返回用户信息（包含 user_id、用户名等），找不到则报错"请重新登录"。
    """
    # 优先使用 Authorization header
    if authorization:
        if authorization.startswith("Bearer "):
            token = authorization[7:]  # 移除 "Bearer " 前缀
        else:
            token = authorization
    
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token. "
                   "请在 Authorization header 中提供 token，格式: 'Authorization: Bearer <token>' "
                   "或使用 query 参数: '?token=<token>'"
        )
    
    # 从 Redis 验证 token
    redis_conn = await get_connection("redis", None)

    try:
        # 从 Redis 读取用户数据
        user_data = await redis_conn.read(SESSION_TOKEN.format(token=token))

        if not user_data:
            logger.warning(f"Token 无效或已过期: {token[:8]}...")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token 无效或已过期，请重新登录"
            )

        # 解析用户数据
        if isinstance(user_data, str):
            try:
                user_data = json.loads(user_data)
            except Exception:
                logger.error(f"Redis 中 session:token:{token[:8]}... 的数据格式错误")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token 数据异常，请重新登录"
                )

        if not isinstance(user_data, dict) or not user_data.get("user_id"):
            logger.error(f"Token 数据缺少 user_id 字段: {token[:8]}...")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token 数据异常，请重新登录"
            )

        return user_data

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token 验证异常: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="认证失败，请重新登录"
        )
    finally:
        if redis_conn:
            await release_connection("redis", redis_conn)


_LOCALHOST_ADDRS = {"127.0.0.1", "::1", "localhost"}


async def require_local_or_auth(
    request:       Request,
    authorization: Optional[str] = Header(None),
    token:         Optional[str] = Query(None),
) -> Optional[dict]:
    """
    【管理接口门禁】本机访问免登录，远程访问必须登录。

    用于后台管理命令（如重新构建向量索引等），
    在服务器本地运行时可以直接调用，无需 Token；
    从外网或前端调用时仍需登录验证。
    """
    client_host = request.client.host if request.client else ""
    has_token   = bool(authorization or token)

    # 本机且无 Token → 直接放行（CLI 后台执行场景）
    if client_host in _LOCALHOST_ADDRS and not has_token:
        return None

    # 其他情况走正常 Token 验证
    return await get_current_user(authorization=authorization, token=token)


async def get_user_model(user: dict = Depends(get_current_user)):
    """
    【模型加载】从数据库中取出当前用户正在使用的 AI 模型配置。

    查找逻辑：
      1. 先查用户自己添加并激活的模型
      2. 找不到则自动回退到系统预置的默认模型
      3. 两者都没有则提示用户去"模型管理"页面添加一个

    同时检查用户缓存剩余时间，即将过期时在后台悄悄把用户画像存回数据库。
    """
    user_id = user.get("user_id", "test_user")

    # 模型配置以 MySQL 为准（llms 表 state=1 的最新记录），保证切换模型后立即生效
    mysql_conn = await get_connection("mysql", None)
    if mysql_conn:
        try:
            sql = (
                "SELECT url, api_key, model_name, temperature, model_type "
                "FROM llms WHERE user_id = :user_id AND state = 1 "
                "AND model_type != 'embedding' "
                "ORDER BY id DESC LIMIT 1"
            )
            # 先查用户自有模型，找不到则回退到系统默认模型（user_id = '0'）
            df = await mysql_conn.execute_raw(sql, {"user_id": user_id})
            if (df is None or len(df) == 0) and user_id != "0":
                logger.info(f"用户 {user_id} 无可用模型，回退到系统默认模型")
                df = await mysql_conn.execute_raw(sql, {"user_id": "0"})

            if df is not None and len(df) > 0:
                row = df.iloc[0]
                model_data = {
                    "model_name": row["model_name"],
                    "api_key":    row["api_key"],
                    "url":        row["url"],
                    "temperature": float(row["temperature"]) if row.get("temperature") is not None else 0.7,
                    "model_type": row.get("model_type", "chat"),
                }
                logger.info(f"从 MySQL 加载模型配置: {user_id}, model={model_data['model_name']}")

                # TTL 告警：USER_INIT 即将到期时异步固化画像到 MySQL
                redis_conn = await get_connection("redis", None)
                if redis_conn:
                    try:
                        init_key = USER_INIT.format(user_id=user_id)
                        ttl = await redis_conn.get_ttl(init_key)
                        if 0 < ttl < INIT_TTL_WARN:
                            init_data = await redis_conn.read(init_key)
                            profile = init_data.get("profile") if isinstance(init_data, dict) else None
                            if profile:
                                from app.api.auth import _flush_profile_to_mysql
                                asyncio.create_task(_flush_profile_to_mysql(user_id, profile))
                    except Exception:
                        pass
                    finally:
                        await release_connection("redis", redis_conn)

                return model_data

            logger.warning(f"用户 {user_id} 及系统默认均无可用模型")
        except Exception as e:
            logger.warning(f"从 MySQL 读取模型配置失败 user={user_id}: {e}")
        finally:
            await release_connection("mysql", mysql_conn)

    raise HTTPException(status_code=404, detail="未找到可用模型，请先配置模型")
