# app/database — 完成说明

**更新日期**: 2026-05-05
**版本**: 2.6
**状态**: ✅ 核心功能已实现并验证

---

## 实现状态

| 文件 | 状态 | 核心特性 |
| --- | --- | --- |
| `base.py` | ✅ | DatabaseBase 抽象基类，统一 CRUD 接口 |
| `mysql.py` | ✅ | SQLAlchemy，execute_raw，switch_db |
| `redis.py` | ✅ | 列表操作，多 DB，scan_keys（非阻塞游标） |
| `elasticsearch.py` | ✅ | KNN 向量搜索，bulk 写入，delete_by_query |
| `pool.py` | ✅ | 统一连接池，心跳检测，排队机制 |

---

## 已实现功能

### MySQL（mysql.py）

- ✅ SQLAlchemy Core 执行 SQL
- ✅ `execute_raw(sql, params)` — 参数化原始 SQL，防 SQL 注入
- ✅ 事务支持
- ✅ `switch_db(name)` — 切换数据库

### Redis（redis.py）

- ✅ 字符串 GET / SET / SETEX / DEL
- ✅ 列表操作：LPUSH / LTRIM / LRANGE / LLEN
- ✅ `scan_keys(pattern)` — 基于 SCAN 命令非阻塞游标扫描（v2.2 新增）
- ✅ `getdel(key)` — 原子读取并删除（预取消费使用）
- ✅ `create(key, value, ttl)` — 用于分布式锁
- ✅ 多 DB 支持（switch_db / select_db）

### Elasticsearch（elasticsearch.py）

- ✅ 文档 CRUD（create / read / update / delete）
- ✅ `bulk` 批量写入
- ✅ `search(index, query, size)` — 全文检索
- ✅ `vector_search(index, vector, top_k, vector_field)` — KNN 向量检索
- ✅ `delete_by_query(index, query)` — 按条件删除
- ✅ `count_documents(index)` — 文档计数
- ✅ 索引创建 / 删除

### 连接池（pool.py）

- ✅ 最小/最大连接数配置
- ✅ 自动排队（asyncio.Queue + Event），超时报错
- ✅ 心跳检测（每 10 秒轮检可用连接）
- ✅ 过期连接自动回收（max_connection_lifetime=3600s）
- ✅ 统计接口（可用/忙碌/等待连接数）
- ✅ `verify_connections()` — 启动验证，移除不健康连接并补足最小数量

---

## 使用约定

```python
from app.database.pool import get_connection, release_connection

conn = await get_connection("redis", None)
try:
    client = conn.redis_client          # 同步 redis-py 客户端
    client.set(key, value, ex=300)
finally:
    await release_connection("redis", conn)
```

`get_connection(db_type, db_name)` 中 `db_name=None` 表示不切换数据库；
`db_name` 非空时自动调用 `switch_db`。

---

## 近期变更

### v2.2 — 2026-04-26

- **`redis.py` — 新增 `scan_keys(pattern)`**：程序关闭时批量扫描 `user:*:init`，
  使用 SCAN 游标代替 KEYS 命令，避免阻塞 Redis 主线程
