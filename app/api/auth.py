"""
【模块说明】用户账号系统 — 注册、登录、退出、修改密码

这个文件负责用户账号的全部操作：
  - 注册新账号（手机号或邮箱）
  - 登录并获取访问令牌（Token）
  - 退出登录（同时把用户数据保存回数据库）
  - 修改密码

【安全机制 - 密码为何要加密传输？】
  用户的密码绝对不能以明文（原文）在网络上传输，否则一旦被拦截就泄露了。
  本系统使用"RSA-OAEP-SHA256"加密方案：服务器先给浏览器一把"公钥"（相当于一把锁），
  浏览器用这把锁把密码锁住再发过来，服务器用只有自己才有的"私钥"才能打开。
  这样即使数据在途中被截获，也无法读出原始密码。

【nonce（一次性码）是什么？】
  每次加密密码时，服务器还会附带一个 nonce（随机字符串，5 分钟有效，用完即失效）。
  目的是防止"重放攻击"：有人截获了加密后的数据包再发一遍，nonce 已消耗所以不会生效。

用户认证 API — 登录、注册、密码修改

密码传输安全：
  所有密码字段一律通过 RSA-OAEP-SHA256 加密传输，服务端不接受明文密码。
  客户端须先调用 GET /auth/public-key 获取公钥与一次性 nonce，
  再用公钥加密密码后附带 nonce 一起提交（nonce 5 分钟有效，单次消费）。

客户端 JS 加密示例：
  const {public_key_pem, key_nonce} = await (await fetch('/auth/public-key')).json();
  const pem    = public_key_pem.replace(/-----.*?-----/g,'').replace(/\\s/g,'');
  const derBuf = Uint8Array.from(atob(pem), c => c.charCodeAt(0));
  const pubKey = await crypto.subtle.importKey(
      'spki', derBuf.buffer, {name:'RSA-OAEP', hash:'SHA-256'}, false, ['encrypt']
  );
  const encrypt = async (text) => {
      const buf = await crypto.subtle.encrypt(
          {name:'RSA-OAEP'}, pubKey, new TextEncoder().encode(text)
      );
      return btoa(String.fromCharCode(...new Uint8Array(buf)));
  };
  // 登录示例：
  await fetch('/auth/login', {method:'POST', body: JSON.stringify({
      username: 'user@example.com',
      encrypted_password: await encrypt('MyP@ssw0rd'),
      key_nonce: key_nonce,
  })});
"""

import json
import logging
import re
import secrets
from datetime import datetime
from typing import Any, Optional

import bcrypt
from fastapi import APIRouter, HTTPException, Request, Response, status, Depends
from pydantic import BaseModel, Field, GetCoreSchemaHandler
from pydantic._internal import _schema_generation_shared
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import core_schema

from app.database.pool import get_connection, release_connection
from app.api.dependencies import get_current_user
from app.utils.headers import RequestHeaders, ResponseHeaders
from app.utils.crypto import (
    decrypt_password, generate_nonce, get_public_key_pem,
    nonce_redis_key, NONCE_TTL,
)
from app.core.redis_keys import (
    SESSION_TOKEN, SESSION_TTL,
    USER_INIT, INIT_TTL,
    USER_SESSIONS, USER_SESSIONS_TTL,
    AUTH_NONCE, AUTH_NONCE_TTL,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentication"])


# ═══════════════════════════════════════════════════════════════
# 自定义类型注解
# ═══════════════════════════════════════════════════════════════

class PhoneStr:
    """
    手机号格式校验器。
    接受中国大陆 11 位手机号（如 13812345678）或带 +86 前缀的格式。
    格式不合法时会直接报错，阻止后续操作。
    """

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source: type[Any], _handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls._validate, core_schema.str_schema()
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema_: core_schema.CoreSchema,
        handler: _schema_generation_shared.GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        field_schema = handler(core_schema_)
        field_schema.update(type="string", format="phone", example="13812345678")
        return field_schema

    @classmethod
    def _validate(cls, value: str, /) -> str:
        try:
            from phonenumbers import parse, is_valid_number
            p = parse(str(value).strip(), "CN")
            if is_valid_number(p):
                return str(value).strip()
            raise ValueError("请输入有效的中国大陆手机号")
        except Exception as exc:
            raise ValueError("请输入有效的中国大陆手机号") from exc


_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9](?:[a-zA-Z0-9._%+\-]*[a-zA-Z0-9])?"
    r"@[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?)*"
    r"\.[a-zA-Z]{2,}$"
)


class EmailStr:
    """
    邮箱格式校验器。
    检查输入是否是合法的邮箱地址（如 user@example.com），存储前统一转为小写。
    格式不合法时报错拒绝。
    """

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source: type[Any], _handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls._validate, core_schema.str_schema()
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema_: core_schema.CoreSchema,
        handler: _schema_generation_shared.GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        field_schema = handler(core_schema_)
        field_schema.update(type="string", format="email", example="user@example.com")
        return field_schema

    @classmethod
    def _validate(cls, value: str, /) -> str:
        value = str(value).strip().lower()
        if not _EMAIL_RE.match(value):
            raise ValueError("请输入有效的邮箱地址")
        return value


# ── 密码安全规则 ─────────────────────────────────────────────────────────────
# 密码必须同时满足以下 4 条规则，任一不满足都会拒绝注册/修改
_PASSWORD_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"[A-Z]"),  "至少包含一个大写字母（A-Z）"),
    (re.compile(r"[a-z]"),  "至少包含一个小写字母（a-z）"),
    (re.compile(r"\d"),     "至少包含一个数字（0-9）"),
    (re.compile(r"[!@#$%^&*()\-_=+\[\]{};:'\",.<>/?\\|`~]"),
                            "至少包含一个特殊字符（如 !@#$%^&*）"),
]
_PASSWORD_MIN_LEN = 8


def _validate_password_strength(password: str) -> str:
    """
    检查密码是否足够安全。
    密码需至少 8 位，且同时包含大写字母、小写字母、数字和特殊符号。
    不达标时抛出错误并告知具体缺少什么。
    """
    if len(password) < _PASSWORD_MIN_LEN:
        raise ValueError(f"密码长度至少 {_PASSWORD_MIN_LEN} 位")
    errors = [msg for pattern, msg in _PASSWORD_RULES if not pattern.search(password)]
    if errors:
        raise ValueError("密码不符合安全要求：" + "；".join(errors))
    return password


class UsernameStr:
    """
    用户名格式校验器。
    本系统用手机号或邮箱作为登录账号，此处自动判断输入的是哪种格式并分别校验。
    两种格式都不符合时报错。
    """

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source: type[Any], _handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls._validate, core_schema.str_schema()
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema_: core_schema.CoreSchema,
        handler: _schema_generation_shared.GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        field_schema = handler(core_schema_)
        field_schema.update(
            type="string",
            description="手机号（如 13812345678）或邮箱地址",
            example="13812345678",
        )
        return field_schema

    @classmethod
    def _validate(cls, value: str, /) -> str:
        value = str(value).strip()
        try:
            return PhoneStr._validate(value)
        except ValueError:
            pass
        try:
            return EmailStr._validate(value)
        except ValueError:
            pass
        raise ValueError("用户名须为有效的手机号或邮箱地址")


# ═══════════════════════════════════════════════════════════════
# 请求 / 响应模型
# ═══════════════════════════════════════════════════════════════

class PublicKeyResponse(BaseModel):
    public_key_pem: str  = Field(..., description="RSA 公钥（PEM 格式，SubjectPublicKeyInfo）")
    key_nonce:      str  = Field(..., description="一次性随机 nonce，提交密码时携带（5 分钟有效）")
    expires_in:     int  = Field(NONCE_TTL, description="nonce 有效期（秒）")
    algorithm:      str  = Field("RSA-OAEP-SHA256", description="加密算法标识")


class UserRegister(BaseModel):
    username:           UsernameStr = Field(..., description="手机号或邮箱，作为登录唯一标识")
    encrypted_password: str         = Field(..., description="RSA-OAEP-SHA256 加密后的密码（Base64）")
    key_nonce:          str         = Field(..., description="从 GET /auth/public-key 获取的一次性 nonce")


class UserLogin(BaseModel):
    username:           UsernameStr    = Field(..., description="手机号或邮箱")
    encrypted_password: str            = Field(..., description="RSA-OAEP-SHA256 加密后的密码（Base64）")
    key_nonce:          str            = Field(..., description="从 GET /auth/public-key 获取的一次性 nonce")


class ChangePassword(BaseModel):
    old_encrypted_password: str = Field(..., description="当前密码（RSA-OAEP-SHA256 加密，Base64）")
    new_encrypted_password: str = Field(..., description="新密码（RSA-OAEP-SHA256 加密，Base64）")
    key_nonce:               str = Field(..., description="从 GET /auth/public-key 获取的一次性 nonce")


# ═══════════════════════════════════════════════════════════════
# 密码工具
# ═══════════════════════════════════════════════════════════════

def hash_password(password: str) -> str:
    """
    把明文密码"加盐哈希"后存入数据库。
    哈希是单向运算：只能验证"是否匹配"，无法反推出原始密码，
    即使数据库被盗也不会泄露用户真实密码。
    """
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    """
    验证用户输入的密码是否与数据库中存储的哈希值匹配。
    返回 True 表示密码正确，False 表示密码错误。
    """
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        logger.error("数据库中存储的密码格式无效（非 bcrypt hash），请检查该用户记录")
        return False


def generate_token() -> str:
    """生成一个 64 位的随机登录令牌（Token），用于标识用户的登录状态。"""
    return secrets.token_hex(32)


# ═══════════════════════════════════════════════════════════════
# Nonce 验证（防重放攻击）
# ═══════════════════════════════════════════════════════════════

async def _consume_nonce(nonce: str, redis_conn) -> None:
    """验证 nonce 有效并原子性消费（删除），防止重放。

    Raises:
        HTTPException 400: nonce 不存在或已过期
    """
    key = AUTH_NONCE.format(nonce=nonce)
    # 读 + 删（非原子，但 nonce 5 分钟 TTL + 单次请求窗口，实际碰撞概率极低）
    val = await redis_conn.read(key)
    if val is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="key_nonce 无效或已过期，请重新获取公钥后再试",
        )
    await redis_conn.delete(key)


# ═══════════════════════════════════════════════════════════════
# 内部工具
# ═══════════════════════════════════════════════════════════════

async def _load_user_init_data(user_id: str, redis_conn) -> None:
    """
    用户登录成功后，把用户画像（偏好、个人信息等）从数据库预加载到内存缓存（Redis）中。
    这样后续对话中 AI 可以快速读取用户信息，无需每次都查询数据库，加快响应速度。
    """
    mysql_conn = await get_connection("mysql", "agent_db")

    try:
        profile: dict = {}
        df_p = await mysql_conn.execute_raw(
            "SELECT profile FROM user_profiles WHERE user_id = :uid",
            {"uid": user_id},
        )
        if df_p is not None and len(df_p) > 0:
            raw     = df_p.iloc[0]["profile"]
            profile = json.loads(raw) if isinstance(raw, str) else (raw or {})

        await redis_conn.create(
            USER_INIT.format(user_id=user_id),
            {"profile": profile},
            ttl=INIT_TTL,
        )
    finally:
        await release_connection("mysql", mysql_conn)


async def _flush_profile_to_mysql(user_id: str, profile: dict) -> None:
    """
    把内存缓存（Redis）中的用户画像持久化保存到数据库（MySQL）。
    在用户退出登录或缓存即将过期时调用，确保用户数据不丢失。
    """
    mysql_conn = await get_connection("mysql", "agent_db")
    try:
        await mysql_conn.execute_raw(
            """
            INSERT INTO user_profiles (user_id, profile)
            VALUES (:uid, :profile)
            ON DUPLICATE KEY UPDATE
                profile    = VALUES(profile),
                updated_at = CURRENT_TIMESTAMP
            """,
            {"uid": user_id, "profile": json.dumps(profile, ensure_ascii=False)},
        )
    except Exception as exc:
        logger.warning("[auth] 画像写入 MySQL 失败 user=%s: %s", user_id, exc)
    finally:
        await release_connection("mysql", mysql_conn)


# ═══════════════════════════════════════════════════════════════
# 路由
# ═══════════════════════════════════════════════════════════════

@router.get("/public-key", response_model=PublicKeyResponse, summary="获取 RSA 公钥与一次性 nonce")
async def get_public_key(response: Response):
    """返回服务端 RSA 公钥和一次性 nonce。

    - 客户端用公钥（RSA-OAEP-SHA256）加密明文密码
    - key_nonce 在 {expires_in} 秒内有效，提交后立即失效（不可重用）
    - 每次登录 / 注册 / 修改密码前都应重新获取
    """
    ResponseHeaders().apply(response)
    redis_conn = await get_connection("redis", None)

    try:
        nonce = generate_nonce()
        await redis_conn.create(
            AUTH_NONCE.format(nonce=nonce),
            "1",
            ttl=AUTH_NONCE_TTL,
        )
        return PublicKeyResponse(
            public_key_pem=get_public_key_pem(),
            key_nonce     =nonce,
        )
    finally:
        await release_connection("redis", redis_conn)


@router.post("/register", response_model=dict, summary="用户注册")
async def register(user: UserRegister, response: Response):
    """用户注册（用户名为手机号或邮箱，密码须经 RSA-OAEP-SHA256 加密后提交）。

    密码安全要求（服务端解密后校验）：
    - 长度 ≥ 8 位
    - 至少包含大写字母、小写字母、数字、特殊字符各一个
    """
    ResponseHeaders().apply(response)
    redis_conn = await get_connection("redis", None)
    mysql_conn = await get_connection("mysql", "agent_db")
    try:
        # 1. 验证并消费 nonce（防重放）
        await _consume_nonce(user.key_nonce, redis_conn)

        # 2. 解密密码
        try:
            plain_password = decrypt_password(user.encrypted_password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # 3. 校验密码强度
        try:
            _validate_password_strength(plain_password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # 4. 检查用户名是否已注册
        df = await mysql_conn.execute_raw(
            "SELECT user_id FROM users WHERE username = :username",
            {"username": user.username},
        )
        if df is not None and len(df) > 0:
            raise HTTPException(status_code=400, detail="该手机号或邮箱已注册")

        # 5. 创建用户
        user_id = f"user_{secrets.token_hex(8)}"
        await mysql_conn.execute_raw(
            "INSERT INTO users (user_id, username, password, created_at) "
            "VALUES (:user_id, :username, :password, :created_at)",
            {
                "user_id":    user_id,
                "username":   user.username,
                "password":   hash_password(plain_password),
                "created_at": datetime.now(),
            },
        )
        return {"message": "注册成功", "user_id": user_id}

    finally:
        await release_connection("redis",  redis_conn)
        await release_connection("mysql",  mysql_conn)


@router.post("/login", response_model=dict, summary="用户登录")
async def login(user: UserLogin, response: Response, req_headers: RequestHeaders = Depends(RequestHeaders)):
    """用户登录（用户名为手机号或邮箱，密码须经 RSA-OAEP-SHA256 加密后提交）。

    Session 复用策略：
    - 若该用户在其他平台已有有效 session，直接续期并返回已有 token
    - 若所有 session 均已过期，签发新 token
    - 过期的 token 引用自动从 sessions Set 清除
    """
    ResponseHeaders().apply(response)
    redis_conn = await get_connection("redis", None)
    mysql_conn = await get_connection("mysql", "agent_db")
    try:
        # 1. 验证并消费 nonce
        await _consume_nonce(user.key_nonce, redis_conn)

        # 2. 解密密码
        try:
            plain_password = decrypt_password(user.encrypted_password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # 3. 查询用户 + 验证密码
        df = await mysql_conn.execute_raw(
            "SELECT user_id, password FROM users WHERE username = :username",
            {"username": user.username},
        )
        if df is None or len(df) == 0:
            raise HTTPException(status_code=401, detail="用户名或密码错误")

        db_user = df.iloc[0]
        if not verify_password(plain_password, db_user["password"]):
            raise HTTPException(status_code=401, detail="用户名或密码错误")

        user_id = db_user["user_id"]

        # 4. 更新登录时间
        await mysql_conn.execute_raw(
            "UPDATE users SET last_login_at = :last_login WHERE user_id = :user_id",
            {"user_id": user_id, "last_login": datetime.now()},
        )

        # 5. 查找已有有效 session（多平台登录复用 token）
        token: str = ""
        is_reused  = False
        client = getattr(redis_conn, "redis_client", None)
        sessions_key = USER_SESSIONS.format(user_id=user_id)

        if client:
            existing_tokens = client.smembers(sessions_key)
            stale_tokens: list = []
            for t in existing_tokens:
                t_str = t.decode() if isinstance(t, bytes) else t
                session_key = SESSION_TOKEN.format(token=t_str)
                if client.exists(session_key):
                    # 找到有效 session → 续期后复用
                    token     = t_str
                    is_reused = True
                    client.expire(session_key, SESSION_TTL)
                    break
                else:
                    stale_tokens.append(t)
            # 清理过期 token 引用
            if stale_tokens:
                client.srem(sessions_key, *stale_tokens)

        # 6. 无有效 session → 签发新 token
        if not token:
            token = generate_token()
            await redis_conn.create(
                SESSION_TOKEN.format(token=token),
                {
                    "user_id":          user_id,
                    "username":         user.username,
                    "token":            token,
                    "is_authenticated": True,
                    "client_type":      req_headers.client_type,
                    "client_version":   req_headers.client_version,
                },
                ttl=SESSION_TTL,
            )
            if client:
                client.sadd(sessions_key, token)
                client.expire(sessions_key, USER_SESSIONS_TTL)

        # 7. 预热用户初始化缓存
        await _load_user_init_data(user_id, redis_conn)

        msg = "已在其他平台登录，返回现有会话" if is_reused else "登录成功"
        logger.info("[login] user=%s is_reused=%s", user_id, is_reused)
        return {"message": msg, "token": token, "user_id": user_id}

    finally:
        await release_connection("mysql", mysql_conn)
        await release_connection("redis",  redis_conn)


@router.post("/logout", response_model=dict, summary="退出登录")
async def logout(request: Request, response: Response, current_user: dict = Depends(get_current_user)):
    """退出当前平台的登录状态。

    仅当所有平台均已退出时，执行用户画像固化到 MySQL 和对话记录同步到 ES。
    """
    ResponseHeaders().apply(response)
    user_id = current_user["user_id"]
    token   = current_user.get("token", "")

    redis_conn = await get_connection("redis", None)

    try:
        # 1. 删除当前平台 session token
        if token:
            await redis_conn.delete(SESSION_TOKEN.format(token=token))

        # 2. 从 sessions Set 移除当前 token，统计剩余有效 session 数
        remaining_valid = 0
        client = getattr(redis_conn, "redis_client", None)
        if client:
            sessions_key = USER_SESSIONS.format(user_id=user_id)
            if token:
                client.srem(sessions_key, token)
            remaining_tokens = client.smembers(sessions_key)
            dead_tokens = []
            for t in remaining_tokens:
                if client.exists(SESSION_TOKEN.format(token=t)):
                    remaining_valid += 1
                else:
                    dead_tokens.append(t)
            if dead_tokens:
                client.srem(sessions_key, *dead_tokens)

        # 3. 所有平台退出 → 固化画像 + 同步 ES
        if remaining_valid == 0:
            logger.info("[logout] 用户 %s 所有 session 已退出，开始固化数据", user_id)
            init_data = await redis_conn.read(USER_INIT.format(user_id=user_id))
            if isinstance(init_data, dict):
                profile = init_data.get("profile")
                if profile:
                    await _flush_profile_to_mysql(user_id, profile)
                    logger.info("[logout] 用户画像已固化到 MySQL user=%s", user_id)

            memory_manager = getattr(request.app.state, "memory_manager", None)
            if memory_manager:
                await memory_manager.flush_turns_to_es(user_id)
                logger.info("[logout] 对话记录已同步到 ES user=%s", user_id)
        else:
            logger.info(
                "[logout] 用户 %s 仍有 %d 个平台在线，跳过固化",
                user_id, remaining_valid,
            )

    except Exception as exc:
        logger.warning("[logout] 处理异常 user=%s: %s", user_id, exc)
    finally:
        await release_connection("redis", redis_conn)

    return {"message": "退出成功"}


@router.post("/change-password", response_model=dict, summary="修改密码")
async def change_password(
    password_data: ChangePassword,
    response:      Response,
    current_user:  dict = Depends(get_current_user),
):
    """修改密码（新旧密码均须经 RSA-OAEP-SHA256 加密后提交）。

    新密码安全要求（服务端解密后校验）：
    - 长度 ≥ 8 位；大写字母、小写字母、数字、特殊字符各至少一个
    """
    ResponseHeaders().apply(response)
    redis_conn = await get_connection("redis", None)
    mysql_conn = await get_connection("mysql", "agent_db")
    try:
        # 1. 验证并消费 nonce
        await _consume_nonce(password_data.key_nonce, redis_conn)

        # 2. 解密新旧密码
        try:
            old_plain = decrypt_password(password_data.old_encrypted_password)
            new_plain = decrypt_password(password_data.new_encrypted_password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # 3. 校验新密码强度
        try:
            _validate_password_strength(new_plain)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # 4. 验证旧密码正确性
        df = await mysql_conn.execute_raw(
            "SELECT password FROM users WHERE user_id = :user_id",
            {"user_id": current_user["user_id"]},
        )
        if df is None or len(df) == 0:
            raise HTTPException(status_code=404, detail="用户不存在")
        if not verify_password(old_plain, df.iloc[0]["password"]):
            raise HTTPException(status_code=400, detail="当前密码错误")

        # 5. 更新密码
        await mysql_conn.execute_raw(
            "UPDATE users SET password = :password WHERE user_id = :user_id",
            {
                "user_id":  current_user["user_id"],
                "password": hash_password(new_plain),
            },
        )
        return {"message": "密码修改成功"}

    finally:
        await release_connection("redis",  redis_conn)
        await release_connection("mysql",  mysql_conn)
