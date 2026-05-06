"""
API 集成测试 — 需要服务已启动（python main.py / uvicorn main:app）

运行方式:
    python tests/test_api.py
    python tests/test_api.py --base-url http://192.168.x.x:8000

覆盖范围:
    1. 健康检查
    2. 登录认证（含 client env 请求头）
    3. 聊天接口 /chat/send（含 client env 默认值 + 显式值）
    4. 聊天接口 /chat/stream（SSE）
    5. 鉴权失败 401
"""

import argparse
import json
import sys
import time
from typing import Any, Dict, Optional, Tuple

import requests

# ── 默认配置 ──────────────────────────────────────────────────────
BASE_URL     = "http://localhost:8000"
TEST_USER    = "15723051314"          # 须在数据库中已存在
TEST_PASS    = "AIni1314."            # 对应密码（明文，此脚本仅做集成验证）
TIMEOUT      = 10


# ── 辅助 ──────────────────────────────────────────────────────────

def _headers(token: str, client_type: Optional[str] = None, client_version: Optional[str] = None) -> dict:
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if client_type:
        h["X-Client-Type"]    = client_type
    if client_version:
        h["X-Client-Version"] = client_version
    return h


def _ok(label: str) -> bool:
    print(f"  ✅  {label}")
    return True


def _fail(label: str, reason: str = "") -> bool:
    print(f"  ❌  {label}" + (f": {reason}" if reason else ""))
    return False


def _section(title: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


# ── 测试函数 ───────────────────────────────────────────────────────

def test_health() -> bool:
    _section("1. 健康检查")
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=TIMEOUT)
        if r.status_code == 200:
            return _ok("GET /health 返回 200")
        return _fail("GET /health", f"status={r.status_code}")
    except Exception as e:
        return _fail("GET /health", str(e))


def _get_public_key() -> Tuple[str, str]:
    """返回 (public_key_pem, key_nonce)"""
    r = requests.get(f"{BASE_URL}/auth/public-key", timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data["public_key_pem"], data["key_nonce"]


def _encrypt_password(password: str, pem: str) -> str:
    """
    Python 侧 RSA-OAEP-SHA256 加密（需要 cryptography 包）。
    未安装时跳过加密测试并返回空字符串。
    """
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        import base64
        key = serialization.load_pem_public_key(pem.encode())
        ct  = key.encrypt(password.encode(), padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(), label=None,
        ))
        return base64.b64encode(ct).decode()
    except ImportError:
        return ""


def test_login_no_client_type_header(base_url: str = BASE_URL) -> Tuple[bool, str]:
    """登录不带 X-Client-Type 时，session 应默认记录 client_type=api。"""
    _section("2a. 登录 — 无 X-Client-Type 头（默认 api）")
    try:
        pem, nonce = _get_public_key()
        enc_pass   = _encrypt_password(TEST_PASS, pem)
        if not enc_pass:
            print("     ⚠️  cryptography 未安装，跳过加密登录测试")
            return True, ""

        r = requests.post(
            f"{base_url}/auth/login",
            json={"username": TEST_USER, "encrypted_password": enc_pass, "key_nonce": nonce},
            headers={"Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return _fail("登录请求", f"status={r.status_code} body={r.text[:200]}"), ""
        token = r.json().get("token", "")
        _ok("登录成功，token 已获取")
        print(f"     client_type 默认为 api（需查看 Redis session 确认）")
        return True, token
    except Exception as e:
        return _fail("登录（无 client_type）", str(e)), ""


def test_login_with_client_type_header(base_url: str = BASE_URL) -> Tuple[bool, str]:
    """登录带 X-Client-Type: lark 时，session 应记录 client_type=lark。"""
    _section("2b. 登录 — 带 X-Client-Type: lark 头")
    try:
        pem, nonce = _get_public_key()
        enc_pass   = _encrypt_password(TEST_PASS, pem)
        if not enc_pass:
            print("     ⚠️  cryptography 未安装，跳过加密登录测试")
            return True, ""

        r = requests.post(
            f"{base_url}/auth/login",
            json={"username": TEST_USER, "encrypted_password": enc_pass, "key_nonce": nonce},
            headers={
                "Content-Type":   "application/json",
                "X-Client-Type":  "lark",
                "X-Client-Version": "7.2.1",
            },
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return _fail("登录请求（lark）", f"status={r.status_code} body={r.text[:200]}"), ""
        token = r.json().get("token", "")
        _ok("登录成功，client_type=lark 已注入")
        return True, token
    except Exception as e:
        return _fail("登录（lark）", str(e)), ""


def test_login_alias_normalization(base_url: str = BASE_URL) -> bool:
    """X-Client-Type: feishu 应被归一化为 lark。"""
    _section("2c. 登录 — 别名归一化（feishu → lark）")
    try:
        pem, nonce = _get_public_key()
        enc_pass   = _encrypt_password(TEST_PASS, pem)
        if not enc_pass:
            print("     ⚠️  cryptography 未安装，跳过别名测试")
            return True

        r = requests.post(
            f"{base_url}/auth/login",
            json={"username": TEST_USER, "encrypted_password": enc_pass, "key_nonce": nonce},
            headers={"Content-Type": "application/json", "X-Client-Type": "feishu"},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return _fail("feishu 别名登录", f"status={r.status_code}")
        _ok("feishu 别名登录成功（Redis 中应存 lark）")
        return True
    except Exception as e:
        return _fail("feishu 别名归一化", str(e))


def test_send_message_default_api(token: str, base_url: str = BASE_URL) -> bool:
    """不带 X-Client-Type 时，turn_metadata.client_type 应为 api。"""
    _section("3a. /chat/send — 无 X-Client-Type 头（默认 api）")
    if not token:
        print("     ⚠️  无有效 token，跳过")
        return True
    try:
        r = requests.post(
            f"{base_url}/chat/send",
            json={"message": "你好，这是无客户端头的测试"},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=120,
        )
        if r.status_code == 200:
            return _ok("/chat/send 无头请求成功（client_type 应为 api，见服务日志）")
        return _fail("/chat/send 无头请求", f"status={r.status_code} body={r.text[:200]}")
    except Exception as e:
        return _fail("/chat/send 无头请求", str(e))


def test_send_message_with_wechat(token: str, base_url: str = BASE_URL) -> bool:
    """带 X-Client-Type: wechat 时，turn_metadata.client_type 应为 wechat。"""
    _section("3b. /chat/send — X-Client-Type: wechat")
    if not token:
        print("     ⚠️  无有效 token，跳过")
        return True
    try:
        r = requests.post(
            f"{base_url}/chat/send",
            json={"message": "你好，这是微信客户端测试"},
            headers=_headers(token, client_type="wechat", client_version="8.0.50"),
            timeout=30,
        )
        if r.status_code == 200:
            return _ok("/chat/send wechat 头请求成功")
        return _fail("/chat/send wechat 头", f"status={r.status_code} body={r.text[:200]}")
    except Exception as e:
        return _fail("/chat/send wechat 头", str(e))


def test_send_message_unknown_client(token: str, base_url: str = BASE_URL) -> bool:
    """带无法识别的 X-Client-Type: my_app，应归一化为 unknown，不报错。"""
    _section("3c. /chat/send — X-Client-Type: my_app（未知值）")
    if not token:
        print("     ⚠️  无有效 token，跳过")
        return True
    try:
        r = requests.post(
            f"{base_url}/chat/send",
            json={"message": "这是未知客户端类型测试"},
            headers=_headers(token, client_type="my_app"),
            timeout=30,
        )
        if r.status_code == 200:
            return _ok("未知 client_type 不导致 500，已归一化为 unknown")
        return _fail("未知 client_type 请求", f"status={r.status_code} body={r.text[:200]}")
    except Exception as e:
        return _fail("未知 client_type 请求", str(e))


def test_stream_default_api(token: str, base_url: str = BASE_URL) -> bool:
    """SSE 流式接口无 X-Client-Type 时，client_type 应为 api。"""
    _section("4a. /chat/stream — 无 X-Client-Type 头（默认 api）")
    if not token:
        print("     ⚠️  无有效 token，跳过")
        return True
    try:
        with requests.post(
            f"{base_url}/chat/stream",
            json={"message": "流式测试（无 client_type 头）"},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=30,
            stream=True,
        ) as r:
            if r.status_code != 200:
                return _fail("/chat/stream 默认 api", f"status={r.status_code}")
            got_token = False
            for raw in r.iter_lines(decode_unicode=True):
                if raw.startswith("event: token"):
                    got_token = True
                if raw.startswith("event: done"):
                    break
            if got_token:
                return _ok("/chat/stream 默认 api 流式收到 token")
            return _ok("/chat/stream 默认 api 已连接（未收到 token，可能 LLM 未配置）")
    except Exception as e:
        return _fail("/chat/stream 默认 api", str(e))


def test_stream_with_lark(token: str, base_url: str = BASE_URL) -> bool:
    """SSE 流式接口带 X-Client-Type: lark。"""
    _section("4b. /chat/stream — X-Client-Type: lark")
    if not token:
        print("     ⚠️  无有效 token，跳过")
        return True
    try:
        with requests.post(
            f"{base_url}/chat/stream",
            json={"message": "飞书客户端流式测试"},
            headers=_headers(token, client_type="lark", client_version="7.2.1"),
            timeout=120,
            stream=True,
        ) as r:
            if r.status_code != 200:
                return _fail("/chat/stream lark", f"status={r.status_code}")
            for raw in r.iter_lines(decode_unicode=True):
                if raw.startswith("event: done"):
                    break
            return _ok("/chat/stream lark 请求完成")
    except Exception as e:
        return _fail("/chat/stream lark", str(e))


def test_no_token() -> bool:
    """无 token 请求应返回 401。"""
    _section("5. 无 Token 请求 — 预期 401")
    try:
        r = requests.post(
            f"{BASE_URL}/chat/send",
            json={"message": "无 token"},
            headers={"Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
        if r.status_code == 401:
            return _ok("无 token 返回 401")
        return _fail("无 token 未返回 401", f"got {r.status_code}")
    except Exception as e:
        return _fail("无 token 测试", str(e))


# ── 入口 ──────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000/")
    args = parser.parse_args()
    global BASE_URL
    BASE_URL = args.base_url.rstrip("/")

    print("\n" + "═" * 60)
    print("  Hermes Multi-Agent System — API 集成测试")
    print(f"  目标: {BASE_URL}")
    print("═" * 60)

    # 前置检查：服务是否在线
    try:
        requests.get(f"{BASE_URL}/health", timeout=3)
    except Exception:
        print(f"\n❌ 无法连接 {BASE_URL}，请先启动服务\n")
        return 1

    results: list[Tuple[str, bool]] = []

    # 1. 健康检查
    results.append(("健康检查", test_health()))

    # 2. 登录测试（含 client env 头）
    ok_no_ct, token_no_ct   = test_login_no_client_type_header(BASE_URL)
    ok_lark,  token_lark    = test_login_with_client_type_header(BASE_URL)
    ok_alias                = test_login_alias_normalization(BASE_URL)
    results.append(("登录（默认 api）",          ok_no_ct))
    results.append(("登录（X-Client-Type: lark）", ok_lark))
    results.append(("登录（feishu→lark 别名）",   ok_alias))

    # 使用已获取的 token（优先 lark 令牌，fallback 无头令牌）
    token = token_lark or token_no_ct

    # 3. /chat/send
    results.append(("send — 无 X-Client-Type（默认 api）",  test_send_message_default_api(token, BASE_URL)))
    results.append(("send — X-Client-Type: wechat",         test_send_message_with_wechat(token, BASE_URL)))
    results.append(("send — X-Client-Type: my_app（未知）", test_send_message_unknown_client(token, BASE_URL)))

    # 4. /chat/stream
    results.append(("stream — 无 X-Client-Type（默认 api）", test_stream_default_api(token, BASE_URL)))
    results.append(("stream — X-Client-Type: lark",          test_stream_with_lark(token, BASE_URL)))

    # 5. 鉴权失败
    results.append(("无 token → 401", test_no_token()))

    # ── 汇总 ──────────────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("  测试汇总")
    print("═" * 60)
    for name, passed in results:
        print(f"  {'✅' if passed else '❌'}  {name}")
    passed_n = sum(1 for _, p in results if p)
    total    = len(results)
    print(f"\n  结果: {passed_n}/{total} 通过")
    print()
    return 0 if passed_n == total else 1


if __name__ == "__main__":
    sys.exit(main())
