"""FastAPI 依赖注入 - 鉴权、会话管理等"""

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
    验证当前用户身份 - 支持多种方式传递 token
    
    支持两种方式传递 token：
    1. HTTP Header: Authorization: Bearer <token>
    2. Query 参数: ?token=<token>
    
    Args:
        authorization: HTTP Authorization 头 (Bearer token)
        token: Query 参数中的 token (备用方式)
        
    Returns:
        dict: 用户信息，包含 user_id 等字段
        
    Raises:
        HTTPException: 认证失败
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
    if not redis_conn:
        logger.error("Redis 连接失败，无法验证 token")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="认证服务暂时不可用，请稍后重试"
        )

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
    """本机请求无需鉴权直接放行；远程请求必须携带有效 Token。

    用于服务端 CLI 命令对应的管理接口，CLI 在本机执行时天然通过，
    前端或远程调用仍走完整 Token 验证。
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
    获取或初始化用户的模型（从数据库或使用默认配置）
    
    Args:
        user: 当前用户信息
        
    Returns:
        dict: 模型配置信息或从 MySQL 加载的 LLM 配置
        
    Raises:
        HTTPException: 如果模型加载失败
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
