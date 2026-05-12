"""
【模块说明】数据库基类（DatabaseBase）— 定义所有数据库类必须实现的统一接口

系统使用三种不同的数据库（MySQL、Redis、Elasticsearch），它们的操作方式各不相同。
这个基类定义了一套"公共接口"，让这三种数据库看起来都有相似的使用方式：
  - connect() / disconnect()    — 连接和断开
  - execute()                   — 执行操作
  - health_check()              — 检查是否正常运行

这样，连接池可以用统一的方式管理所有类型的数据库连接，不需要为每种数据库写不同的逻辑。

数据库基类 - 定义所有数据库类的通用接口
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class DatabaseBase(ABC):
    """数据库基类，所有数据库类都应继承此类"""
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化数据库基类
        
        Args:
            config: 数据库配置信息
        """
        self.config = config
        self.is_connected = False
        self.connection = None
        self.created_at = datetime.now()
    
    @abstractmethod
    async def connect(self) -> bool:
        """
        建立数据库连接
        
        Returns:
            bool: 连接是否成功
        """
        pass
    
    @abstractmethod
    async def disconnect(self) -> bool:
        """
        断开数据库连接
        
        Returns:
            bool: 断开是否成功
        """
        pass
    
    @abstractmethod
    async def health_check(self) -> bool:
        """
        健康检查，验证连接是否仍然有效
        
        Returns:
            bool: 连接是否健康
        """
        pass
    
    @abstractmethod
    async def switch_db(self, db_name: str) -> bool:
        """
        切换到指定的数据库/索引
        
        Args:
            db_name: 数据库名或索引名
            
        Returns:
            bool: 切换是否成功
        """
        pass
    
    @abstractmethod
    async def create(self, key: str, value: Any, **kwargs) -> bool:
        """
        创建/插入数据
        
        Args:
            key: 数据键
            value: 数据值
            **kwargs: 其他参数
            
        Returns:
            bool: 操作是否成功
        """
        pass
    
    @abstractmethod
    async def read(self, key: str, **kwargs) -> Optional[Any]:
        """
        读取数据
        
        Args:
            key: 数据键
            **kwargs: 其他参数
            
        Returns:
            Optional[Any]: 返回的数据，如果不存在则返回 None
        """
        pass
    
    @abstractmethod
    async def update(self, key: str, value: Any, **kwargs) -> bool:
        """
        更新数据
        
        Args:
            key: 数据键
            value: 新数据值
            **kwargs: 其他参数
            
        Returns:
            bool: 操作是否成功
        """
        pass
    
    @abstractmethod
    async def delete(self, key: str, **kwargs) -> bool:
        """
        删除数据
        
        Args:
            key: 数据键
            **kwargs: 其他参数
            
        Returns:
            bool: 操作是否成功
        """
        pass
    
    @abstractmethod
    async def batch_create(self, data: Dict[str, Any], **kwargs) -> bool:
        """
        批量创建/插入数据
        
        Args:
            data: 键值对字典
            **kwargs: 其他参数
            
        Returns:
            bool: 操作是否成功
        """
        pass
    
    @abstractmethod
    async def batch_read(self, keys: List[str], **kwargs) -> Dict[str, Any]:
        """
        批量读取数据
        
        Args:
            keys: 数据键列表
            **kwargs: 其他参数
            
        Returns:
            Dict[str, Any]: 键值对字典
        """
        pass
    
    @abstractmethod
    async def batch_delete(self, keys: List[str], **kwargs) -> bool:
        """
        批量删除数据
        
        Args:
            keys: 数据键列表
            **kwargs: 其他参数
            
        Returns:
            bool: 操作是否成功
        """
        pass
    
    def get_config(self) -> Dict[str, Any]:
        """
        获取数据库配置
        
        Returns:
            Dict[str, Any]: 配置信息
        """
        return self.config
    
    def get_connection_status(self) -> Dict[str, Any]:
        """
        获取连接状态
        
        Returns:
            Dict[str, Any]: 连接状态信息
        """
        return {
            "is_connected": self.is_connected,
            "database_type": self.__class__.__name__,
            "created_at": self.created_at.isoformat(),
        }
