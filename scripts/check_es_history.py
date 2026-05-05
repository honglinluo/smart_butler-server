#!/usr/bin/env python3
"""检查 Elasticsearch 中指定用户的最近聊天记录。

用法:
    python scripts/check_es_history.py --user-id user_a... --size 10

脚本会使用项目的连接池获取 ES 连接（需要在项目根目录运行）。
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# 确保可以导入项目模块
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database.pool import get_connection, release_connection, close_all_pools
import yaml
from elasticsearch import Elasticsearch


async def fetch_recent(user_id: str, size: int = 5):
    es_conn = None
    try:
        es_conn = await get_connection("elasticsearch", None)
        if not es_conn:
            print("无法获取 Elasticsearch 连接，请确认服务已启动且连接池已由 main.py 初始化。")
            return 1

        try:
            res = await es_conn.search(index=user_id, query={"match_all": {}}, size=size, from_=0)
        except Exception:
            # 连接池方式失败，退回到直接使用底层 ES 客户端（读取配置）
            cfg_path = os.path.join(str(ROOT), "config", "system_config.yaml")
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f)
                es_cfg = cfg.get("database", {}).get("elasticsearch", {})
                url = es_cfg.get("url", "http://localhost:9200")
                index_prefix = es_cfg.get("index_prefix", "")
            else:
                url = "http://localhost:9200"
                index_prefix = ""

            client = Elasticsearch([url])
            full_index = f"{index_prefix}_{user_id}" if index_prefix else user_id
            try:
                res = client.search(index=full_index, body={"query": {"match_all": {}}}, size=size)
            except Exception as e:
                print("直接使用 ES client 查询失败:", e)
                return 2
        hits = []
        import pdb;pdb.set_trace()
        hits = res.get("hits", {}).get("hits", [])

        if not hits:
            print(f"未在索引 {user_id} 中找到消息（返回 0 条）。")
            return 0

        for i, h in enumerate(hits, 1):
            src = h.get("_source", h) if isinstance(h, dict) else h
            role = src.get("role") if isinstance(src, dict) else None
            content = src.get("content") if isinstance(src, dict) else str(src)
            ts = src.get("timestamp") if isinstance(src, dict) else None
            doc_id = h.get("_id") if isinstance(h, dict) else None
            print(f"--- #{i} id={doc_id} role={role} timestamp={ts}\n{content}\n")

        return 0

    except Exception as e:
        print("查询 Elasticsearch 失败:", e)
        return 2
    finally:
        if es_conn:
            await release_connection("elasticsearch", es_conn)
        # 关闭所有连接池
        try:
            await close_all_pools()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="检查 ES 中的聊天记录")
    parser.add_argument("--user-id", required=True, help="用户 ID（作为索引名）")
    parser.add_argument("--size", type=int, default=int(os.getenv("CHAT_HISTORY_SIZE", "5")), help="要读取的最近消息数")

    args = parser.parse_args()

    code = asyncio.run(fetch_recent(args.user_id, args.size))
    sys.exit(code)


if __name__ == "__main__":
    main()
