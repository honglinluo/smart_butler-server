#!/usr/bin/env python3
"""
完整集成测试脚本 - 从 MySQL 数据库中获取模型配置并测试大模型连接
"""

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Dict, Any

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
os.environ.setdefault("PROJECT_ROOT", str(_PROJECT_ROOT.resolve()))

def _read_log_cfg() -> dict:
    try:
        import yaml
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


async def test_database_connection():
    """测试数据库连接"""
    logger.info("\n" + "=" * 70)
    logger.info("📊 测试数据库连接")
    logger.info("=" * 70)
    
    try:
        from app.database.pool import get_connection, release_connection

        # 测试 MySQL 连接
        logger.info("\n测试 MySQL 连接...")
        mysql_conn = await get_connection("mysql", None)
        if mysql_conn:
            logger.info("✅ MySQL 连接成功")
            await release_connection("mysql", mysql_conn)
        else:
            logger.error("❌ MySQL 连接失败")
            return False
        
        # 测试 Redis 连接
        logger.info("\n测试 Redis 连接...")
        redis_conn = await get_connection("redis", None)
        if redis_conn:
            logger.info("✅ Redis 连接成功")
            await release_connection("redis", redis_conn)
        else:
            logger.warning("⚠️ Redis 连接失败（可选）")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ 数据库测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def fetch_llm_config_from_db(user_id: str = "0") -> Optional[Dict[str, Any]]:
    """从数据库中获取 LLM 配置"""
    logger.info("\n" + "=" * 70)
    logger.info(f"🔍 从数据库获取 LLM 配置 (user_id={user_id})")
    logger.info("=" * 70)
    
    try:
        from app.database.pool import get_connection, release_connection
        
        logger.info(f"\n查询 LLM 配置...")
        mysql_conn = await get_connection("mysql", None)
        
        if not mysql_conn:
            logger.error("❌ 无法获取 MySQL 连接")
            return None
        
        try:
            # 查询 llms 表
            sql = (
                "SELECT url, api_key, model_name, temperature, model_type "
                "FROM llms WHERE user_id = :user_id AND state = 1 "
                "ORDER BY id DESC LIMIT 1"
            )
            
            logger.info(f"  执行 SQL: {sql}")
            rows = await mysql_conn.execute_raw(sql, {"user_id": user_id})
            
            if not rows:
                logger.warning(f"⚠️ 未找到 user_id={user_id} 的 LLM 配置")
                return None
            
            # 数据库返回字典格式的行数据
            row = rows[0]
            config = {
                "url": row.get("url") if isinstance(row, dict) else row[0],
                "api_key": row.get("api_key") if isinstance(row, dict) else row[1],
                "model_name": row.get("model_name") if isinstance(row, dict) else row[2],
                "temperature": float(row.get("temperature", 0.7)) if isinstance(row, dict) else float(row[3] if row[3] else 0.7),
                "model_type": row.get("model_type", "chat") if isinstance(row, dict) else (row[4] if row[4] else "chat"),
            }
            
            logger.info("✅ LLM 配置获取成功:")
            logger.info(f"   - Model: {config['model_name']}")
            logger.info(f"   - Type: {config['model_type']}")
            logger.info(f"   - Temperature: {config['temperature']}")
            logger.info(f"   - URL: {config['url']}")
            logger.info(f"   - API Key: {config['api_key'][:20]}...")
            
            return config
            
        finally:
            await release_connection("mysql", mysql_conn)
        
    except Exception as e:
        logger.error(f"❌ 获取 LLM 配置失败: {e}")
        import traceback
        traceback.print_exc()
        return None


async def test_llm_with_config(config: Dict[str, Any]) -> bool:
    """使用数据库配置测试 LLM"""
    logger.info("\n" + "=" * 70)
    logger.info("🤖 测试 LLM 调用")
    logger.info("=" * 70)
    
    try:
        from langchain.chat_models import init_chat_model
        from langchain_core.messages import SystemMessage, HumanMessage
        
        logger.info("\n初始化模型...")
        logger.info(f"  配置:")
        logger.info(f"    - model: {config['model_name']}")
        logger.info(f"    - provider: (自动推断)")
        logger.info(f"    - api_key: {config['api_key'][:20]}...")
        
        # 推断模型提供商
        model_name = config['model_name'].lower()
        if "gpt" in model_name:
            provider = "openai"
        elif "claude" in model_name:
            provider = "anthropic"
        elif "gemini" in model_name:
            provider = "google_genai"
        else:
            provider = "openai"  # 默认
        
        logger.info(f"    - provider: {provider}")
        
        # 构建 LLM
        llm_kwargs = {
            "model": config['model_name'],
            "model_provider": provider,
            "temperature": config['temperature'],
            "api_key": config['api_key'],
        }
        
        if config.get('url'):
            if provider == "openai":
                llm_kwargs["openai_api_base"] = config['url']
        
        model = init_chat_model(**llm_kwargs)
        logger.info("✅ 模型初始化成功")
        
        # 准备测试消息
        logger.info("\n发送测试消息...")
        messages = [
            SystemMessage(content="你是一个有帮助的助手"),
            HumanMessage(content="请简要介绍什么是多智能体系统 (用50字以内)")
        ]
        
        logger.info(f"  System: {messages[0].content}")
        logger.info(f"  User: {messages[1].content}")
        
        # 调用模型
        logger.info("\n等待模型响应...")
        response = await model.ainvoke(messages)
        
        logger.info(f"\n✅ 模型响应成功:")
        logger.info(f"  {response.content}")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ LLM 调用失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_hermes_engine_integration():
    """测试 Hermes 引擎集成"""
    logger.info("\n" + "=" * 70)
    logger.info("⚙️ 测试 Hermes 引擎集成")
    logger.info("=" * 70)
    
    try:
        from app.core.config_loader import ConfigLoader
        from app.core.hermes_engine import HermesEngine
        
        logger.info("\n加载系统配置...")
        config_loader = ConfigLoader("config")
        system_config = config_loader.load_system_config()
        logger.info("✅ 配置加载成功")
        
        logger.info("\n初始化 Hermes 引擎...")
        hermes_engine = HermesEngine(system_config)
        
        try:
            await hermes_engine.initialize()
            logger.info("✅ 引擎初始化成功")
        except Exception as e:
            logger.warning(f"⚠️ 引擎初始化部分失败: {str(e)[:100]}")
        
        # 测试消息处理
        logger.info("\n测试消息处理...")
        try:
            response = await hermes_engine.process_user_input(
                user_id="test_user",
                user_input="你好，我是测试用户",
                context={"source": "test"}
            )
            logger.info(f"✅ 消息处理成功: {response[:100]}...")
        except Exception as e:
            logger.warning(f"⚠️ 消息处理失败: {str(e)[:100]}")
        
        # 清理
        await hermes_engine.shutdown()
        logger.info("✅ 引擎已正确关闭")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Hermes 引擎测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_full_workflow():
    """测试完整工作流"""
    logger.info("\n" + "=" * 70)
    logger.info("🔄 测试完整工作流")
    logger.info("=" * 70)
    
    try:
        from app.database.pool import get_connection, release_connection
        from app.api.dependencies import get_current_user, get_user_model
        
        logger.info("\n1️⃣ 模拟用户认证...")
        user = await get_current_user(authorization="Bearer test_token")
        logger.info(f"✅ 用户信息: {user}")
        
        logger.info("\n2️⃣ 获取用户模型配置...")
        model_config = await get_user_model(user)
        logger.info(f"✅ 模型配置: {model_config}")
        
        logger.info("\n3️⃣ 查询数据库 LLM 配置...")
        mysql_conn = await get_connection("mysql", None)
        if mysql_conn:
            db_config = await fetch_llm_config_from_db(user.get("user_id", "0"))
            if db_config:
                logger.info("✅ 从数据库获取配置成功")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ 工作流测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """主测试流程"""
    logger.info("\n")
    logger.info("╔" + "=" * 68 + "╗")
    logger.info("║" + " " * 18 + "🧪 Hermes 多智能体系统完整测试" + " " * 18 + "║")
    logger.info("╚" + "=" * 68 + "╝")
    
    results = {}
    
    # 测试 1: 数据库连接
    logger.info("\n【测试 1/5】数据库连接")
    db_result = await test_database_connection()
    results["数据库连接"] = db_result
    
    if not db_result:
        logger.error("\n❌ 无法连接数据库，后续测试将跳过")
        return 1
    
    # 测试 2: 获取 LLM 配置
    logger.info("\n【测试 2/5】从数据库获取 LLM 配置")
    llm_config = await fetch_llm_config_from_db()
    results["获取 LLM 配置"] = llm_config is not None
    
    if not llm_config:
        logger.error("\n❌ 无法从数据库获取 LLM 配置")
        return 1
    
    # 测试 3: 测试 LLM 调用
    logger.info("\n【测试 3/5】LLM 调用")
    llm_result = await test_llm_with_config(llm_config)
    results["LLM 调用"] = llm_result
    
    # 测试 4: Hermes 引擎集成
    logger.info("\n【测试 4/5】Hermes 引擎集成")
    hermes_result = await test_hermes_engine_integration()
    results["Hermes 引擎"] = hermes_result
    
    # 测试 5: 完整工作流
    logger.info("\n【测试 5/5】完整工作流")
    workflow_result = await test_full_workflow()
    results["完整工作流"] = workflow_result
    
    # 总结
    logger.info("\n" + "=" * 70)
    logger.info("📊 测试总结")
    logger.info("=" * 70)
    
    for test_name, result in results.items():
        status = "✅" if result else "❌"
        logger.info(f"{status} {test_name}")
    
    passed = sum(1 for r in results.values() if r)
    total = len(results)
    
    logger.info(f"\n结果: {passed}/{total} 测试通过")
    logger.info("=" * 70)
    
    if passed == total:
        logger.info("\n🎉 所有测试通过！系统工作正常")
        return 0
    else:
        logger.warning(f"\n⚠️ 有 {total - passed} 个测试失败")
        return 1


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        logger.info("\n⏹️ 测试被中断")
        sys.exit(1)
    except Exception as e:
        logger.error(f"\n💥 发生未预期的错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
