# 数据库模块使用指南

本文档说明如何使用 Hermes Multi-Agent System 的数据库模块。

## 模块结构

```
app/database/
├── __init__.py              # 模块入口
├── base.py                  # 数据库基类
├── mysql.py                 # MySQL 数据库实现
├── redis.py                 # Redis 数据库实现
├── elasticsearch.py         # Elasticsearch 数据库实现
├── pool.py                  # 连接池管理
└── examples.py              # 使用示例
```

## 核心特性

### 1. 数据库基类 (`DatabaseBase`)
所有数据库类都继承自此基类，提供统一的接口：
- `connect()` - 建立连接
- `disconnect()` - 断开连接
- `health_check()` - 健康检查
- `create()` - 插入数据
- `read()` - 读取数据
- `update()` - 更新数据
- `delete()` - 删除数据
- `batch_create()` - 批量插入
- `batch_read()` - 批量读取
- `batch_delete()` - 批量删除

### 2. MySQL 数据库 (`MySQLDatabase`)
支持 MySQL 数据库操作：
```python
mysql_db = MySQLDatabase({
    "url": "mysql+pymysql://user:pass@localhost/db",
    "pool_size": 10,
    "max_overflow": 20,
})

await mysql_db.connect()
await mysql_db.create(table="users", data={"name": "Alice"})
results = await mysql_db.read(table="users", where={"name": "Alice"})
await mysql_db.update(table="users", data={"age": 26}, where={"name": "Alice"})
await mysql_db.delete(table="users", where={"name": "Alice"})
await mysql_db.disconnect()
```

### 3. Redis 数据库 (`RedisDatabase`)
支持 Redis 操作，包括列表操作和多 DB 支持：

#### 基本操作
```python
redis_db = RedisDatabase({
    "url": "redis://localhost:6379",
    "db": 0,
})

await redis_db.connect()
await redis_db.create("key", "value", ttl=3600)
value = await redis_db.read("key")
await redis_db.update("key", "new_value")
await redis_db.delete("key")
```

#### 列表操作
```python
# 向列表添加单个元素
await redis_db.push_to_list("my_list", {"id": 1, "name": "Alice"})

# 向列表批量添加多个元素
await redis_db.push_multiple_to_list("my_list", [
    {"id": 1, "name": "Alice"},
    {"id": 2, "name": "Bob"},
])

# 读取列表元素
items = await redis_db.read_list("my_list", start=0, end=-1)

# 获取列表长度
length = await redis_db.get_list_length("my_list")

# 删除列表
await redis_db.delete_list("my_list")
```

#### 多 DB 支持
```python
# 切换到 DB 1
await redis_db.select_db(1)
await redis_db.create("session:123", {"user_id": 1})

# 切换回 DB 0
await redis_db.select_db(0)
```

#### 高级操作
```python
# 检查键是否存在
exists = await redis_db.exists("key")

# 获取键的 TTL
ttl = await redis_db.get_ttl("key")

# 增加键的值
count = await redis_db.increment("counter", 1)

# 清空数据库
await redis_db.flushdb()
```

### 4. Elasticsearch 数据库 (`ElasticsearchDatabase`)
支持 Elasticsearch 操作，包括向量搜索：

#### 基本操作
```python
es_db = ElasticsearchDatabase({
    "url": "http://localhost:9200",
    "index_prefix": "hermes",
})

await es_db.connect()

# 创建索引
await es_db.create_index("chat_history", mappings={
    "properties": {
        "user_id": {"type": "keyword"},
        "message": {"type": "text"},
        "embedding": {"type": "dense_vector", "dims": 768},
    }
})

# 创建文档
await es_db.create(
    index="chat_history",
    doc_id="1",
    document={"user_id": "user_1", "message": "Hello"}
)

# 读取文档
doc = await es_db.read(index="chat_history", doc_id="1")

# 更新文档
await es_db.update(index="chat_history", doc_id="1", document={"message": "Hi"})

# 删除文档
await es_db.delete(index="chat_history", doc_id="1")
```

#### 批量操作
```python
# 批量创建文档
await es_db.batch_create("chat_history", [
    {"_id": "1", "message": "Hello"},
    {"_id": "2", "message": "World"},
])

# 批量读取文档
docs = await es_db.batch_read("chat_history", ["1", "2"])

# 批量删除文档
await es_db.batch_delete("chat_history", ["1", "2"])
```

#### 搜索操作
```python
# 关键词搜索
result = await es_db.search(
    index="chat_history",
    query={"match": {"message": "hello"}},
    size=10
)

# 向量相似度搜索
similar_docs = await es_db.vector_search(
    index="chat_history",
    vector=[0.1, 0.2, 0.3, ...],  # 768 维向量
    top_k=5
)

# 按条件删除
deleted_count = await es_db.delete_by_query(
    index="chat_history",
    query={"term": {"user_id": "user_1"}}
)

# 计数
count = await es_db.count_documents("chat_history")
```

### 5. 连接池 (`DatabaseConnectionPool`)
管理数据库连接，支持：
- 最小/最大连接数配置
- 自动排队机制
- 心跳检测
- 连接健康管理

#### 使用连接池
```python
from app.database import pool_manager, MySQLDatabase

# 注册 MySQL 连接池
await pool_manager.register_pool(
    pool_name="mysql_pool",
    connection_class=MySQLDatabase,
    config={
        "url": "mysql+pymysql://user:pass@localhost/db",
    },
    min_connections=5,      # 最小连接数
    max_connections=20,     # 最大连接数
    connection_timeout=30,  # 获取连接超时 (秒)
)

# 获取连接
connection = await pool_manager.acquire("mysql_pool", timeout=5)
if connection:
    # 使用连接
    await connection.read(table="users")
    
    # 释放连接
    await pool_manager.release("mysql_pool", connection)

# 获取连接池统计信息
stats = pool_manager.get_pool_statistics("mysql_pool")
print(f"可用连接: {stats['available_connections']}")
print(f"忙碌连接: {stats['busy_connections']}")
print(f"等待请求: {stats['waiting_requests']}")

# 关闭连接池
await pool_manager.close_all()
```

#### 连接池特性
- **自动排队**：当所有连接都在使用且达到最大连接数时，请求会进入等待队列
- **心跳检测**：后台定期检查连接健康状态，自动删除不健康的连接
- **连接回收**：过期的连接会自动关闭并替换
- **统计信息**：提供详细的连接池统计和监控数据

## 快速开始

### 1. 直接使用单个数据库

```python
import asyncio
from app.database import MySQLDatabase

async def main():
    db = MySQLDatabase({
        "url": "mysql+pymysql://root:password@localhost/mydb",
    })
    
    await db.connect()
    
    # 进行数据库操作
    await db.create(table="users", data={"name": "Alice", "age": 25})
    
    await db.disconnect()

asyncio.run(main())
```

### 2. 使用连接池进行生产环境部署

```python
import asyncio
from app.database import pool_manager, MySQLDatabase, RedisDatabase

async def init_pools():
    """初始化所有连接池"""
    
    # MySQL 连接池
    await pool_manager.register_pool(
        pool_name="mysql",
        connection_class=MySQLDatabase,
        config={
            "url": "mysql+pymysql://root:password@localhost/mydb",
        },
        min_connections=5,
        max_connections=20,
    )
    
    # Redis 连接池
    await pool_manager.register_pool(
        pool_name="redis",
        connection_class=RedisDatabase,
        config={
            "url": "redis://localhost:6379",
            "db": 0,
        },
        min_connections=3,
        max_connections=10,
    )

async def use_database():
    """使用数据库"""
    
    # 从 MySQL 连接池获取连接
    mysql_conn = await pool_manager.acquire("mysql")
    if mysql_conn:
        await mysql_conn.read(table="users")
        await pool_manager.release("mysql", mysql_conn)
    
    # 从 Redis 连接池获取连接
    redis_conn = await pool_manager.acquire("redis")
    if redis_conn:
        await redis_conn.read("key")
        await pool_manager.release("redis", redis_conn)

async def main():
    await init_pools()
    await use_database()
    await pool_manager.close_all()

asyncio.run(main())
```

## 配置示例

### MySQL 配置
```python
mysql_config = {
    "url": "mysql+pymysql://user:password@host:3306/database",
    "pool_size": 10,           # 连接池大小
    "max_overflow": 20,        # 溢出连接数
    "pool_recycle": 3600,      # 连接回收时间 (秒)
    "echo": False,             # 是否打印 SQL
}
```

### Redis 配置
```python
redis_config = {
    "url": "redis://host:6379",
    "db": 0,                   # 默认 DB (0-15)
    "encoding": "utf-8",
    "pool_size": 10,           # 连接池大小
    "max_connections": 50,     # 最大连接数
    "decode_responses": True,  # 自动解码
}
```

### Elasticsearch 配置
```python
es_config = {
    "url": "http://localhost:9200",  # 可以是列表
    "index_prefix": "hermes",
    "username": "user",               # 可选
    "password": "password",           # 可选
    "timeout": 10,
    "max_retries": 3,
    "vector_field": "embedding",
}
```

## 错误处理

```python
import asyncio
from app.database import MySQLDatabase

async def safe_db_operation():
    db = MySQLDatabase(config)
    
    try:
        if not await db.connect():
            print("连接失败")
            return
        
        # 进行操作
        result = await db.read(table="users")
        
        # 检查健康状态
        if not await db.health_check():
            print("连接不健康")
            return
        
    except Exception as e:
        print(f"操作出错: {str(e)}")
    
    finally:
        await db.disconnect()
```

## 监控和调试

```python
# 获取连接池统计信息
stats = pool_manager.get_pool_statistics("mysql_pool")
print(f"总连接数: {stats['total_connections']}")
print(f"可用连接: {stats['available_connections']}")
print(f"忙碌连接: {stats['busy_connections']}")
print(f"等待请求: {stats['waiting_requests']}")
print(f"平均等待时间: {stats['average_wait_time']:.2f}s")

# 获取连接详细信息
details = pool_manager.pools["mysql_pool"].get_connection_details()
for conn_info in details:
    print(f"连接 {conn_info['pool_id']}: 状态={conn_info['status']}, "
          f"使用次数={conn_info['use_count']}")
```

## 运行示例

```bash
# 查看示例代码
cat app/database/examples.py

# 运行示例 (需要已启动的数据库)
python -m app.database.examples
```

## 最佳实践

1. **使用连接池**：生产环境中始终使用连接池而不是直接创建连接
2. **及时释放连接**：在 `try-finally` 块中确保连接被释放
3. **设置合理的超时**：避免长时间等待连接
4. **监控连接池**：定期检查连接池统计信息，及时发现问题
5. **异常处理**：对所有数据库操作进行异常处理
6. **使用 Redis 列表**：充分利用 Redis 列表功能进行多数据插入同一个 key
7. **心跳检测**：连接池会自动进行心跳检测，无需手动干预

## 常见问题

**Q: 连接数超过最大值会怎样？**
A: 新请求会进入等待队列，直到有连接释放或超时。

**Q: 如何处理不健康的连接？**
A: 连接池会自动检测并删除不健康的连接，然后创建新的替代连接。

**Q: Redis 支持多少个 DB？**
A: Redis 默认支持 0-15 的 16 个 DB，可以使用 `select_db()` 方法切换。

**Q: 如何向同一个 Redis key 添加多个数据？**
A: 使用列表功能，调用 `push_to_list()` 或 `push_multiple_to_list()` 方法。
