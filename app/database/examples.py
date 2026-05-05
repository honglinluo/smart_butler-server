"""数据库模块使用示例和快速开始指南"""

import asyncio
import logging

import yaml
from app.database import (
    MySQLDatabase,
    RedisDatabase,
    ElasticsearchDatabase,
    ConnectionPoolManager,
    pool_manager,
)
from app.core.paths import PROJECT_ROOT

def _read_log_cfg() -> dict:
    try:
        p = PROJECT_ROOT / "config" / "system_config.yaml"
        if p.exists():
            with open(p, encoding="utf-8") as f:
                return (yaml.safe_load(f) or {}).get("logging", {})
    except Exception:
        pass
    return {}

_log_cfg = _read_log_cfg()
logging.basicConfig(
    level=getattr(logging, _log_cfg.get("level", "INFO").upper(), logging.INFO),
    format=_log_cfg.get("format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s"),
)
logger = logging.getLogger(__name__)


# ==================== 示例 1: 直接使用数据库类 ====================

async def example_mysql_direct():
    """直接使用 MySQL 数据库类的示例"""
    logger.info("=== MySQL 直接使用示例 ===")
    
    # 创建 MySQL 配置
    mysql_config = {
        "url": "mysql+pymysql://root:password@localhost:3306/test_db",
        "pool_size": 10,
        "max_overflow": 20,
        "pool_recycle": 3600,
    }
    
    # 创建数据库实例
    mysql_db = MySQLDatabase(mysql_config)
    
    # 连接
    if await mysql_db.connect():
        logger.info("MySQL 连接成功")
        
        # 创建表 (假设已存在)
        # 插入单条数据
        await mysql_db.create(
            table="users",
            data={
                "name": "Alice",
                "email": "alice@example.com",
                "age": 25,
            }
        )
        
        # 读取数据
        results = await mysql_db.read(
            table="users",
            where={"name": "Alice"},
        )
        logger.info(f"查询结果: {results}")
        
        # 更新数据
        await mysql_db.update(
            table="users",
            data={"age": 26},
            where={"name": "Alice"},
        )
        
        # 批量插入
        await mysql_db.batch_create(
            table="users",
            data_list=[
                {"name": "Bob", "email": "bob@example.com", "age": 30},
                {"name": "Charlie", "email": "charlie@example.com", "age": 28},
            ]
        )
        
        # 删除数据
        await mysql_db.delete(table="users", where={"name": "Bob"})
        
        # 断开连接
        await mysql_db.disconnect()
        logger.info("MySQL 连接已断开")
    else:
        logger.error("MySQL 连接失败")


async def example_redis_direct():
    """直接使用 Redis 数据库类的示例"""
    logger.info("=== Redis 直接使用示例 ===")
    
    # 创建 Redis 配置
    redis_config = {
        "url": "redis://localhost:6379",
        "db": 0,
        "encoding": "utf-8",
    }
    
    # 创建数据库实例
    redis_db = RedisDatabase(redis_config)
    
    # 连接
    if await redis_db.connect():
        logger.info("Redis 连接成功")
        
        # 设置键值对
        await redis_db.create("user:1", {"name": "Alice", "age": 25}, ttl=3600)
        
        # 读取数据
        value = await redis_db.read("user:1")
        logger.info(f"读取的值: {value}")
        
        # 向列表添加元素
        await redis_db.push_to_list("user_list", {"id": 1, "name": "Alice"})
        await redis_db.push_to_list("user_list", {"id": 2, "name": "Bob"})
        
        # 读取列表
        list_values = await redis_db.read_list("user_list")
        logger.info(f"列表内容: {list_values}")
        
        # 批量设置
        await redis_db.batch_create({
            "config:timeout": 30,
            "config:retries": 3,
            "config:debug": True,
        })
        
        # 增加键的值
        count = await redis_db.increment("request_count", 1)
        logger.info(f"请求计数: {count}")
        
        # 切换到 DB 1
        await redis_db.select_db(1)
        await redis_db.create("session:123", {"user_id": 1, "token": "abc123"})
        
        # 切换回 DB 0
        await redis_db.select_db(0)
        
        # 删除键
        await redis_db.delete("user:1")
        
        # 断开连接
        await redis_db.disconnect()
        logger.info("Redis 连接已断开")
    else:
        logger.error("Redis 连接失败")


async def example_elasticsearch_direct():
    """直接使用 Elasticsearch 数据库类的示例"""
    logger.info("=== Elasticsearch 直接使用示例 ===")
    
    # 创建 ES 配置
    es_config = {
        "url": "http://localhost:9200",
        "index_prefix": "hermes",
        "vector_field": "embedding",
    }
    
    # 创建数据库实例
    es_db = ElasticsearchDatabase(es_config)
    
    # 连接
    if await es_db.connect():
        logger.info("Elasticsearch 连接成功")
        
        # 创建索引
        await es_db.create_index(
            index="chat_history",
            mappings={
                "properties": {
                    "user_id": {"type": "keyword"},
                    "message": {"type": "text"},
                    "embedding": {"type": "dense_vector", "dims": 768},
                    "timestamp": {"type": "date"},
                }
            }
        )
        
        # 创建文档
        await es_db.create(
            index="chat_history",
            doc_id="1",
            document={
                "_id": "1",
                "user_id": "user_1",
                "message": "Hello, world!",
                "embedding": [0.1] * 768,
            }
        )
        
        # 读取文档
        doc = await es_db.read(index="chat_history", doc_id="1")
        logger.info(f"读取的文档: {doc}")
        
        # 批量创建文档
        await es_db.batch_create(
            index="chat_history",
            documents=[
                {
                    "_id": "2",
                    "user_id": "user_2",
                    "message": "Hi there!",
                    "embedding": [0.2] * 768,
                },
                {
                    "_id": "3",
                    "user_id": "user_3",
                    "message": "How are you?",
                    "embedding": [0.3] * 768,
                }
            ]
        )
        
        # 搜索文档
        search_result = await es_db.search(
            index="chat_history",
            query={"match": {"message": "hello"}},
            size=10
        )
        logger.info(f"搜索结果: {search_result}")
        
        # 计数
        count = await es_db.count_documents(index="chat_history")
        logger.info(f"文档总数: {count}")
        
        # 删除文档
        await es_db.delete(index="chat_history", doc_id="1")
        
        # 删除索引
        await es_db.delete_index(index="chat_history")
        
        # 断开连接
        await es_db.disconnect()
        logger.info("Elasticsearch 连接已断开")
    else:
        logger.error("Elasticsearch 连接失败")


# ==================== 示例 2: 使用连接池 ====================

async def example_connection_pool():
    """使用连接池的示例"""
    logger.info("=== 连接池使用示例 ===")
    
    # 配置
    mysql_config = {
        "url": "mysql+pymysql://root:password@localhost:3306/test_db",
        "pool_size": 10,
        "max_overflow": 20,
    }
    
    redis_config = {
        "url": "redis://localhost:6379",
        "db": 0,
    }
    
    # 注册 MySQL 连接池
    await pool_manager.register_pool(
        pool_name="mysql_pool",
        connection_class=MySQLDatabase,
        config=mysql_config,
        min_connections=3,
        max_connections=10,
    )
    
    # 注册 Redis 连接池
    await pool_manager.register_pool(
        pool_name="redis_pool",
        connection_class=RedisDatabase,
        config=redis_config,
        min_connections=2,
        max_connections=8,
    )
    
    # 从 MySQL 连接池获取连接
    mysql_conn = await pool_manager.acquire("mysql_pool")
    if mysql_conn:
        logger.info("从 MySQL 连接池获取连接成功")
        
        # 使用连接进行操作
        results = await mysql_conn.read(table="users", limit=5)
        logger.info(f"查询结果: {results}")
        
        # 释放连接
        await pool_manager.release("mysql_pool", mysql_conn)
        logger.info("MySQL 连接已释放")
    
    # 从 Redis 连接池获取连接
    redis_conn = await pool_manager.acquire("redis_pool")
    if redis_conn:
        logger.info("从 Redis 连接池获取连接成功")
        
        # 使用连接进行操作
        value = await redis_conn.read("key")
        logger.info(f"读取的值: {value}")
        
        # 释放连接
        await pool_manager.release("redis_pool", redis_conn)
        logger.info("Redis 连接已释放")
    
    # 获取连接池统计信息
    mysql_stats = pool_manager.get_pool_statistics("mysql_pool")
    logger.info(f"MySQL 连接池统计: {mysql_stats}")
    
    all_stats = pool_manager.get_all_statistics()
    logger.info(f"所有连接池统计: {all_stats}")
    
    # 关闭所有连接池
    await pool_manager.close_all()
    logger.info("所有连接池已关闭")


# ==================== 示例 3: 并发使用 ====================

async def example_concurrent_access():
    """并发访问连接池的示例"""
    logger.info("=== 并发访问示例 ===")
    
    # 注册连接池
    await pool_manager.register_pool(
        pool_name="redis_pool",
        connection_class=RedisDatabase,
        config={"url": "redis://localhost:6379", "db": 0},
        min_connections=2,
        max_connections=5,
    )
    
    async def worker(worker_id: int, num_requests: int):
        """工作线程"""
        for i in range(num_requests):
            conn = await pool_manager.acquire("redis_pool", timeout=5)
            if conn:
                # 模拟操作
                await conn.create(f"key:{worker_id}:{i}", f"value:{i}")
                await asyncio.sleep(0.1)
                await pool_manager.release("redis_pool", conn)
            else:
                logger.warning(f"Worker {worker_id} 获取连接失败")
    
    # 创建多个工作协程
    tasks = [worker(i, 3) for i in range(10)]
    await asyncio.gather(*tasks)
    
    # 获取最终统计
    stats = pool_manager.get_pool_statistics("redis_pool")
    logger.info(f"最终统计: {stats}")
    
    await pool_manager.close_all()


# ==================== 主函数 ====================

async def main():
    """主函数，运行所有示例"""
    try:
        # 运行直接使用示例 (需要已启动的数据库)
        # await example_mysql_direct()
        # await example_redis_direct()
        # await example_elasticsearch_direct()
        
        # 运行连接池示例
        await example_connection_pool()
        
        # 运行并发访问示例
        # await example_concurrent_access()
        
    except Exception as e:
        logger.error(f"示例执行出错: {str(e)}", exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
