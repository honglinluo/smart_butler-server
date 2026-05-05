"""数据库连接池管理 - 实现连接池、队列、心跳检测等功能"""

import asyncio
from typing import Any, Dict, List, Optional, Type, Tuple
from collections import deque
from datetime import datetime, timedelta
import logging
from enum import Enum

from .base import DatabaseBase
from .mysql import MySQLDatabase
from .redis import RedisDatabase
from .elasticsearch import ElasticsearchDatabase

logger = logging.getLogger(__name__)


class ConnectionStatus(Enum):
    """连接状态枚举"""
    AVAILABLE = "available"           # 可用
    BUSY = "busy"                     # 忙碌
    UNHEALTHY = "unhealthy"          # 不健康
    CLOSED = "closed"                 # 已关闭


class PooledConnection:
    """连接池中的连接包装类"""
    
    def __init__(self, connection: DatabaseBase, pool_id: str):
        """
        初始化连接包装类
        
        Args:
            connection: 数据库连接实例
            pool_id: 连接在池中的唯一 ID
        """
        self.connection = connection
        self.pool_id = pool_id
        self.status = ConnectionStatus.AVAILABLE
        self.last_used_at = datetime.now()
        self.created_at = datetime.now()
        self.use_count = 0
        self.last_heartbeat_at = datetime.now()
        self.heartbeat_interval = 30  # 心跳间隔 (秒)
    
    def mark_as_busy(self):
        """标记连接为忙碌状态"""
        self.status = ConnectionStatus.BUSY
        self.last_used_at = datetime.now()
        self.use_count += 1
    
    def mark_as_available(self):
        """标记连接为可用状态"""
        self.status = ConnectionStatus.AVAILABLE
        self.last_used_at = datetime.now()
    
    def mark_as_unhealthy(self):
        """标记连接为不健康状态"""
        self.status = ConnectionStatus.UNHEALTHY
    
    def is_expired(self, max_connection_lifetime: int = 3600) -> bool:
        """
        检查连接是否过期
        
        Args:
            max_connection_lifetime: 连接最大生存时间 (秒)
            
        Returns:
            bool: 连接是否过期
        """
        connection_age = (datetime.now() - self.created_at).total_seconds()
        return connection_age > max_connection_lifetime
    
    def needs_heartbeat(self) -> bool:
        """
        检查是否需要进行心跳检测
        
        Returns:
            bool: 是否需要心跳
        """
        time_since_heartbeat = (datetime.now() - self.last_heartbeat_at).total_seconds()
        return time_since_heartbeat >= self.heartbeat_interval
    
    def update_heartbeat(self):
        """更新心跳时间"""
        self.last_heartbeat_at = datetime.now()
    
    async def close(self):
        """关闭连接"""
        try:
            await self.connection.disconnect()
            self.status = ConnectionStatus.CLOSED
        except Exception as e:
            logger.error(f"关闭连接失败: {str(e)}")


class DatabaseConnectionPool:
    """数据库连接池管理器"""
    
    def __init__(
        self,
        connection_class: Type[DatabaseBase],
        config: Dict[str, Any],
        min_connections: int = 5,
        max_connections: int = 20,
        connection_timeout: int = 30,
        max_connection_lifetime: int = 3600,
    ):
        """
        初始化连接池
        
        Args:
            connection_class: 连接类 (MySQLDatabase, RedisDatabase 等)
            config: 数据库配置
            min_connections: 最小连接数
            max_connections: 最大连接数
            connection_timeout: 连接超时时间 (秒)
            max_connection_lifetime: 连接最大生存时间 (秒)
        """
        self.connection_class = connection_class
        self.config = config
        self.min_connections = min_connections
        self.max_connections = max_connections
        self.connection_timeout = connection_timeout
        self.max_connection_lifetime = max_connection_lifetime
        
        # 连接存储
        self.available_connections: deque[PooledConnection] = deque()  # 可用连接队列
        self.busy_connections: Dict[str, PooledConnection] = {}        # 忙碌连接字典
        self.all_connections: Dict[str, PooledConnection] = {}         # 所有连接
        
        # 等待队列
        self.waiting_queue: asyncio.Queue = asyncio.Queue()
        
        # 锁和事件
        self.lock = asyncio.Lock()
        self.connection_id_counter = 0
        self.pool_initialized = False
        
        # 统计信息
        self.stats = {
            "total_connections": 0,
            "available_connections": 0,
            "busy_connections": 0,
            "total_requests": 0,
            "waiting_requests": 0,
            "average_wait_time": 0.0,
            "max_connection_lifetime": max_connection_lifetime,
        }
        
        logger.info(
            f"初始化连接池: {connection_class.__name__}, "
            f"最小连接: {min_connections}, 最大连接: {max_connections}"
        )
    
    async def initialize(self):
        """初始化连接池，创建最小数量的连接"""
        async with self.lock:
            try:
                for _ in range(self.min_connections):
                    conn = await self._create_connection()
                    if conn:
                        self.available_connections.append(conn)
                
                self.pool_initialized = True
                logger.info(f"连接池已初始化，当前连接数: {len(self.all_connections)}")
                
                # 启动后台心跳任务
                asyncio.create_task(self._heartbeat_loop())
                
            except Exception as e:
                logger.error(f"初始化连接池失败: {str(e)}")
                self.pool_initialized = False
    
    async def _create_connection(self) -> Optional[PooledConnection]:
        """
        创建一个新的连接
        
        Returns:
            Optional[PooledConnection]: 新创建的连接包装对象
        """
        try:
            # 检查是否已达到最大连接数
            if len(self.all_connections) >= self.max_connections:
                logger.warning(f"连接池已达到最大连接数: {self.max_connections}")
                return None
            
            # 创建连接
            connection = self.connection_class(self.config)
            
            # 连接到数据库
            if await connection.connect():
                self.connection_id_counter += 1
                pool_id = f"{self.connection_class.__name__}_{self.connection_id_counter}"
                
                pooled_conn = PooledConnection(connection, pool_id)
                self.all_connections[pool_id] = pooled_conn
                
                logger.debug(f"成功创建新连接: {pool_id}")
                return pooled_conn
            else:
                logger.error(f"连接失败: {self.connection_class.__name__}")
                return None
                
        except Exception as e:
            logger.error(f"创建连接失败: {str(e)}")
            return None
    
    async def acquire(self, timeout: Optional[int] = None) -> Optional[DatabaseBase]:
        """
        从连接池获取一个连接
        
        Args:
            timeout: 等待超时时间 (秒)，None 表示使用默认超时
            
        Returns:
            Optional[DatabaseBase]: 数据库连接实例
        """
        timeout = timeout or self.connection_timeout
        wait_start = datetime.now()
        
        async with self.lock:
            self.stats["total_requests"] += 1
        
        try:
            # 1. 尝试从可用连接队列获取
            if self.available_connections:
                async with self.lock:
                    pooled_conn = self.available_connections.popleft()
                    
                    # 检查连接健康状态
                    if pooled_conn.status == ConnectionStatus.UNHEALTHY:
                        logger.warning(f"连接不健康，重新创建: {pooled_conn.pool_id}")
                        await pooled_conn.close()
                        del self.all_connections[pooled_conn.pool_id]
                        return await self.acquire(timeout)
                    
                    # 检查连接是否过期
                    if pooled_conn.is_expired(self.max_connection_lifetime):
                        logger.warning(f"连接已过期，重新创建: {pooled_conn.pool_id}")
                        await pooled_conn.close()
                        del self.all_connections[pooled_conn.pool_id]
                        return await self.acquire(timeout)
                    
                    pooled_conn.mark_as_busy()
                    self.busy_connections[pooled_conn.pool_id] = pooled_conn
                    
                    self.stats["available_connections"] = len(self.available_connections)
                    self.stats["busy_connections"] = len(self.busy_connections)
                    
                    logger.debug(f"从连接池获取连接: {pooled_conn.pool_id}")
                    return pooled_conn.connection
            
            # 2. 如果没有可用连接但未达到最大值，创建新连接
            async with self.lock:
                if len(self.all_connections) < self.max_connections:
                    pooled_conn = await self._create_connection()
                    if pooled_conn:
                        pooled_conn.mark_as_busy()
                        self.busy_connections[pooled_conn.pool_id] = pooled_conn
                        
                        self.stats["total_connections"] = len(self.all_connections)
                        self.stats["busy_connections"] = len(self.busy_connections)
                        
                        logger.debug(f"创建新连接: {pooled_conn.pool_id}")
                        return pooled_conn.connection
            
            # 3. 等待可用连接
            logger.debug(f"连接池满，等待可用连接 (超时: {timeout}s)")
            
            async with self.lock:
                self.stats["waiting_requests"] += 1
            
            # 使用信号机制等待连接释放
            acquired = False
            wait_event = asyncio.Event()
            
            # 添加到等待队列
            await self.waiting_queue.put(wait_event)
            
            # 等待信号
            try:
                await asyncio.wait_for(wait_event.wait(), timeout=timeout)
                acquired = True
            except asyncio.TimeoutError:
                logger.error(f"获取连接超时 ({timeout}s)")
                # 从等待队列中移除
                try:
                    self.waiting_queue.get_nowait()  # Remove if still there
                except asyncio.QueueEmpty:
                    pass
                return None
            
            # 收到信号后再次尝试获取可用连接
            if acquired and self.available_connections:
                async with self.lock:
                    pooled_conn = self.available_connections.popleft()
                    pooled_conn.mark_as_busy()
                    self.busy_connections[pooled_conn.pool_id] = pooled_conn
                    
                    # 计算等待时间
                    wait_time = (datetime.now() - wait_start).total_seconds()
                    old_avg = self.stats["average_wait_time"]
                    old_waiting = self.stats["waiting_requests"]
                    self.stats["average_wait_time"] = (
                        (old_avg * (old_waiting - 1) + wait_time) / old_waiting
                    )
                    
                    logger.debug(f"从等待队列获取连接: {pooled_conn.pool_id}")
                    return pooled_conn.connection
            
            return None
            
        except Exception as e:
            logger.error(f"获取连接出错: {str(e)}")
            return None
    
    async def release(self, connection: DatabaseBase) -> bool:
        """
        释放连接回到连接池
        
        Args:
            connection: 要释放的连接
            
        Returns:
            bool: 操作是否成功
        """
        try:
            async with self.lock:
                # 找到对应的连接包装对象
                pooled_conn = None
                for pool_id, pc in self.busy_connections.items():
                    if pc.connection == connection:
                        pooled_conn = pc
                        break
                
                if not pooled_conn:
                    logger.warning("释放的连接不在忙碌连接中")
                    return False
                
                # 移出忙碌列表
                del self.busy_connections[pooled_conn.pool_id]
                
                # 检查连接健康状态
                if pooled_conn.status == ConnectionStatus.UNHEALTHY:
                    logger.warning(f"释放不健康的连接: {pooled_conn.pool_id}")
                    await pooled_conn.close()
                    del self.all_connections[pooled_conn.pool_id]
                else:
                    # 放回可用连接队列
                    pooled_conn.mark_as_available()
                    self.available_connections.append(pooled_conn)
                
                self.stats["available_connections"] = len(self.available_connections)
                self.stats["busy_connections"] = len(self.busy_connections)
                
                # 唤醒等待的协程
                try:
                    wait_event = self.waiting_queue.get_nowait()
                    wait_event.set()
                except asyncio.QueueEmpty:
                    pass
                
                logger.debug(f"连接已释放: {pooled_conn.pool_id}")
                return True
                
        except Exception as e:
            logger.error(f"释放连接出错: {str(e)}")
            return False
    
    async def _heartbeat_loop(self):
        """心跳检测循环，定期检查连接健康状况"""
        while True:
            try:
                await asyncio.sleep(10)  # 每 10 秒检查一次
                
                unhealthy_connections = []
                
                async with self.lock:
                    for pool_id, pooled_conn in self.all_connections.items():
                        # 仅对可用连接进行心跳检测
                        if pooled_conn.status == ConnectionStatus.AVAILABLE and pooled_conn.needs_heartbeat():
                            # 异步执行心跳检测
                            try:
                                is_healthy = await pooled_conn.connection.health_check()
                                if is_healthy:
                                    pooled_conn.update_heartbeat()
                                    logger.debug(f"心跳检测通过: {pool_id}")
                                else:
                                    pooled_conn.mark_as_unhealthy()
                                    unhealthy_connections.append(pool_id)
                                    logger.warning(f"心跳检测失败: {pool_id}")
                            except Exception as e:
                                pooled_conn.mark_as_unhealthy()
                                unhealthy_connections.append(pool_id)
                                logger.error(f"心跳检测异常: {pool_id}, {str(e)}")
                
                # 移除不健康的连接
                for pool_id in unhealthy_connections:
                    async with self.lock:
                        if pool_id in self.all_connections:
                            pooled_conn = self.all_connections[pool_id]
                            await pooled_conn.close()
                            del self.all_connections[pool_id]
                            
                            # 从可用连接队列中移除
                            try:
                                self.available_connections.remove(pooled_conn)
                            except ValueError:
                                pass
                
                # 检查是否需要添加新连接以维持最小连接数
                async with self.lock:
                    current_total = len(self.all_connections)
                    if current_total < self.min_connections:
                        logger.info(f"连接数低于最小值，补充新连接")
                        for _ in range(self.min_connections - current_total):
                            new_conn = await self._create_connection()
                            if new_conn:
                                self.available_connections.append(new_conn)
                
            except Exception as e:
                logger.error(f"心跳检测循环异常: {str(e)}")
                await asyncio.sleep(10)
    
    async def verify_connections(self) -> bool:
        """
        验证所有可用连接的健康状态，移除不健康连接并补足至最小连接数。

        Returns:
            bool: 最终连接数是否达到最小连接数要求
        """
        unhealthy: list[PooledConnection] = []

        async with self.lock:
            for pooled_conn in list(self.available_connections):
                try:
                    is_healthy = await pooled_conn.connection.health_check()
                    if is_healthy:
                        pooled_conn.update_heartbeat()
                    else:
                        pooled_conn.mark_as_unhealthy()
                        unhealthy.append(pooled_conn)
                except Exception as e:
                    pooled_conn.mark_as_unhealthy()
                    unhealthy.append(pooled_conn)
                    logger.warning(f"连接验证异常: {pooled_conn.pool_id}, {e}")

            for pooled_conn in unhealthy:
                try:
                    self.available_connections.remove(pooled_conn)
                except ValueError:
                    pass
                await pooled_conn.close()
                self.all_connections.pop(pooled_conn.pool_id, None)

            shortage = self.min_connections - len(self.all_connections)
            if shortage > 0:
                logger.info(f"连接数不足（缺 {shortage} 个），正在补充...")
                for _ in range(shortage):
                    new_conn = await self._create_connection()
                    if new_conn:
                        self.available_connections.append(new_conn)

        final_count = len(self.all_connections)
        logger.info(
            f"连接池验证完成: 共 {final_count} 个连接"
            + (f"，已移除 {len(unhealthy)} 个不健康连接" if unhealthy else "")
        )
        return final_count >= self.min_connections

    async def close_all(self):
        """关闭所有连接"""
        async with self.lock:
            logger.info(f"关闭所有连接，共 {len(self.all_connections)} 个")
            
            for pool_id, pooled_conn in self.all_connections.items():
                try:
                    await pooled_conn.close()
                except Exception as e:
                    logger.error(f"关闭连接失败: {pool_id}, {str(e)}")
            
            self.all_connections.clear()
            self.busy_connections.clear()
            self.available_connections.clear()
            
            self.pool_initialized = False
            logger.info("所有连接已关闭")
    
    def get_statistics(self) -> Dict[str, Any]:
        """
        获取连接池统计信息
        
        Returns:
            Dict[str, Any]: 统计信息
        """
        self.stats["total_connections"] = len(self.all_connections)
        self.stats["available_connections"] = len(self.available_connections)
        self.stats["busy_connections"] = len(self.busy_connections)
        self.stats["waiting_requests"] = self.waiting_queue.qsize()
        
        return self.stats.copy()
    
    def get_connection_details(self) -> List[Dict[str, Any]]:
        """
        获取所有连接的详细信息
        
        Returns:
            List[Dict[str, Any]]: 连接详细信息列表
        """
        details = []
        
        for pool_id, pooled_conn in self.all_connections.items():
            details.append({
                "pool_id": pool_id,
                "status": pooled_conn.status.value,
                "use_count": pooled_conn.use_count,
                "created_at": pooled_conn.created_at.isoformat(),
                "last_used_at": pooled_conn.last_used_at.isoformat(),
                "last_heartbeat_at": pooled_conn.last_heartbeat_at.isoformat(),
                "age_seconds": (datetime.now() - pooled_conn.created_at).total_seconds(),
            })
        
        return details


class ConnectionPoolManager:
    """连接池管理器，管理多个数据库的连接池"""
    
    def __init__(self):
        """初始化连接池管理器"""
        self.pools: Dict[str, DatabaseConnectionPool] = {}
        self.lock = asyncio.Lock()
    
    async def register_pool(
        self,
        pool_name: str,
        connection_class: Type[DatabaseBase],
        config: Dict[str, Any],
        min_connections: int = 5,
        max_connections: int = 20,
        connection_timeout: int = 30,
        max_connection_lifetime: int = 3600,
    ) -> bool:
        """
        注册一个新的连接池
        
        Args:
            pool_name: 连接池名称
            connection_class: 连接类
            config: 数据库配置
            min_connections: 最小连接数
            max_connections: 最大连接数
            connection_timeout: 连接超时
            max_connection_lifetime: 连接最大生存时间
            
        Returns:
            bool: 注册是否成功
        """
        try:
            async with self.lock:
                if pool_name in self.pools:
                    logger.warning(f"连接池已存在: {pool_name}")
                    return False
                
                pool = DatabaseConnectionPool(
                    connection_class=connection_class,
                    config=config,
                    min_connections=min_connections,
                    max_connections=max_connections,
                    connection_timeout=connection_timeout,
                    max_connection_lifetime=max_connection_lifetime,
                )
                
                await pool.initialize()
                self.pools[pool_name] = pool
                
                logger.info(f"连接池已注册: {pool_name}")
                return True
                
        except Exception as e:
            logger.error(f"注册连接池失败: {str(e)}")
            return False
    
    async def acquire(self, pool_name: str, timeout: Optional[int] = None) -> Optional[DatabaseBase]:
        """
        从指定的连接池获取连接
        
        Args:
            pool_name: 连接池名称
            timeout: 超时时间
            
        Returns:
            Optional[DatabaseBase]: 数据库连接
        """
        if pool_name not in self.pools:
            logger.error(f"连接池不存在: {pool_name}")
            return None
        
        return await self.pools[pool_name].acquire(timeout)
    
    async def release(self, pool_name: str, connection: DatabaseBase) -> bool:
        """
        释放连接回到连接池
        
        Args:
            pool_name: 连接池名称
            connection: 连接对象
            
        Returns:
            bool: 操作是否成功
        """
        if pool_name not in self.pools:
            logger.error(f"连接池不存在: {pool_name}")
            return False
        
        return await self.pools[pool_name].release(connection)
    
    async def close_all(self):
        """关闭所有连接池"""
        for pool_name, pool in self.pools.items():
            await pool.close_all()
        
        self.pools.clear()
        logger.info("所有连接池已关闭")
    
    def get_pool_statistics(self, pool_name: str) -> Optional[Dict[str, Any]]:
        """
        获取指定连接池的统计信息
        
        Args:
            pool_name: 连接池名称
            
        Returns:
            Optional[Dict[str, Any]]: 统计信息
        """
        if pool_name not in self.pools:
            return None
        
        return self.pools[pool_name].get_statistics()
    
    def get_all_statistics(self) -> Dict[str, Dict[str, Any]]:
        """
        获取所有连接池的统计信息
        
        Returns:
            Dict[str, Dict[str, Any]]: 所有连接池的统计信息
        """
        stats = {}
        for pool_name, pool in self.pools.items():
            stats[pool_name] = pool.get_statistics()
        
        return stats


# 全局连接池管理器实例
pool_manager = ConnectionPoolManager()


async def initialize_pools(database_config: Dict[str, Any]) -> bool:
    """
    初始化所有数据库连接池

    Args:
        database_config: 数据库配置字典，对应 system_config.yaml 的 database 节点

    Returns:
        bool: 初始化是否成功
    """
    try:
        
        # 初始化 MySQL 连接池
        if 'mysql' in database_config:
            mysql_config = database_config['mysql']
            await pool_manager.register_pool(
                pool_name='mysql',
                connection_class=MySQLDatabase,
                config=mysql_config,
                min_connections=mysql_config.get('min_connections', 5),
                max_connections=mysql_config.get('max_connections', 20),
                connection_timeout=30,
                max_connection_lifetime=3600,
            )
            await pool_manager.pools['mysql'].verify_connections()
            logger.info("MySQL 连接池已初始化并验证")

        # 初始化 Redis 连接池
        if 'redis' in database_config:
            redis_config = database_config['redis']
            await pool_manager.register_pool(
                pool_name='redis',
                connection_class=RedisDatabase,
                config=redis_config,
                min_connections=redis_config.get('min_connections', 5),
                max_connections=redis_config.get('max_connections', 20),
                connection_timeout=30,
                max_connection_lifetime=3600,
            )
            await pool_manager.pools['redis'].verify_connections()
            logger.info("Redis 连接池已初始化并验证")

        # 初始化 Elasticsearch 连接池
        if 'elasticsearch' in database_config:
            es_config = database_config['elasticsearch']
            await pool_manager.register_pool(
                pool_name='elasticsearch',
                connection_class=ElasticsearchDatabase,
                config=es_config,
                min_connections=es_config.get('min_connections', 2),
                max_connections=es_config.get('max_connections', 10),
                connection_timeout=30,
                max_connection_lifetime=3600,
            )
            await pool_manager.pools['elasticsearch'].verify_connections()
            logger.info("Elasticsearch 连接池已初始化并验证")

        logger.info("所有数据库连接池初始化完成")
        return True
        
    except Exception as e:
        logger.error(f"初始化连接池失败: {str(e)}")
        return False


async def get_connection(db_type: str, db_name: Optional[str], timeout: Optional[int] = None) -> Optional[DatabaseBase]:
    """
    获取数据库连接
    
    Args:
        db_type: 数据库类型 ('mysql', 'redis', 'elasticsearch')
        db_name: 数据库名或索引名，None 表示保持当前连接数据库
        timeout: 超时时间 (秒)
        
    Returns:
        Optional[DatabaseBase]: 数据库连接实例
    """
    try:
        # 获取连接
        connection = await pool_manager.acquire(db_type, timeout)
        if not connection:
            logger.error(f"获取 {db_type} 连接失败")
            return None

        # 只有当 db_name 不为空时才进行切换
        if db_name and db_name.strip():
            if await connection.switch_db(db_name):
                logger.debug(f"成功获取 {db_type} 连接并切换到 {db_name}")
                return connection
            else:
                logger.error(f"切换 {db_type} 到 {db_name} 失败")
                await pool_manager.release(db_type, connection)
                return None

        logger.debug(f"成功获取 {db_type} 连接，未切换数据库")
        return connection
    except Exception as e:
        logger.error(f"获取连接失败: {str(e)}")
        return None


async def release_connection(db_type: str, connection: DatabaseBase) -> bool:
    """
    释放数据库连接
    
    Args:
        db_type: 数据库类型
        connection: 连接实例
        
    Returns:
        bool: 释放是否成功
    """
    return await pool_manager.release(db_type, connection)


async def close_all_pools():
    """关闭所有连接池"""
    await pool_manager.close_all()
