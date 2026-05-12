"""
【模块说明】Elasticsearch 搜索引擎操作类

封装了对 Elasticsearch（ES）的操作。
ES 是一个强大的搜索引擎，支持全文搜索和向量（语义）搜索，
在本系统中用于存储和检索用户的全量历史对话。

【在本系统中的用途】
  - 存档全量历史对话（MySQL 只保留统计，ES 保留完整内容）
  - 全文检索：根据关键词从历史中找出相关对话
  - 向量语义搜索：根据含义相似度找出相关历史（需配合向量化服务）
  - 对话历史分页查询（/chat/history 接口）

每个用户有独立的 ES 索引（index），索引名格式：hermes_chat_{user_id}，
确保用户数据完全隔离。
"""


from typing import Any, Dict, List, Optional
from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk
import logging
from datetime import datetime

from .base import DatabaseBase

logger = logging.getLogger(__name__)


class ElasticsearchDatabase(DatabaseBase):
    """Elasticsearch 数据库类，实现了基类的所有方法"""
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化 Elasticsearch 数据库类
        
        Args:
            config: 数据库配置信息，应包含:
                - url: ES 连接字符串或列表 (格式: http://host:port)
                - index_prefix: 索引前缀
                - username: 用户名 (可选)
                - password: 密码 (可选)
                - timeout: 连接超时 (默认 10s)
                - max_retries: 最大重试次数 (默认 3)
                - vector_field: 向量字段名 (默认 embedding)
        """
        super().__init__(config)
        self.es_client = None
        
        # 提取配置参数
        self.url = config.get("url", "http://localhost:9200")
        self.index_prefix = config.get("index_prefix", "hermes")
        self.username = config.get("username")
        self.password = config.get("password")
        self.timeout = config.get("timeout", 10)
        self.max_retries = config.get("max_retries", 3)
        self.vector_field = config.get("vector_field", "embedding")
        self._es_major_version: int = 7  # 连接后由 connect() 检测并更新
    
    async def connect(self) -> bool:
        """
        建立 Elasticsearch 连接
        
        Returns:
            bool: 连接是否成功
        """
        try:
            # 处理连接参数
            connect_kwargs = {}
            
            # 如果提供了用户名和密码
            if self.username and self.password:
                connect_kwargs["basic_auth"] = (self.username, self.password)
            
            # 处理 URL（可能是列表或字符串）
            if isinstance(self.url, str):
                hosts = [self.url]
            else:
                hosts = self.url
            
            self.es_client = Elasticsearch(hosts, **connect_kwargs)
            
            # 测试连接并检测版本
            info = self.es_client.info()
            self.is_connected = True

            version_str = info.get("version", {}).get("number", "7.0.0")
            try:
                self._es_major_version = int(version_str.split(".")[0])
            except Exception:
                self._es_major_version = 7

            logger.info(f"Elasticsearch 数据库连接成功: {hosts}")
            logger.info(f"集群信息: {version_str}（主版本 {self._es_major_version}）")
            return True
            
        except Exception as e:
            logger.error(f"Elasticsearch 数据库连接失败: {str(e)}")
            self.is_connected = False
            return False
    
    async def disconnect(self) -> bool:
        """
        断开 Elasticsearch 连接
        
        Returns:
            bool: 操作是否成功
        """
        try:
            if self.es_client:
                self.es_client.close()
            
            self.is_connected = False
            logger.info("Elasticsearch 数据库连接已断开")
            return True
            
        except Exception as e:
            logger.error(f"断开 Elasticsearch 连接时出错: {str(e)}")
            return False
    
    async def health_check(self) -> bool:
        """
        健康检查，验证连接是否仍然有效
        
        Returns:
            bool: 连接是否健康
        """
        try:
            if not self.is_connected or not self.es_client:
                return False
            
            health = self.es_client.cluster.health()
            return health.get("status") in ("green", "yellow")
            
        except Exception as e:
            logger.warning(f"Elasticsearch 健康检查失败: {str(e)}")
            self.is_connected = False
            return False
    
    async def switch_db(self, db_name: str) -> bool:
        """
        切换到指定的索引前缀
        
        Args:
            db_name: 索引前缀名
            
        Returns:
            bool: 切换是否成功
        """
        try:
            self.index_prefix = db_name
            logger.info(f"切换到 Elasticsearch 索引前缀: {db_name}")
            return True
            
        except Exception as e:
            logger.error(f"切换 Elasticsearch 索引前缀失败: {str(e)}")
            return False
    
    def _get_index_name(self, index: str) -> str:
        """
        获取完整的索引名称
        
        Args:
            index: 索引名称
            
        Returns:
            str: 带前缀的索引名称
        """
        return f"{self.index_prefix}_{index}"
    
    async def create_index(self, index: str, mappings: Optional[Dict[str, Any]] = None, **kwargs) -> bool:
        """
        创建索引
        
        Args:
            index: 索引名称
            mappings: 索引映射配置
            **kwargs: 其他参数 (settings 等)
            
        Returns:
            bool: 操作是否成功
        """
        try:
            if not self.is_connected:
                return False
            
            full_index_name = self._get_index_name(index)
            
            # 检查索引是否已存在
            if self.es_client.indices.exists(index=full_index_name):
                logger.warning(f"索引已存在: {full_index_name}")
                return True
            
            # 构建索引配置
            body = {
                "settings": kwargs.get("settings", {
                    "number_of_shards": 1,
                    "number_of_replicas": 0,
                }),
            }
            
            if mappings:
                body["mappings"] = mappings
            
            self.es_client.indices.create(index=full_index_name, body=body)
            logger.info(f"成功创建索引: {full_index_name}")
            return True
            
        except Exception as e:
            logger.error(f"创建索引失败: {str(e)}")
            return False
    
    async def delete_index(self, index: str, **kwargs) -> bool:
        """
        删除索引
        
        Args:
            index: 索引名称
            **kwargs: 其他参数
            
        Returns:
            bool: 操作是否成功
        """
        try:
            if not self.is_connected:
                return False
            
            full_index_name = self._get_index_name(index)
            
            if not self.es_client.indices.exists(index=full_index_name):
                logger.warning(f"索引不存在: {full_index_name}")
                return True
            
            self.es_client.indices.delete(index=full_index_name)
            logger.info(f"成功删除索引: {full_index_name}")
            return True
            
        except Exception as e:
            logger.error(f"删除索引失败: {str(e)}")
            return False
    
    async def create(self, index: str, doc_id: str, document: Dict[str, Any], **kwargs) -> bool:
        """
        创建/插入文档
        
        Args:
            index: 索引名称
            doc_id: 文档 ID
            document: 文档内容
            **kwargs: 其他参数
            
        Returns:
            bool: 操作是否成功
        """
        try:
            if not self.is_connected:
                return False
            
            full_index_name = self._get_index_name(index)
            
            # 添加时间戳
            if "timestamp" not in document:
                document["timestamp"] = datetime.now().isoformat()
            
            result = self.es_client.index(
                index=full_index_name,
                id=doc_id,
                document=document,
            )
            
            logger.debug(f"成功创建文档: {full_index_name}/{doc_id}")
            return result["result"] in ("created", "updated")
            
        except Exception as e:
            logger.error(f"创建文档失败: {str(e)}")
            return False
    
    async def read(self, index: str, doc_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        """
        读取单个文档
        
        Args:
            index: 索引名称
            doc_id: 文档 ID
            **kwargs: 其他参数
            
        Returns:
            Optional[Dict[str, Any]]: 文档内容
        """
        try:
            if not self.is_connected:
                return None
            
            full_index_name = self._get_index_name(index)
            
            result = self.es_client.get(
                index=full_index_name,
                id=doc_id,
            )
            
            return result["_source"]
            
        except Exception as e:
            if "404" not in str(e):
                logger.error(f"读取文档失败: {str(e)}")
            return None
    
    async def update(self, index: str, doc_id: str, document: Dict[str, Any], **kwargs) -> bool:
        """
        更新文档
        
        Args:
            index: 索引名称
            doc_id: 文档 ID
            document: 要更新的字段
            **kwargs: 其他参数
            
        Returns:
            bool: 操作是否成功
        """
        try:
            if not self.is_connected:
                return False
            
            full_index_name = self._get_index_name(index)
            
            result = self.es_client.update(
                index=full_index_name,
                id=doc_id,
                doc=document,
            )
            
            logger.debug(f"成功更新文档: {full_index_name}/{doc_id}")
            return result["result"] in ("updated", "noop")
            
        except Exception as e:
            logger.error(f"更新文档失败: {str(e)}")
            return False
    
    async def delete(self, index: str, doc_id: str, **kwargs) -> bool:
        """
        删除文档
        
        Args:
            index: 索引名称
            doc_id: 文档 ID
            **kwargs: 其他参数
            
        Returns:
            bool: 操作是否成功
        """
        try:
            if not self.is_connected:
                return False
            
            full_index_name = self._get_index_name(index)
            
            result = self.es_client.delete(
                index=full_index_name,
                id=doc_id,
            )
            
            logger.debug(f"成功删除文档: {full_index_name}/{doc_id}")
            return result["result"] in ("deleted", "not_found")
            
        except Exception as e:
            logger.error(f"删除文档失败: {str(e)}")
            return False
    
    async def batch_create(self, index: str, documents: List[Dict[str, Any]], **kwargs) -> bool:
        """
        批量创建/插入文档
        
        Args:
            index: 索引名称
            documents: 文档列表，每个文档应包含 '_id' 字段
            **kwargs: 其他参数
            
        Returns:
            bool: 操作是否成功
        """
        try:
            if not self.is_connected or not documents:
                return False
            
            full_index_name = self._get_index_name(index)
            
            actions = []
            for doc in documents:
                doc_id = doc.pop("_id", None)
                
                # 添加时间戳
                if "timestamp" not in doc:
                    doc["timestamp"] = datetime.now().isoformat()
                
                action = {
                    "_index": full_index_name,
                    "_id": doc_id,
                    "_source": doc,
                }
                actions.append(action)
            
            success_count, errors = bulk(self.es_client, actions)
            
            if errors:
                logger.warning(f"批量插入出现 {len(errors)} 个错误")
            
            logger.debug(f"成功批量插入 {success_count} 个文档到 {full_index_name}")
            return len(errors) == 0
            
        except Exception as e:
            logger.error(f"批量插入文档失败: {str(e)}")
            return False
    
    async def batch_read(self, index: str, doc_ids: List[str], **kwargs) -> Dict[str, Dict[str, Any]]:
        """
        批量读取文档
        
        Args:
            index: 索引名称
            doc_ids: 文档 ID 列表
            **kwargs: 其他参数
            
        Returns:
            Dict[str, Dict[str, Any]]: 文档字典 {doc_id: document}
        """
        try:
            if not self.is_connected or not doc_ids:
                return {}
            
            full_index_name = self._get_index_name(index)
            
            result = self.es_client.mget(
                index=full_index_name,
                ids=doc_ids,
            )
            
            documents = {}
            for doc in result["docs"]:
                if doc.get("found"):
                    documents[doc["_id"]] = doc["_source"]
            
            return documents
            
        except Exception as e:
            logger.error(f"批量读取文档失败: {str(e)}")
            return {}
    
    async def batch_delete(self, index: str, doc_ids: List[str], **kwargs) -> bool:
        """
        批量删除文档
        
        Args:
            index: 索引名称
            doc_ids: 文档 ID 列表
            **kwargs: 其他参数
            
        Returns:
            bool: 操作是否成功
        """
        try:
            if not self.is_connected or not doc_ids:
                return False
            
            full_index_name = self._get_index_name(index)
            
            actions = []
            for doc_id in doc_ids:
                action = {
                    "_op_type": "delete",
                    "_index": full_index_name,
                    "_id": doc_id,
                }
                actions.append(action)
            
            success_count, errors = bulk(self.es_client, actions)
            
            if errors:
                logger.warning(f"批量删除出现 {len(errors)} 个错误")
            
            logger.debug(f"成功批量删除 {success_count} 个文档从 {full_index_name}")
            return len(errors) == 0
            
        except Exception as e:
            logger.error(f"批量删除文档失败: {str(e)}")
            return False
    
    async def search(self, index: str, query: Dict[str, Any], size: int = 10, from_: int = 0, **kwargs) -> Dict[str, Any]:
        """
        搜索文档

        Args:
            index: 索引名称
            query: 查询体
            size: 返回的文档数量
            from_: 分页偏移
            **kwargs: 额外参数，支持 sort（列表，如 [{"timestamp": {"order": "desc"}}]）

        Returns:
            Dict[str, Any]: 搜索结果
        """
        try:
            if not self.is_connected:
                return {"hits": {"hits": [], "total": {"value": 0}}}

            full_index_name = self._get_index_name(index)
            search_kwargs: Dict[str, Any] = {
                "index": full_index_name,
                "query": query,
                "size": size,
                "from_": from_,
            }
            if "sort" in kwargs:
                search_kwargs["sort"] = kwargs["sort"]

            result = self.es_client.search(**search_kwargs)
            return result

        except Exception as e:
            logger.error(f"搜索文档失败: {str(e)}")
            return {"hits": {"hits": [], "total": {"value": 0}}}
    
    async def vector_search(self, index: str, vector: List[float], top_k: int = 10, **kwargs) -> List[Dict[str, Any]]:
        """向量相似度搜索，自动适配 ES 7.x（script_score）和 ES 8.x（knn）语法。

        Args:
            index:        索引名称（不含前缀）
            vector:       查询向量
            top_k:        返回最相关的 K 个结果
            vector_field: 可选，覆盖实例默认的 vector_field（kwargs 传入）
            filter:       可选，ES filter 过滤条件（kwargs 传入）
        """
        try:
            if not self.is_connected:
                return []

            full_index_name = self._get_index_name(index)
            field = kwargs.get("vector_field", self.vector_field)

            if self._es_major_version >= 8:
                # ES 8.x：顶层 knn 参数
                knn_body: Dict[str, Any] = {
                    "field":          field,
                    "query_vector":   vector,
                    "k":              top_k,
                    "num_candidates": top_k * 10,
                }
                if "filter" in kwargs:
                    knn_body["filter"] = kwargs["filter"]
                result = self.es_client.search(
                    index=full_index_name,
                    knn=knn_body,
                    size=top_k,
                    source={"excludes": [field]},
                )
            else:
                # ES 7.x：script_score + cosineSimilarity
                filter_clause = kwargs.get("filter", {"match_all": {}})
                result = self.es_client.search(
                    index=full_index_name,
                    body={
                        "query": {
                            "script_score": {
                                "query": filter_clause,
                                "script": {
                                    "source": (
                                        f"cosineSimilarity(params.query_vector, '{field}') + 1.0"
                                    ),
                                    "params": {"query_vector": vector},
                                },
                            }
                        },
                        "size": top_k,
                        "_source": {"excludes": [field]},
                    },
                )

            documents = []
            for hit in result["hits"]["hits"]:
                doc = dict(hit["_source"])
                doc["_score"] = hit["_score"]
                doc["_id"]    = hit["_id"]
                documents.append(doc)

            return documents

        except Exception as e:
            logger.error(f"向量搜索失败: {str(e)}")
            return []
    
    async def count_documents(self, index: str, query: Optional[Dict[str, Any]] = None, **kwargs) -> int:
        """
        计数文档
        
        Args:
            index: 索引名称
            query: 查询条件 (可选)
            **kwargs: 其他参数
            
        Returns:
            int: 文档数量
        """
        try:
            if not self.is_connected:
                return 0
            
            full_index_name = self._get_index_name(index)
            
            if query:
                result = self.es_client.count(
                    index=full_index_name,
                    query=query,
                )
            else:
                result = self.es_client.count(index=full_index_name)
            
            return result["count"]
            
        except Exception as e:
            logger.error(f"计数文档失败: {str(e)}")
            return 0
    
    async def delete_by_query(self, index: str, query: Dict[str, Any], **kwargs) -> int:
        """
        按查询条件删除文档
        
        Args:
            index: 索引名称
            query: 查询条件
            **kwargs: 其他参数
            
        Returns:
            int: 删除的文档数量
        """
        try:
            if not self.is_connected:
                return 0
            
            full_index_name = self._get_index_name(index)
            
            result = self.es_client.delete_by_query(
                index=full_index_name,
                query=query,
            )
            
            deleted_count = result["deleted"]
            logger.debug(f"成功删除 {deleted_count} 个文档从 {full_index_name}")
            return deleted_count
            
        except Exception as e:
            logger.error(f"按查询删除文档失败: {str(e)}")
            return 0
