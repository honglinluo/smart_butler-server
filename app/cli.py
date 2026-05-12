"""
【模块说明】Hermes CLI — 服务端管理命令行工具

这个命令行工具让管理员可以在终端直接对运行中的服务器执行管理操作，
不需要打开浏览器或调用 API。

【可用命令】
  reload-agents   — 重新加载所有 Agent 配置（在服务器修改了 agents_config.yaml 后使用）
  revectorize     — 重新向量化历史对话（更换了 Embedding 模型后使用）
                    可指定 --user-id 只处理某个用户，或 --date 只处理某天数据

【使用方式】
  python -m app.cli reload-agents  --host http://localhost:8000 --token your_token
  python -m app.cli revectorize    --user-id u123 --date 2025-01-01

Hermes CLI - 服务端管理工具

用法：
    python -m app.cli reload-agents  [--host ...] [--token ...]
    python -m app.cli revectorize    [--host ...] [--token ...] [--user-id UID] [--date YYYY-MM-DD]

环境变量（优先级低于命令行参数）：
    HERMES_HOST   服务器地址，默认 http://localhost:8000
    HERMES_TOKEN  认证 token
"""

import argparse
import os
import sys


def _http_client():
    try:
        import httpx
        return httpx
    except ImportError:
        print("缺少 httpx 依赖，请执行：pip install httpx", file=sys.stderr)
        sys.exit(1)


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def reload_agents(host: str, token: str) -> None:
    """向服务器发送重载代码 Agent 的请求。"""
    httpx = _http_client()
    url = f"{host.rstrip('/')}/agents/admin/reload"
    print(f"正在请求：POST {url}")
    try:
        resp = httpx.post(url, headers=_headers(token), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        print("重载成功！")
        print(f"  已加载模块:     {data.get('loaded_modules', [])}")
        print(f"  DB Agent 数量:  {data.get('db_agents_loaded', 0)}")
        print(f"  已注册 Agent:   {data.get('registered', [])}")
    except Exception as e:
        _handle_error(e)


def revectorize(host: str, token: str, user_id: str, date: str) -> None:
    """向服务器发送重向量化请求。"""
    httpx = _http_client()
    url = f"{host.rstrip('/')}/agents/admin/revectorize"

    payload = {}
    if user_id:
        payload["user_id"] = user_id
    if date:
        # 简单校验日期格式
        import re
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            print(f"日期格式不合法，应为 YYYY-MM-DD，得到：{date}", file=sys.stderr)
            sys.exit(1)
        payload["date"] = date

    desc_parts = []
    if user_id:
        desc_parts.append(f"用户={user_id}")
    if date:
        desc_parts.append(f"日期={date}")
    scope = "、".join(desc_parts) if desc_parts else "全量（所有用户、所有日期）"
    print(f"正在请求重向量化：{scope}")
    print(f"POST {url}  payload={payload or '{}（全量）'}")

    try:
        resp = httpx.post(url, json=payload, headers=_headers(token), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        print(f"\n任务已启动：")
        print(f"  用户范围: {data.get('user_id', '-')}")
        print(f"  日期范围: {data.get('date',    '-')}")
        print(f"  状态:     {data.get('status',  '-')}")
        print(f"\n{data.get('message', '')}")
        print("提示：进度可通过服务端日志（INFO 级别）实时查看。")
    except Exception as e:
        _handle_error(e)


def _handle_error(e) -> None:
    try:
        import httpx
        if isinstance(e, httpx.HTTPStatusError):
            print(f"请求失败（{e.response.status_code}）: {e.response.text}", file=sys.stderr)
            sys.exit(1)
    except ImportError:
        pass
    print(f"请求异常: {e}", file=sys.stderr)
    sys.exit(1)


def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--host",
        default=os.getenv("HERMES_HOST", "http://localhost:8000"),
        help="服务器地址（默认 http://localhost:8000）",
    )
    p.add_argument(
        "--token",
        default=os.getenv("HERMES_TOKEN", ""),
        help="认证 token（Bearer）",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Hermes 多智能体系统 CLI 管理工具",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="command")
    subparsers.required = True

    # ── reload-agents ────────────────────────────────────────────
    reload_parser = subparsers.add_parser(
        "reload-agents",
        help="重新扫描并注册所有服务端代码 Agent 及 DB Agent",
    )
    _add_common_args(reload_parser)

    # ── revectorize ──────────────────────────────────────────────
    rev_parser = subparsers.add_parser(
        "revectorize",
        help="重新向量化聊天历史并更新 ES 向量索引",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "重新向量化聊天历史并更新 ES 向量数据。\n\n"
            "示例：\n"
            "  全量重向量化:\n"
            "    python -m app.cli revectorize\n\n"
            "  指定用户:\n"
            "    python -m app.cli revectorize --user-id user123\n\n"
            "  指定日期:\n"
            "    python -m app.cli revectorize --date 2024-01-15\n\n"
            "  指定用户 + 日期:\n"
            "    python -m app.cli revectorize --user-id user123 --date 2024-01-15"
        ),
    )
    _add_common_args(rev_parser)
    rev_parser.add_argument(
        "--user-id",
        default="",
        metavar="UID",
        help="指定用户 ID（不填则处理所有用户）",
    )
    rev_parser.add_argument(
        "--date",
        default="",
        metavar="YYYY-MM-DD",
        help="指定日期，格式 YYYY-MM-DD（不填则不限日期）",
    )

    args = parser.parse_args()

    if args.command == "reload-agents":
        reload_agents(args.host, args.token)
    elif args.command == "revectorize":
        revectorize(args.host, args.token, args.user_id, args.date)


if __name__ == "__main__":
    main()
