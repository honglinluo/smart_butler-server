"""数据库连接池使用示例"""

import asyncio
from app.database.pool import get_connection, release_connection


async def example_usage():
    """使用示例"""
    
    # 获取 MySQL 连接并切换到指定数据库
    mysql_conn = await get_connection('mysql', 'hermes_db')
    if mysql_conn:
        try:
            # 使用连接进行操作
            # await mysql_conn.create('users', {'name': 'test', 'email': 'test@example.com'})
            print("MySQL 连接获取成功")
        finally:
            # 释放连接
            await release_connection('mysql', mysql_conn)
    
    # 获取 Redis 连接并切换到指定数据库
    redis_conn = await get_connection('redis', '1')  # DB 1
    if redis_conn:
        try:
            # 使用连接进行操作
            # await redis_conn.create('key', 'value')
            print("Redis 连接获取成功")
        finally:
            await release_connection('redis', redis_conn)
    
    # 获取 Elasticsearch 连接并设置索引前缀
    es_conn = await get_connection('elasticsearch', 'chat_logs')
    if es_conn:
        try:
            # 使用连接进行操作
            # await es_conn.create('message_1', {'content': 'hello', 'user': 'alice'})
            print("Elasticsearch 连接获取成功")
        finally:
            await release_connection('elasticsearch', es_conn)


if __name__ == "__main__":
    # 注意：需要先调用 initialize_pools() 初始化连接池
    # 在实际应用中，这会在应用启动时自动完成
    asyncio.run(example_usage())