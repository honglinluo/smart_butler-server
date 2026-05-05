"""Redis 数据库类 - 实现 Redis 的增删改查操作，支持多个 DB"""

from typing import Any, Dict, List, Optional, Union
import redis
from redis import Redis, ConnectionPool
import json
import logging

from .base import DatabaseBase

logger = logging.getLogger(__name__)


class RedisDatabase(DatabaseBase):
    """Redis 数据库类，支持多个 DB 和列表操作"""
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化 Redis 数据库类
        
        Args:
            config: 数据库配置信息，应包含:
                - url: Redis 连接字符串 (格式: redis://host:port)
                - db: 默认数据库号 (0-15)
                - encoding: 编码方式 (默认 utf-8)
                - pool_size: 连接池大小 (默认 10)
                - max_connections: 最大连接数 (默认 50)
                - decode_responses: 是否自动解码 (默认 True)
        """
        super().__init__(config)
        self.redis_client = None
        self.connection_pool = None
        
        # 提取配置参数
        self.url = config.get("url", "redis://localhost:6379")
        self.db = config.get("db", 0)
        self.encoding = config.get("encoding", "utf-8")
        self.pool_size = config.get("pool_size", 10)
        self.max_connections = config.get("max_connections", 50)
        self.decode_responses = config.get("decode_responses", True)
    
    async def connect(self) -> bool:
        """
        建立 Redis 连接
        
        Returns:
            bool: 连接是否成功
        """
        try:
            # 创建连接池
            self.connection_pool = ConnectionPool.from_url(
                self.url,
                db=self.db,
                encoding=self.encoding,
                decode_responses=self.decode_responses,
                max_connections=self.max_connections,
            )
            
            # 创建 Redis 客户端
            self.redis_client = Redis(
                connection_pool=self.connection_pool,
                decode_responses=self.decode_responses,
            )
            
            # 测试连接
            self.redis_client.ping()
            
            self.is_connected = True
            logger.info(f"Redis 数据库连接成功: {self.url} (DB {self.db})")
            return True
            
        except Exception as e:
            logger.error(f"Redis 数据库连接失败: {str(e)}")
            self.is_connected = False
            return False
    
    async def disconnect(self) -> bool:
        """
        断开 Redis 连接
        
        Returns:
            bool: 操作是否成功
        """
        try:
            if self.redis_client:
                self.redis_client.close()
            
            if self.connection_pool:
                self.connection_pool.disconnect()
            
            self.is_connected = False
            logger.info("Redis 数据库连接已断开")
            return True
            
        except Exception as e:
            logger.error(f"断开 Redis 连接时出错: {str(e)}")
            return False
    
    async def health_check(self) -> bool:
        """
        健康检查，验证连接是否仍然有效
        
        Returns:
            bool: 连接是否健康
        """
        try:
            if not self.is_connected or not self.redis_client:
                return False
            
            self.redis_client.ping()
            return True
            
        except Exception as e:
            logger.warning(f"Redis 健康检查失败: {str(e)}")
            self.is_connected = False
            return False
    
    async def switch_db(self, db_name: str) -> bool:
        """
        切换到指定的数据库
        
        Args:
            db_name: 数据库名 (实际上是数据库号)
            
        Returns:
            bool: 切换是否成功
        """
        try:
            if not self.is_connected or not self.redis_client:
                return False
            
            db = int(db_name)
            self.redis_client.select(db)
            self.db = db
            logger.info(f"切换到 Redis 数据库: {db}")
            return True
            
        except Exception as e:
            logger.error(f"切换 Redis 数据库失败: {str(e)}")
            return False
    
    async def create(self, key: str, value: Any, ttl: Optional[int] = None, **kwargs) -> bool:
        """
        创建/设置键值对
        
        Args:
            key: 键
            value: 值 (支持字符串、数字、字典、列表)
            ttl: 过期时间 (秒)，None 表示不过期
            **kwargs: 其他参数
            
        Returns:
            bool: 操作是否成功
        """
        try:
            if not self.is_connected:
                return False
            
            # 如果值是字典或列表，转为 JSON
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            
            if ttl:
                self.redis_client.setex(key, ttl, value)
            else:
                self.redis_client.set(key, value)
            
            logger.debug(f"成功设置 Redis 键: {key}")
            return True
            
        except Exception as e:
            logger.error(f"设置 Redis 键失败: {str(e)}")
            return False
    
    async def read(self, key: str, **kwargs) -> Optional[Any]:
        """
        读取键对应的值
        
        Args:
            key: 键
            **kwargs: 其他参数
            
        Returns:
            Optional[Any]: 值，如果不存在则返回 None
        """
        try:
            if not self.is_connected:
                return None
            
            value = self.redis_client.get(key)
            
            if value is None:
                return None
            
            # 尝试解析 JSON
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return value
            
        except Exception as e:
            logger.error(f"读取 Redis 键失败: {str(e)}")
            return None
    
    async def update(self, key: str, value: Any, ttl: Optional[int] = None, **kwargs) -> bool:
        """
        更新键对应的值 (与 create 相同)
        
        Args:
            key: 键
            value: 新值
            ttl: 过期时间 (秒)
            **kwargs: 其他参数
            
        Returns:
            bool: 操作是否成功
        """
        return await self.create(key, value, ttl, **kwargs)
    
    async def delete(self, key: str, **kwargs) -> bool:
        """
        删除键
        
        Args:
            key: 键
            **kwargs: 其他参数
            
        Returns:
            bool: 操作是否成功
        """
        try:
            if not self.is_connected:
                return False
            
            result = self.redis_client.delete(key)
            logger.debug(f"成功删除 Redis 键: {key}")
            return result > 0
            
        except Exception as e:
            logger.error(f"删除 Redis 键失败: {str(e)}")
            return False
    
    async def batch_create(self, data: Dict[str, Any], ttl: Optional[int] = None, **kwargs) -> bool:
        """
        批量设置键值对
        
        Args:
            data: 键值对字典
            ttl: 过期时间 (秒)
            **kwargs: 其他参数
            
        Returns:
            bool: 操作是否成功
        """
        try:
            if not self.is_connected or not data:
                return False
            
            pipe = self.redis_client.pipeline()
            
            for key, value in data.items():
                # 如果值是字典或列表，转为 JSON
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False)
                
                if ttl:
                    pipe.setex(key, ttl, value)
                else:
                    pipe.set(key, value)
            
            pipe.execute()
            logger.debug(f"成功批量设置 {len(data)} 个 Redis 键")
            return True
            
        except Exception as e:
            logger.error(f"批量设置 Redis 键失败: {str(e)}")
            return False
    
    async def batch_read(self, keys: List[str], **kwargs) -> Dict[str, Any]:
        """
        批量读取键值对
        
        Args:
            keys: 键列表
            **kwargs: 其他参数
            
        Returns:
            Dict[str, Any]: 键值对字典
        """
        try:
            if not self.is_connected or not keys:
                return {}
            
            values = self.redis_client.mget(keys)
            
            result = {}
            for key, value in zip(keys, values):
                if value is not None:
                    try:
                        result[key] = json.loads(value)
                    except (json.JSONDecodeError, TypeError):
                        result[key] = value
            
            return result
            
        except Exception as e:
            logger.error(f"批量读取 Redis 键失败: {str(e)}")
            return {}
    
    async def batch_delete(self, keys: List[str], **kwargs) -> bool:
        """
        批量删除键
        
        Args:
            keys: 键列表
            **kwargs: 其他参数
            
        Returns:
            bool: 操作是否成功
        """
        try:
            if not self.is_connected or not keys:
                return False
            
            result = self.redis_client.delete(*keys)
            logger.debug(f"成功批量删除 {result} 个 Redis 键")
            return True
            
        except Exception as e:
            logger.error(f"批量删除 Redis 键失败: {str(e)}")
            return False
    
    # ==================== 列表操作 ====================
    
    async def push_to_list(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """
        向列表左端添加元素 (LPUSH)
        
        Args:
            key: 列表键
            value: 要添加的值
            ttl: 列表过期时间
            
        Returns:
            bool: 操作是否成功
        """
        try:
            if not self.is_connected:
                return False
            
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            
            self.redis_client.lpush(key, value)
            
            if ttl:
                self.redis_client.expire(key, ttl)
            
            logger.debug(f"成功向列表 {key} 添加元素")
            return True
            
        except Exception as e:
            logger.error(f"向列表添加元素失败: {str(e)}")
            return False
    
    async def push_multiple_to_list(self, key: str, values: List[Any], ttl: Optional[int] = None) -> bool:
        """
        向列表批量添加多个元素
        
        Args:
            key: 列表键
            values: 值列表
            ttl: 列表过期时间
            
        Returns:
            bool: 操作是否成功
        """
        try:
            if not self.is_connected or not values:
                return False
            
            pipe = self.redis_client.pipeline()
            
            for value in values:
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False)
                pipe.lpush(key, value)
            
            if ttl:
                pipe.expire(key, ttl)
            
            pipe.execute()
            logger.debug(f"成功向列表 {key} 批量添加 {len(values)} 个元素")
            return True
            
        except Exception as e:
            logger.error(f"向列表批量添加元素失败: {str(e)}")
            return False
    
    async def read_list(self, key: str, start: int = 0, end: int = -1, **kwargs) -> List[Any]:
        """
        读取列表元素 (LRANGE)
        
        Args:
            key: 列表键
            start: 起始位置 (默认 0)
            end: 结束位置 (默认 -1 表示到末尾)
            **kwargs: 其他参数
            
        Returns:
            List[Any]: 元素列表
        """
        try:
            if not self.is_connected:
                return []
            
            values = self.redis_client.lrange(key, start, end)
            
            result = []
            for value in values:
                try:
                    result.append(json.loads(value))
                except (json.JSONDecodeError, TypeError):
                    result.append(value)
            
            return result
            
        except Exception as e:
            logger.error(f"读取列表失败: {str(e)}")
            return []
    
    async def get_list_length(self, key: str) -> int:
        """
        获取列表长度 (LLEN)
        
        Args:
            key: 列表键
            
        Returns:
            int: 列表长度
        """
        try:
            if not self.is_connected:
                return 0
            
            return self.redis_client.llen(key)
            
        except Exception as e:
            logger.error(f"获取列表长度失败: {str(e)}")
            return 0
    
    async def delete_list(self, key: str) -> bool:
        """
        删除列表
        
        Args:
            key: 列表键
            
        Returns:
            bool: 操作是否成功
        """
        try:
            if not self.is_connected:
                return False
            
            result = self.redis_client.delete(key)
            logger.debug(f"成功删除列表: {key}")
            return result > 0
            
        except Exception as e:
            logger.error(f"删除列表失败: {str(e)}")
            return False
    
    # ==================== 高级操作 ====================
    
    async def exists(self, key: str) -> bool:
        """
        检查键是否存在
        
        Args:
            key: 键
            
        Returns:
            bool: 键是否存在
        """
        try:
            if not self.is_connected:
                return False
            
            return self.redis_client.exists(key) > 0
            
        except Exception as e:
            logger.error(f"检查键是否存在失败: {str(e)}")
            return False
    
    async def get_ttl(self, key: str) -> int:
        """
        获取键的剩余生存时间 (秒)
        
        Args:
            key: 键
            
        Returns:
            int: TTL (秒)，-1 表示无过期时间，-2 表示键不存在
        """
        try:
            if not self.is_connected:
                return -2
            
            return self.redis_client.ttl(key)
            
        except Exception as e:
            logger.error(f"获取键的 TTL 失败: {str(e)}")
            return -2
    
    async def increment(self, key: str, amount: int = 1) -> Optional[int]:
        """
        增加键的值 (INCR/INCRBY)
        
        Args:
            key: 键
            amount: 增量 (默认 1)
            
        Returns:
            Optional[int]: 增加后的值
        """
        try:
            if not self.is_connected:
                return None
            
            if amount == 1:
                return self.redis_client.incr(key)
            else:
                return self.redis_client.incrby(key, amount)
            
        except Exception as e:
            logger.error(f"增加键的值失败: {str(e)}")
            return None
    
    async def scan_keys(self, pattern: str, count: int = 100) -> List[str]:
        """
        使用 SCAN 命令按模式扫描键（不阻塞，适合生产环境）

        Args:
            pattern: 键匹配模式（如 'user:*:init'）
            count: 每次扫描的建议数量

        Returns:
            List[str]: 匹配的键列表
        """
        try:
            if not self.is_connected:
                return []

            matched: List[str] = []
            cursor = 0
            while True:
                cursor, keys = self.redis_client.scan(cursor, match=pattern, count=count)
                matched.extend(keys)
                if cursor == 0:
                    break
            return matched

        except Exception as e:
            logger.error(f"SCAN 键失败 pattern={pattern}: {str(e)}")
            return []

    async def flushdb(self) -> bool:
        """
        清空当前数据库的所有键
        
        Returns:
            bool: 操作是否成功
        """
        try:
            if not self.is_connected:
                return False
            
            self.redis_client.flushdb()
            logger.warning(f"已清空 Redis DB {self.db} 的所有键")
            return True
            
        except Exception as e:
            logger.error(f"清空数据库失败: {str(e)}")
            return False
