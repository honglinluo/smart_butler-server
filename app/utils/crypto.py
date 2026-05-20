"""
【模块说明】RSA 加密工具 — 密码安全传输的底层实现

负责管理服务器的 RSA 密钥对，实现密码的加密传输：
  - 生成和保存 RSA 密钥对（首次启动时自动创建，保存在 config/server_rsa.pem）
  - 提供公钥给前端（前端用公钥加密密码）
  - 用私钥解密前端传来的加密密码（只有服务器能解密）

【为什么要用 RSA？】
  即使网络传输被截获，攻击者拿到的是加密后的密文，无法得到原始密码。
  RSA 是非对称加密：公钥加密的内容只有私钥才能解密，私钥只在服务器上。

【nonce（一次性码）】
  每次获取公钥时还附带一个随机 nonce（5 分钟有效，用完即销毁），
  防止"重放攻击"——有人截获了加密数据包，再发一次也没用，因为 nonce 已消耗。

RSA 非对称加密管理器 — 密码加密传输

流程：
  1. 客户端  GET /auth/public-key
             ← {public_key_pem, key_nonce, expires_in, algorithm}
  2. 客户端  用 RSA-OAEP-SHA256 公钥加密明文密码，Base64 编码
  3. 客户端  POST /auth/login (或 register / change-password)
             → {username, encrypted_password, key_nonce}
  4. 服务端  验证 key_nonce 有效（Redis 一次性 key，防重放攻击）
             → 用私钥解密 → 得到明文密码
             → 按原有规则做格式校验 / bcrypt 哈希

密钥管理：
  - 私钥文件：config/server_rsa.pem（PEM PKCS8 格式，无密码保护）
  - 首次启动自动生成并保存；多 worker 进程通过共享文件使用同一密钥
  - 密钥长度：2048 bit（RSA-OAEP 最大有效载荷 ≈ 190 字节，足以传输任何密码）

客户端 JS 参考示例：
  const resp   = await fetch('/auth/public-key');
  const {public_key_pem, key_nonce} = await resp.json();

  const pem    = public_key_pem.replace(/-----.*?-----/g, '').replace(/\\s/g, '');
  const derBuf = Uint8Array.from(atob(pem), c => c.charCodeAt(0));
  const pubKey = await crypto.subtle.importKey(
      'spki', derBuf.buffer,
      {name:'RSA-OAEP', hash:'SHA-256'}, false, ['encrypt']
  );
  const encBuf = await crypto.subtle.encrypt(
      {name:'RSA-OAEP'}, pubKey,
      new TextEncoder().encode(plainPassword)
  );
  const encrypted_password = btoa(String.fromCharCode(...new Uint8Array(encBuf)));
  // 提交 {encrypted_password, key_nonce}
"""
from __future__ import annotations

import base64
import logging
import secrets
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from app.utils.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)

_KEY_FILE       = PROJECT_ROOT / "config" / "server_rsa.pem"
_KEY_BITS       = 2048
_NONCE_TTL      = 300   # 5 min（与客户端拿到公钥到提交请求的最长等待时间匹配）
_NONCE_PREFIX   = "auth:nonce:"


# ══════════════════════════════════════════════════════════════════════════════
# 私钥加载 / 生成（进程级单例）
# ══════════════════════════════════════════════════════════════════════════════

_private_key: Optional[RSAPrivateKey] = None


def _load_or_generate_key() -> RSAPrivateKey:
    """加载已有私钥文件，不存在则生成并持久化。"""
    _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)

    if _KEY_FILE.exists():
        try:
            pem_bytes = _KEY_FILE.read_bytes()
            key = serialization.load_pem_private_key(pem_bytes, password=None)
            logger.info("[Crypto] 已加载 RSA 私钥: %s", _KEY_FILE)
            return key  # type: ignore[return-value]
        except Exception as exc:
            logger.warning("[Crypto] 私钥文件损坏，重新生成: %s", exc)

    # 生成新密钥对
    key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_BITS)
    pem = key.private_bytes(
        encoding   =serialization.Encoding.PEM,
        format     =serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    _KEY_FILE.write_bytes(pem)
    _KEY_FILE.chmod(0o600)  # 仅 owner 可读
    logger.info("[Crypto] 已生成并保存新 RSA 私钥: %s", _KEY_FILE)
    return key


def _get_private_key() -> RSAPrivateKey:
    global _private_key
    if _private_key is None:
        _private_key = _load_or_generate_key()
    return _private_key


def get_public_key_pem() -> str:
    """返回 PEM 格式公钥字符串（SubjectPublicKeyInfo，供客户端 import）。"""
    return _get_private_key().public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format  =serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


# ══════════════════════════════════════════════════════════════════════════════
# Nonce 管理（防重放）
# ══════════════════════════════════════════════════════════════════════════════

def generate_nonce() -> str:
    """生成 32 字节随机 nonce（hex 字符串）。"""
    return secrets.token_hex(32)


def nonce_redis_key(nonce: str) -> str:
    return f"{_NONCE_PREFIX}{nonce}"


# ══════════════════════════════════════════════════════════════════════════════
# 解密
# ══════════════════════════════════════════════════════════════════════════════

def decrypt_password(encrypted_b64: str) -> str:
    """用 RSA-OAEP-SHA256 私钥解密 Base64 编码的密文，返回明文密码字符串。

    Raises:
        ValueError: 密文格式错误或解密失败（统一返回模糊错误信息，避免信息泄露）
    """
    try:
        cipher_bytes = base64.b64decode(encrypted_b64)
    except Exception:
        raise ValueError("密码加密格式错误（非合法 Base64）")

    try:
        plain_bytes = _get_private_key().decrypt(
            cipher_bytes,
            padding.OAEP(
                mgf      =padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label    =None,
            ),
        )
    except Exception:
        raise ValueError("密码解密失败，请重新获取公钥后再试")

    try:
        return plain_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("密码解密后内容非合法 UTF-8")


# ══════════════════════════════════════════════════════════════════════════════
# nonce TTL 常量（供外部使用）
# ══════════════════════════════════════════════════════════════════════════════

NONCE_TTL = _NONCE_TTL
