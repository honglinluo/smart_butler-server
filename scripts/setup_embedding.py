"""scripts/setup_embedding.py

查询本地 Ollama 中的向量模型，让用户选择后写入 config/system_config.yaml。

用法：
    python scripts/setup_embedding.py
    python scripts/setup_embedding.py --model nomic-embed-text   # 直接指定
    python scripts/setup_embedding.py --list                     # 仅列出可用模型
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx
import yaml

os.environ.setdefault("PROJECT_ROOT", str(Path(__file__).parent.parent.resolve()))

ROOT = Path(os.environ["PROJECT_ROOT"])
CONFIG_PATH = ROOT / "config" / "system_config.yaml"

# 常见向量模型关键字（用于从全量模型列表中过滤）
EMBED_KEYWORDS = ("embed", "bge", "minilm", "e5-", "gte-", "nomic")

OLLAMA_API = "http://localhost:11434"


async def list_ollama_models(api_url: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{api_url}/api/tags")
        resp.raise_for_status()
        return resp.json().get("models", [])


def is_embed_model(name: str) -> bool:
    lower = name.lower()
    return any(kw in lower for kw in EMBED_KEYWORDS)


async def probe_dim(api_url: str, model_name: str) -> int:
    """向 Ollama 发送单条 embed 请求，通过结果推断向量维度。"""
    async with httpx.AsyncClient(timeout=60.0) as client:
        # 先尝试新版 /api/embed
        for endpoint, payload_key in [("/api/embed", "input"), ("/api/embeddings", "prompt")]:
            try:
                resp = await client.post(
                    f"{api_url}{endpoint}",
                    json={"model": model_name, payload_key: "hello"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    vecs = data.get("embeddings") or data.get("embedding")
                    if vecs:
                        vec = vecs[0] if isinstance(vecs[0], list) else vecs
                        return len(vec)
            except Exception:
                continue
    return 0


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


async def main():
    parser = argparse.ArgumentParser(description="配置 Hermes 向量模型")
    parser.add_argument("--list", action="store_true", help="仅列出可用的向量模型")
    parser.add_argument("--model", type=str, default="", help="直接指定模型名称（跳过交互）")
    parser.add_argument("--api-url", type=str, default=OLLAMA_API, help="Ollama API 地址")
    args = parser.parse_args()

    api_url = args.api_url.rstrip("/")

    # ── 拉取模型列表 ──────────────────────────────────────────
    print(f"\n正在连接 Ollama ({api_url})...")
    try:
        all_models = await list_ollama_models(api_url)
    except Exception as e:
        print(f"[错误] 无法连接 Ollama: {e}")
        print("请确认 Ollama 已启动：ollama serve")
        sys.exit(1)

    embed_models = [m for m in all_models if is_embed_model(m["name"])]
    other_models = [m for m in all_models if not is_embed_model(m["name"])]

    print(f"\n全部模型 {len(all_models)} 个，其中识别为向量模型 {len(embed_models)} 个：")
    if not embed_models:
        print("  (未找到向量模型)")
        print("\n推荐安装：")
        print("  ollama pull nomic-embed-text     # 768 维，274MB，中英文均可")
        print("  ollama pull mxbai-embed-large    # 1024 维，669MB，英文精度高")
        print("  ollama pull bge-m3               # 1024 维，1.2GB，多语言")
        if args.list:
            sys.exit(0)
        print("\n[提示] 安装向量模型后重新运行此脚本。")
        sys.exit(1)

    for i, m in enumerate(embed_models):
        size_mb = m.get("size", 0) // 1024 // 1024
        print(f"  [{i}] {m['name']}  ({size_mb} MB)")

    if other_models:
        print(f"\n其他非向量模型 {len(other_models)} 个（略）")

    if args.list:
        sys.exit(0)

    # ── 选择模型 ─────────────────────────────────────────────
    if args.model:
        chosen_name = args.model
        # 允许用户只输入短名，匹配完整名
        matches = [m["name"] for m in embed_models if args.model in m["name"]]
        if matches:
            chosen_name = matches[0]
        print(f"\n使用指定模型: {chosen_name}")
    elif len(embed_models) == 1:
        chosen_name = embed_models[0]["name"]
        print(f"\n自动选择唯一向量模型: {chosen_name}")
    else:
        try:
            idx = int(input(f"\n请输入模型编号 [0-{len(embed_models)-1}]: ").strip())
            chosen_name = embed_models[idx]["name"]
        except (ValueError, IndexError):
            print("[错误] 无效编号")
            sys.exit(1)

    # ── 探测向量维度 ──────────────────────────────────────────
    print(f"\n正在探测 {chosen_name} 的向量维度（发送测试请求）...")
    dim = await probe_dim(api_url, chosen_name)
    if dim == 0:
        print("[警告] 无法自动探测维度，将使用默认值 768。可在 config/system_config.yaml 中手动修正。")
        dim = 768
    else:
        print(f"  向量维度: {dim}")

    # ── 写入配置 ─────────────────────────────────────────────
    cfg = load_config()
    if "embedding" not in cfg:
        cfg["embedding"] = {}
    cfg["embedding"]["provider"]   = "ollama"
    cfg["embedding"]["api_url"]    = api_url
    cfg["embedding"]["model_name"] = chosen_name
    cfg["embedding"]["model_dim"]  = dim
    save_config(cfg)

    print(f"\n已写入配置文件 {CONFIG_PATH}:")
    print(f"  embedding.model_name = {chosen_name}")
    print(f"  embedding.model_dim  = {dim}")
    print(f"  embedding.api_url    = {api_url}")
    print("\n完成。启动服务时将自动校验并同步到数据库。")


if __name__ == "__main__":
    asyncio.run(main())
