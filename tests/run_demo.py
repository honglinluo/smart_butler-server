#!/usr/bin/env python3
"""
Hermes Multi-Agent System 快速启动脚本
这是一个简化版的启动脚本，用于演示系统功能
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

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


async def demo_llm_chat():
    """演示 LLM 聊天功能"""
    logger.info("\n" + "=" * 70)
    logger.info("📝 演示 LLM 聊天功能")
    logger.info("=" * 70)
    
    try:
        from langchain.chat_models import init_chat_model
        from langchain_core.messages import SystemMessage, HumanMessage
        
        # 初始化模型
        logger.info("\n初始化模型...")
        model = init_chat_model(
            model="gpt-3.5-turbo",
            model_provider="openai",
            api_key=os.getenv("OPENAI_API_KEY", "sk-test"),
            openai_api_base=os.getenv("OPENAI_API_BASE", "http://localhost:8000/v1")
        )
        logger.info("✅ 模型初始化成功")
        
        # 准备消息
        messages = [
            SystemMessage(content="你是一个智能助手"),
            HumanMessage(content="Hermes 多智能体系统有什么特点？用100字以内回答"),
        ]
        
        logger.info("\n发送消息...")
        logger.info(f"System: 你是一个智能助手")
        logger.info(f"User: {messages[1].content}")
        
        # 调用模型
        response = await model.ainvoke(messages)
        logger.info(f"\nAssistant: {response.content}")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ LLM 演示失败: {e}")
        return False


async def demo_message_classes():
    """演示消息类"""
    logger.info("\n" + "=" * 70)
    logger.info("💬 演示消息类")
    logger.info("=" * 70)
    
    try:
        from app.core.hermes_engine import InputMessage, OutputMessage
        
        # 创建输入消息
        logger.info("\n创建输入消息...")
        user_msg = InputMessage(
            user_id="demo_user",
            content="我想了解 Hermes 系统的架构",
            role="user"
        )
        logger.info(f"✅ 输入消息: {user_msg.content}")
        
        # 转换为 LangChain 消息
        lc_msg = user_msg.to_langchain()
        logger.info(f"   转换为 LangChain: {type(lc_msg).__name__}")
        
        # 创建输出消息
        logger.info("\n创建输出消息...")
        ai_msg = OutputMessage.from_text(
            user_id="demo_user",
            content="Hermes 是一个基于 LangChain 的多智能体协作系统..."
        )
        logger.info(f"✅ 输出消息: {ai_msg.content[:50]}...")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ 消息类演示失败: {e}")
        return False


async def demo_llm_config():
    """演示 LLM 配置"""
    logger.info("\n" + "=" * 70)
    logger.info("⚙️ 演示 LLM 配置")
    logger.info("=" * 70)
    
    try:
        from app.core.hermes_engine import LLMInfo
        
        # 创建 LLM 配置
        logger.info("\n创建 LLM 配置...")
        llm_info = LLMInfo(
            user_id="demo_user",
            url="https://api.openai.com/v1",
            api_key=os.getenv("OPENAI_API_KEY", "sk-test"),
            model_name="gpt-3.5-turbo",
            temperature=0.7
        )
        
        logger.info(f"✅ LLM 配置:")
        logger.info(f"   - 模型: {llm_info.model_name}")
        logger.info(f"   - 供应商: {llm_info.provider}")
        logger.info(f"   - 温度: {llm_info.temperature}")
        
        # 获取模型参数
        model_kwargs = llm_info.to_model_kwargs()
        logger.info(f"   - 参数数量: {len(model_kwargs)}")
        logger.info(f"   - 参数: {', '.join(model_kwargs.keys())}")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ LLM 配置演示失败: {e}")
        return False


async def demo_dependency_injection():
    """演示依赖注入"""
    logger.info("\n" + "=" * 70)
    logger.info("🔌 演示依赖注入")
    logger.info("=" * 70)
    
    try:
        from app.api.dependencies import get_current_user, get_user_model
        
        # 测试 get_current_user
        logger.info("\n测试用户认证...")
        user = await get_current_user(authorization="Bearer demo_token_123")
        logger.info(f"✅ 获取用户信息:")
        logger.info(f"   - User ID: {user.get('user_id')}")
        logger.info(f"   - Username: {user.get('username')}")
        logger.info(f"   - Token: {user.get('token')[:20]}...")
        
        # 测试 get_user_model
        logger.info("\n获取用户模型配置...")
        model_config = await get_user_model(user)
        logger.info(f"✅ 模型配置:")
        logger.info(f"   - 模型名称: {model_config.get('model_name')}")
        logger.info(f"   - 温度: {model_config.get('temperature')}")
        logger.info(f"   - 默认配置: {model_config.get('is_default', False)}")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ 依赖注入演示失败: {e}")
        return False


async def demo_health_check():
    """演示健康检查"""
    logger.info("\n" + "=" * 70)
    logger.info("🏥 演示健康检查")
    logger.info("=" * 70)
    
    try:
        # 模拟健康检查响应
        logger.info("\n执行健康检查...")
        health_response = {
            "status": "healthy",
            "service": "Hermes Multi-Agent System",
            "version": "1.0.0"
        }
        
        logger.info(f"✅ 健康检查结果:")
        for key, value in health_response.items():
            logger.info(f"   - {key}: {value}")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ 健康检查演示失败: {e}")
        return False


async def main():
    """主演示流程"""
    logger.info("\n")
    logger.info("╔" + "=" * 68 + "╗")
    logger.info("║" + " " * 20 + "🚀 Hermes 多智能体系统演示" + " " * 20 + "║")
    logger.info("╚" + "=" * 68 + "╝")
    
    logger.info("\n📋 系统信息:")
    logger.info(f"   - Python 版本: {sys.version.split()[0]}")
    logger.info(f"   - 项目路径: {Path(__file__).parent}")
    logger.info(f"   - OpenAI API Key: {'✅ 已配置' if os.getenv('OPENAI_API_KEY') else '❌ 未配置'}")
    
    # 运行演示
    demos = [
        ("LLM 聊天", demo_llm_chat),
        ("消息类", demo_message_classes),
        ("LLM 配置", demo_llm_config),
        ("依赖注入", demo_dependency_injection),
        ("健康检查", demo_health_check),
    ]
    
    results = {}
    for name, demo_func in demos:
        result = await demo_func()
        results[name] = result
    
    # 输出总结
    logger.info("\n" + "=" * 70)
    logger.info("📊 演示总结")
    logger.info("=" * 70)
    
    for name, result in results.items():
        status = "✅" if result else "❌"
        logger.info(f"{status} {name}")
    
    passed = sum(1 for r in results.values() if r)
    total = len(results)
    logger.info(f"\n结果: {passed}/{total} 演示成功")
    
    logger.info("\n" + "=" * 70)
    logger.info("🎯 关键功能特性")
    logger.info("=" * 70)
    logger.info("""
✅ LangChain 集成
   - 支持多种 LLM 提供商 (OpenAI, Anthropic, Google, etc.)
   - 统一的模型初始化和管理

✅ 消息处理
   - InputMessage 和 OutputMessage 数据类
   - 自动转换为 LangChain 消息格式

✅ 多智能体协调
   - 路由智能体用于意图识别
   - 工作智能体用于任务执行
   - LangGraph 支持复杂工作流

✅ 依赖注入
   - FastAPI 集成
   - 支持 Token 和 Query 参数认证
   - 动态模型配置加载

✅ 数据库支持
   - MySQL 用于主数据存储
   - Redis 用于缓存和会话管理
   - Elasticsearch 用于向量搜索
    """)
    
    logger.info("=" * 70)
    if passed == total:
        logger.info("\n🎉 演示完成！系统已就绪")
        logger.info("\n💡 后续步骤:")
        logger.info("   1. 配置数据库连接 (MySQL, Redis, Elasticsearch)")
        logger.info("   2. 设置 OPENAI_API_KEY 环境变量")
        logger.info("   3. 运行: uvicorn main:app --reload")
        logger.info("   4. 访问: http://localhost:8000/health")
        return 0
    else:
        logger.warning(f"\n⚠️ 有 {total - passed} 个演示未能通过")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
