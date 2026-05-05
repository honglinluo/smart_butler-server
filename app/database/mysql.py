"""MySQL 数据库类 - 实现 MySQL 的增删改查操作"""

from typing import Any, Dict, List, Optional, Union
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import QueuePool
import logging

from .base import DatabaseBase

logger = logging.getLogger(__name__)


class MySQLDatabase(DatabaseBase):
    """MySQL 数据库类，实现了基类的所有方法"""
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化 MySQL 数据库类
        
        Args:
            config: 数据库配置信息，应包含:
                - url: MySQL 连接字符串
                - pool_size: 连接池大小 (默认 10)
                - max_overflow: 最大溢出连接数 (默认 20)
                - pool_recycle: 连接回收时间 (默认 3600s)
                - echo: 是否打印 SQL (默认 False)
        """
        super().__init__(config)
        self.engine = None
        self.SessionLocal = None
        self.session = None
        
        # 提取配置参数
        self.url = config.get("url", "mysql+pymysql://user:pass@localhost/db")
        self.pool_size = config.get("pool_size", 10)
        self.max_overflow = config.get("max_overflow", 20)
        self.pool_recycle = config.get("pool_recycle", 3600)
        self.echo = config.get("echo", False)
    
    async def connect(self) -> bool:
        """
        建立 MySQL 连接
        
        Returns:
            bool: 连接是否成功
        """
        try:
            # 创建引擎，使用连接池
            self.engine = create_engine(
                self.url,
                poolclass=QueuePool,
                pool_size=self.pool_size,
                max_overflow=self.max_overflow,
                pool_recycle=self.pool_recycle,
                echo=self.echo,
                pool_pre_ping=True,  # 启用连接健康检查
            )
            
            # 创建会话工厂
            self.SessionLocal = sessionmaker(
                autocommit=False,
                autoflush=False,
                bind=self.engine,
            )
            
            # 测试连接
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            
            self.is_connected = True
            logger.info(f"MySQL 数据库连接成功: {self.url}")
            return True
            
        except Exception as e:
            logger.error(f"MySQL 数据库连接失败: {str(e)}")
            self.is_connected = False
            return False
    
    async def disconnect(self) -> bool:
        """
        断开 MySQL 连接
        
        Returns:
            bool: 操作是否成功
        """
        try:
            if self.engine:
                self.engine.dispose()
            
            self.is_connected = False
            logger.info("MySQL 数据库连接已断开")
            return True
            
        except Exception as e:
            logger.error(f"断开 MySQL 连接时出错: {str(e)}")
            return False
    
    async def health_check(self) -> bool:
        """
        健康检查，验证连接是否仍然有效
        
        Returns:
            bool: 连接是否健康
        """
        try:
            if not self.is_connected or not self.engine:
                return False
            
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            
            return True
            
        except Exception as e:
            logger.warning(f"MySQL 健康检查失败: {str(e)}")
            self.is_connected = False
            return False
    
    async def switch_db(self, db_name: str) -> bool:
        """
        切换到指定的数据库
        
        Args:
            db_name: 数据库名
            
        Returns:
            bool: 切换是否成功
        """
        try:
            if not self.is_connected or not self.engine:
                return False
            
            with self.engine.connect() as conn:
                conn.execute(text(f"USE {db_name}"))
            
            logger.info(f"切换到 MySQL 数据库: {db_name}")
            return True
            
        except Exception as e:
            logger.error(f"切换 MySQL 数据库失败: {str(e)}")
            return False
    
    async def create(self, table: str, data: Dict[str, Any], **kwargs) -> bool:
        """
        插入单条数据
        
        Args:
            table: 表名
            data: 数据字典 {列名: 值}
            **kwargs: 其他参数
            
        Returns:
            bool: 操作是否成功
        """
        session = None
        try:
            session = self.SessionLocal()
            
            # 构建 INSERT SQL
            columns = ", ".join(data.keys())
            values = ", ".join([f":{k}" for k in data.keys()])
            sql = f"INSERT INTO {table} ({columns}) VALUES ({values})"
            
            session.execute(text(sql), data)
            session.commit()
            
            logger.debug(f"成功插入数据到 {table} 表")
            return True
            
        except Exception as e:
            if session:
                session.rollback()
            logger.error(f"插入数据失败: {str(e)}")
            return False
            
        finally:
            if session:
                session.close()
    
    async def read(self, table: str, where: Optional[Dict[str, Any]] = None, 
                   columns: Optional[List[str]] = None, **kwargs) -> Optional[pd.DataFrame]:
        """
        读取数据
        
        Args:
            table: 表名
            where: WHERE 条件 {列名: 值} (AND 关系)
            columns: 要查询的列名列表，默认为 *
            **kwargs: 其他参数 (limit, offset 等)
            
        Returns:
            Optional[pd.DataFrame]: 返回的数据DataFrame，如果无数据返回None
        """
        session = None
        try:
            session = self.SessionLocal()
            
            # 构建 SELECT SQL
            cols = ", ".join(columns) if columns else "*"
            sql = f"SELECT {cols} FROM {table}"
            params = {}
            
            if where:
                conditions = [f"{k} = :{k}" for k in where.keys()]
                sql += " WHERE " + " AND ".join(conditions)
                params.update(where)
            
            # 处理 LIMIT 和 OFFSET
            limit = kwargs.get("limit")
            offset = kwargs.get("offset", 0)
            if limit:
                sql += f" LIMIT {limit} OFFSET {offset}"
            
            result = session.execute(text(sql), params)
            
            # 转换为 DataFrame
            rows = []
            for row in result:
                rows.append(dict(row._mapping))
            
            if not rows:
                return None
            
            df = pd.DataFrame(rows)
            return df
            
        except Exception as e:
            logger.error(f"读取数据失败: {str(e)}")
            return None
            
        finally:
            if session:
                session.close()
    
    async def update(self, table: str, data: Dict[str, Any], 
                     where: Dict[str, Any], **kwargs) -> bool:
        """
        更新数据
        
        Args:
            table: 表名
            data: 要更新的数据 {列名: 新值}
            where: WHERE 条件 {列名: 值}
            **kwargs: 其他参数
            
        Returns:
            bool: 操作是否成功
        """
        session = None
        try:
            session = self.SessionLocal()
            
            # 构建 UPDATE SQL
            set_clauses = [f"{k} = :{k}" for k in data.keys()]
            where_clauses = [f"{k} = :where_{k}" for k in where.keys()]
            
            sql = f"UPDATE {table} SET {', '.join(set_clauses)}"
            sql += " WHERE " + " AND ".join(where_clauses)
            
            params = data.copy()
            for k, v in where.items():
                params[f"where_{k}"] = v
            
            session.execute(text(sql), params)
            session.commit()
            
            logger.debug(f"成功更新 {table} 表")
            return True
            
        except Exception as e:
            if session:
                session.rollback()
            logger.error(f"更新数据失败: {str(e)}")
            return False
            
        finally:
            if session:
                session.close()
    
    async def delete(self, table: str, where: Dict[str, Any], **kwargs) -> bool:
        """
        删除数据
        
        Args:
            table: 表名
            where: WHERE 条件 {列名: 值}
            **kwargs: 其他参数
            
        Returns:
            bool: 操作是否成功
        """
        session = None
        try:
            session = self.SessionLocal()
            
            # 构建 DELETE SQL
            where_clauses = [f"{k} = :where_{k}" for k in where.keys()]
            sql = f"DELETE FROM {table} WHERE " + " AND ".join(where_clauses)
            
            params = {}
            for k, v in where.items():
                params[f"where_{k}"] = v
            
            session.execute(text(sql), params)
            session.commit()
            
            logger.debug(f"成功从 {table} 表删除数据")
            return True
            
        except Exception as e:
            if session:
                session.rollback()
            logger.error(f"删除数据失败: {str(e)}")
            return False
            
        finally:
            if session:
                session.close()
    
    async def batch_create(self, table: str, data_list: List[Dict[str, Any]], **kwargs) -> bool:
        """
        批量插入数据
        
        Args:
            table: 表名
            data_list: 数据列表 [{'列': 值}, ...]
            **kwargs: 其他参数
            
        Returns:
            bool: 操作是否成功
        """
        if not data_list:
            return False
        
        session = None
        try:
            session = self.SessionLocal()
            
            # 使用 executemany 进行批量插入
            columns = list(data_list[0].keys())
            columns_str = ", ".join(columns)
            values_str = ", ".join([f":{k}" for k in columns])
            sql = f"INSERT INTO {table} ({columns_str}) VALUES ({values_str})"
            
            for data in data_list:
                session.execute(text(sql), data)
            
            session.commit()
            
            logger.debug(f"成功批量插入 {len(data_list)} 条数据到 {table} 表")
            return True
            
        except Exception as e:
            if session:
                session.rollback()
            logger.error(f"批量插入数据失败: {str(e)}")
            return False
            
        finally:
            if session:
                session.close()
    
    async def batch_read(self, table: str, keys: List[str], 
                        key_column: str = "id", **kwargs) -> Union[pd.DataFrame, Dict[str, Any]]:
        """
        批量读取数据
        
        Args:
            table: 表名
            keys: 主键值列表
            key_column: 作为查询条件的列名 (默认 'id')
            **kwargs: 其他参数，return_dict=True返回字典形式，否则返回DataFrame
            
        Returns:
            Union[pd.DataFrame, Dict[str, Any]]: 返回DataFrame或字典形式
        """
        session = None
        try:
            session = self.SessionLocal()
            
            # 构建 IN SQL
            placeholders = ", ".join([f":{i}" for i in range(len(keys))])
            sql = f"SELECT * FROM {table} WHERE {key_column} IN ({placeholders})"
            
            params = {str(i): key for i, key in enumerate(keys)}
            result = session.execute(text(sql), params)
            
            # 转换为 DataFrame
            rows = []
            for row in result:
                rows.append(dict(row._mapping))
            
            if not rows:
                return {} if kwargs.get('return_dict') else None
            
            df = pd.DataFrame(rows)
            
            # 如果需要字典格式
            if kwargs.get('return_dict'):
                data_dict = {}
                for _, row in df.iterrows():
                    data_dict[row[key_column]] = row.to_dict()
                return data_dict
            
            return df
            
        except Exception as e:
            logger.error(f"批量读取数据失败: {str(e)}")
            return {}
            
        finally:
            if session:
                session.close()
    
    async def batch_delete(self, table: str, keys: List[str], 
                          key_column: str = "id", **kwargs) -> bool:
        """
        批量删除数据
        
        Args:
            table: 表名
            keys: 主键值列表
            key_column: 作为删除条件的列名 (默认 'id')
            **kwargs: 其他参数
            
        Returns:
            bool: 操作是否成功
        """
        if not keys:
            return False
        
        session = None
        try:
            session = self.SessionLocal()
            
            # 构建 IN SQL
            placeholders = ", ".join([f":{i}" for i in range(len(keys))])
            sql = f"DELETE FROM {table} WHERE {key_column} IN ({placeholders})"
            
            params = {str(i): key for i, key in enumerate(keys)}
            session.execute(text(sql), params)
            session.commit()
            
            logger.debug(f"成功批量删除 {len(keys)} 条数据从 {table} 表")
            return True
            
        except Exception as e:
            if session:
                session.rollback()
            logger.error(f"批量删除数据失败: {str(e)}")
            return False
            
        finally:
            if session:
                session.close()
    
    async def execute_raw(self, sql: str, params: Optional[Dict[str, Any]] = None) -> Optional[pd.DataFrame]:
        """
        执行原始 SQL 查询
        
        Args:
            sql: SQL 语句
            params: SQL 参数
            
        Returns:
            Optional[pd.DataFrame]: 查询结果DataFrame，修改操作返回None
        """
        session = None
        try:
            session = self.SessionLocal()
            result = session.execute(text(sql), params or {})

            if not result.returns_rows:
                session.commit()
                logger.debug(f"成功执行非查询操作: {sql[:50]}...")
                return None

            # SELECT 查询 - 转换为 DataFrame
            rows = [dict(row._mapping) for row in result]
            if not rows:
                return None
            return pd.DataFrame(rows)
            
        except Exception as e:
            if session:
                session.rollback()
            logger.error(f"执行 SQL 查询失败: {type(e).__name__}: {e}", exc_info=True)
            return None
            
        finally:
            if session:
                session.close()
