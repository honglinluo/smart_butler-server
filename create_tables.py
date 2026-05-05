"""数据库表创建脚本 - 创建用户表和LLM信息表"""

import os
from pathlib import Path

os.environ.setdefault("PROJECT_ROOT", str(Path(__file__).parent.resolve()))

import asyncio
import logging

import yaml
from sqlalchemy import text
from app.database.pool import get_connection, release_connection


def _read_log_cfg() -> dict:
    try:
        p = Path(os.environ["PROJECT_ROOT"]) / "config" / "system_config.yaml"
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


async def create_tables():
    """创建数据库表"""

    try:
        # 先连接到MySQL服务器（不指定数据库），创建数据库
        logger.info("创建数据库...")
        # 临时修改配置以连接到默认数据库
        import app.database.pool as pool_module
        original_config = pool_module.pool_manager.pools['mysql'].config.copy()
        temp_config = original_config.copy()
        # 移除数据库名，只连接到服务器
        temp_url = temp_config['url'].replace('/agent_db', '')
        temp_config['url'] = temp_url
        
        # 创建临时连接
        from app.database.mysql import MySQLDatabase
        temp_conn = MySQLDatabase(temp_config)
        if await temp_conn.connect():
            # 创建数据库（如果不存在）
            create_db_sql = "CREATE DATABASE IF NOT EXISTS agent_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
            with temp_conn.engine.connect() as connection:
                connection.execute(text(create_db_sql))
                connection.commit()
            logger.info("数据库创建成功")
            await temp_conn.disconnect()
        else:
            logger.error("连接到MySQL服务器失败")
            return

        # 现在连接到agent_db数据库
        logger.info("获取MySQL连接...")
        conn = await get_connection('mysql', 'agent_db')
        if not conn:
            logger.error("获取MySQL连接失败")
            return

        try:
            # 创建用户表
            logger.info("创建用户表...")
            create_user_table_sql = """
            CREATE TABLE IF NOT EXISTS user (
                user_id VARCHAR(50) PRIMARY KEY COMMENT '用户ID',
                username VARCHAR(100) UNIQUE NOT NULL COMMENT '用户名',
                password VARCHAR(255) NOT NULL COMMENT '密码哈希',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                last_login_at TIMESTAMP NULL COMMENT '最近登录时间',
                INDEX idx_user_id (user_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户表';
            """

            # 直接使用SQLAlchemy执行SQL
            with conn.engine.connect() as connection:
                connection.execute(text(create_user_table_sql))
                connection.commit()

            logger.info("用户表创建成功")

            # 创建LLM信息表
            logger.info("创建LLM信息表...")
            create_llm_table_sql = """
            CREATE TABLE IF NOT EXISTS llm_info (
                id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键ID',
                url VARCHAR(500) NOT NULL COMMENT 'LLM API URL',
                api_key VARCHAR(500) NOT NULL COMMENT 'API密钥',
                user_id VARCHAR(50) NOT NULL COMMENT '用户ID',
                model_name VARCHAR(100) NOT NULL COMMENT '模型名称',
                model_type ENUM('text', 'image', 'multimodal') NOT NULL COMMENT '模型类型',
                state TINYINT(1) DEFAULT 1 COMMENT '是否启用模型',
                is_deleted TINYINT(1) DEFAULT 0 COMMENT '是否删除',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                INDEX idx_user_id (user_id),
                FOREIGN KEY (user_id) REFERENCES user(user_id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='LLM信息表';
            """

            # 直接使用SQLAlchemy执行SQL
            with conn.engine.connect() as connection:
                connection.execute(text(create_llm_table_sql))
                connection.commit()

            logger.info("LLM信息表创建成功")

            # 创建用户画像表
            logger.info("创建用户画像表...")
            create_profile_table_sql = """
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id    VARCHAR(50)  NOT NULL COMMENT '用户ID',
                profile    JSON         NOT NULL COMMENT '用户画像 JSON，含 preferences/personal_info/work_content',
                updated_at TIMESTAMP    NULL DEFAULT CURRENT_TIMESTAMP
                           ON UPDATE CURRENT_TIMESTAMP COMMENT '最近更新时间',
                PRIMARY KEY (user_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
              COMMENT='用户画像表';
            """
            with conn.engine.connect() as connection:
                connection.execute(text(create_profile_table_sql))
                connection.commit()
            logger.info("用户画像表创建成功")

            logger.info("所有表创建完成！")

        finally:
            # 释放连接
            await release_connection('mysql', conn)

    except Exception as e:
        logger.error(f"创建表失败: {str(e)}")


if __name__ == "__main__":
    asyncio.run(create_tables())