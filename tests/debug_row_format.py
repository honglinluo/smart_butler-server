#!/usr/bin/env python3
"""快速调试脚本 - 检查数据库返回的行数据格式"""

import asyncio
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
os.environ.setdefault("PROJECT_ROOT", str(_PROJECT_ROOT.resolve()))

async def check_row_format():
    from app.database.pool import get_connection, release_connection

    # 获取 MySQL 连接
    mysql_conn = await get_connection("mysql", None)
    
    if mysql_conn:
        try:
            # 查询 llms 表
            sql = (
                "SELECT url, api_key, model_name, temperature, model_type "
                "FROM llms WHERE user_id = :user_id AND state = 1 "
                "ORDER BY id DESC LIMIT 1"
            )
            
            print(f"执行 SQL: {sql}")
            df = await mysql_conn.execute_raw(sql, {"user_id": "0"})
            
            print(f"\n返回的 DataFrame 类型: {type(df)}")
            
            if df is not None:
                print(f"DataFrame 长度: {len(df)}")
                print(f"DataFrame 列名: {list(df.columns)}")
                print(f"\nDataFrame 内容:\n{df}")
                
                if len(df) > 0:
                    row = df.iloc[0]
                    print(f"\n第一行数据: {row.to_dict()}")
            else:
                print("\n❌ 没有返回数据")
        finally:
            await release_connection("mysql", mysql_conn)

if __name__ == "__main__":
    asyncio.run(check_row_format())
