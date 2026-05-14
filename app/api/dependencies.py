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
from app.core.hermes_engine import LLMInfo
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
    【模型加载】从数据库中取出当前用户正在使用的 AI 模型配置，返回 LLMInfo 对象。

    查找逻辑：
      a. 读取 users.current_llm_id（用户手动选择的模型 ID）
      b. 有值则直接读取 llms 表对应记录；无值则读取系统默认模型（user_id='0'）
      c. 两者都没有则提示用户前往「模型管理」页面创建一个

    同时检查用户缓存剩余时间，即将过期时在后台悄悄把用户画像存回数据库。
    """
    user_id = user.get("user_id", "test_user")

    llm_info = await LLMInfo.load(user_id)

    if llm_info is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到可用模型，请前往「模型管理」页面创建一个模型",
        )

    logger.info("从 MySQL 加载模型配置: user=%s model=%s", user_id, llm_info.model_name)

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
            logger.error("用户 %s 的画像信息固化失败", user_id)
        finally:
            await release_connection("redis", redis_conn)

    return llm_info
