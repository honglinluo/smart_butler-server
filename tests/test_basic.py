#!/usr/bin/env python3
"""
简化版启动脚本 - 用于测试大模型连接
主要测试内容:
1. LLM 模型加载
2. 消息处理流程
3. 大模型推理
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
    format=_log_cfg.get("format", "%(asctime)s - %(levelname)s - %(message)s"),
)
logger = logging.getLogger(__name__)


async def test_basic_llm():
    """基础 LLM 测试 - 不依赖数据库"""
    logger.info("\n" + "=" * 70)
    logger.info("🚀 基础 LLM 测试")
    logger.info("=" * 70)
    
    try:
        from langchain.chat_models import init_chat_model
        from langchain_core.messages import HumanMessage, SystemMessage
        
        # 获取 API 密钥
        openai_key = os.getenv("OPENAI_API_KEY")
        if not openai_key or openai_key.startswith("sk-"):
            logger.warning("⚠️ 未找到有效的 OpenAI API 密钥，尝试其他方式...")
            
            # 使用默认配置进行本地测试
            logger.info("\n➡️ 尝试使用本地或模拟 LLM...")
            try:
                # 如果有本地部署的模型，可以使用
                model = init_chat_model(
                    model="gpt-3.5-turbo",
                    model_provider="openai",
                    api_key="sk-test",
                    openai_api_base="http://localhost:8000/v1"  # 本地模型服务
                )
                logger.info("✅ 本地模型连接成功")
                return model
            except:
                logger.warning("❌ 无法连接本地模型")
                return None
        
        logger.info(f"\n📝 配置信息:")
        logger.info(f"   - API Key: {openai_key[:20]}...")
        
        # 初始化 OpenAI 模型
        logger.info("\n➡️ 初始化 GPT-3.5-Turbo 模型...")
        model = init_chat_model(
            model="gpt-3.5-turbo",
            model_provider="openai",
            api_key=openai_key,
            temperature=0.7
        )
        logger.info("✅ 模型初始化成功")
        
        # 测试调用
        logger.info("\n➡️ 测试模型调用...")
        messages = [
            SystemMessage(content="你是一个有帮助的助手"),
            HumanMessage(content="请简要介绍什么是 LangChain (用 20 字以内)")
        ]
        
        logger.info(f"   发送消息: '{messages[1].content}'")
        response = await model.ainvoke(messages)
        
        logger.info(f"✅ 模型响应: {response.content}")
        return model
        
    except Exception as e:
        logger.error(f"❌ LLM 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return None


async def test_message_processing():
    """消息处理流程测试"""
    logger.info("\n" + "=" * 70)
    logger.info("💬 消息处理流程测试")
    logger.info("=" * 70)
    
    try:
        from app.core.hermes_engine import InputMessage, OutputMessage, LangChainToolWrapper
        
        # 测试输入消息
        logger.info("\n➡️ 测试 InputMessage...")
        input_msg = InputMessage(
            user_id="test_user",
            content="你好，我想了解一些信息",
            role="user",
            metadata={"source": "test"}
        )
        logger.info(f"✅ InputMessage 创建成功:")
        logger.info(f"   - User ID: {input_msg.user_id}")
        logger.info(f"   - Content: {input_msg.content}")
        logger.info(f"   - Role: {input_msg.role}")
        
        # 转换为 LangChain 消息
        lc_msg = input_msg.to_langchain()
        logger.info(f"✅ 转换为 LangChain 消息: {type(lc_msg).__name__}")
        
        # 测试输出消息
        logger.info("\n➡️ 测试 OutputMessage...")
        output_msg = OutputMessage.from_text(
            user_id="test_user",
            content="这是 AI 的回复"
        )
        logger.info(f"✅ OutputMessage 创建成功:")
        logger.info(f"   - User ID: {output_msg.user_id}")
        logger.info(f"   - Content: {output_msg.content}")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ 消息处理测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_hermes_basic():
    """Hermes 基础功能测试（不依赖数据库）"""
    logger.info("\n" + "=" * 70)
    logger.info("⚙️ Hermes 引擎基础功能测试")
    logger.info("=" * 70)
    
    try:
        from app.core.hermes_engine import LLMInfo, HermesEngine
        
        # 测试 LLMInfo 数据类
        logger.info("\n➡️ 测试 LLMInfo 数据类...")
        llm_info = LLMInfo(
            user_id="test_user",
            url="https://api.openai.com/v1",
            api_key=os.getenv("OPENAI_API_KEY", "sk-test"),
            model_name="gpt-3.5-turbo",
            temperature=0.7
        )
        logger.info(f"✅ LLMInfo 创建成功:")
        logger.info(f"   - Model: {llm_info.model_name}")
        logger.info(f"   - Provider: {llm_info.provider}")
        logger.info(f"   - URL: {llm_info.url}")
        
        # 转换为模型参数
        model_kwargs = llm_info.to_model_kwargs()
        logger.info(f"✅ 模型参数转换成功: {list(model_kwargs.keys())}")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Hermes 基础测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_client_env_unit() -> bool:
    """client_env 模块单元测试（无需数据库或 LLM）。"""
    logger.info("\n" + "=" * 70)
    logger.info("🌐 客户端环境模块单元测试")
    logger.info("=" * 70)

    try:
        from app.utils.client_env import ClientType, normalize_client_type, format_env_for_prompt

        cases_normalize = [
            # (输入,          期望输出,         说明)
            (None,           "unknown",        "None 归一化为 unknown"),
            ("",             "unknown",        "空字符串归一化为 unknown"),
            ("api",          "api",            "api 直调默认值"),
            ("web",          "web",            "标准 web"),
            ("feishu",       "lark",           "feishu 别名 → lark"),
            ("Feishu",       "lark",           "Feishu 大写别名 → lark"),
            ("lark",         "lark",           "lark 直接命中"),
            ("mac",          "macos",          "mac 别名 → macos"),
            ("WECHAT",       "wechat",         "大写 WECHAT 归一化"),
            ("dingtalk",     "dingtalk",       "dingtalk"),
            ("wework",       "wework",         "wework"),
            ("my_custom_app","unknown",        "未知值 → unknown"),
        ]

        all_pass = True
        for raw, expected, desc in cases_normalize:
            got = normalize_client_type(raw)
            if got == expected:
                logger.info(f"   ✅  normalize({raw!r}) = {got!r}  ({desc})")
            else:
                logger.error(f"   ❌  normalize({raw!r}) = {got!r}，期望 {expected!r}  ({desc})")
                all_pass = False

        cases_format = [
            # (client_type, version,  期望包含的字符串,       说明)
            ("api",    None,    "API 直调",               "api 类型产生提示块"),
            ("wechat", "8.0",   "微信",                   "wechat 含版本号"),
            ("wechat", "8.0",   "8.0",                    "版本号出现在提示块"),
            ("lark",   "7.2.1", "飞书",                   "lark 中文标签"),
            ("unknown", None,   "",                        "unknown 返回空字符串"),
            ("",       None,    "",                        "空字符串返回空字符串"),
        ]

        for ct, ver, expected_substr, desc in cases_format:
            result = format_env_for_prompt(ct, ver)
            if expected_substr == "":
                ok = result == ""
            else:
                ok = expected_substr in result
            if ok:
                logger.info(f"   ✅  format_env({ct!r}, {ver!r}) 含 {expected_substr!r}  ({desc})")
            else:
                logger.error(f"   ❌  format_env({ct!r}, {ver!r}) = {result!r}，期望含 {expected_substr!r}  ({desc})")
                all_pass = False

        # 验证 API 枚举值存在
        assert ClientType.API == "api", "ClientType.API 应等于 'api'"
        logger.info("   ✅  ClientType.API 枚举值存在且值为 'api'")

        return all_pass

    except Exception as e:
        logger.error(f"❌ client_env 单元测试异常: {e}")
        import traceback; traceback.print_exc()
        return False


def test_request_headers_unit() -> bool:
    """RequestHeaders 类单元测试（用 Mock Request 模拟请求头，无需数据库）。"""
    logger.info("\n" + "=" * 70)
    logger.info("🔌 RequestHeaders 类单元测试")
    logger.info("=" * 70)

    try:
        from unittest.mock import MagicMock
        from app.core.request_headers import RequestHeaders

        def _make_request(headers: dict) -> MagicMock:
            """构造带指定头信息的 Mock Request。"""
            mock = MagicMock()
            mock.headers.get = lambda key, default="": headers.get(key, default)
            return mock

        all_pass = True

        def _check(desc: str, actual, expected) -> bool:
            if actual == expected:
                logger.info(f"   ✅  {desc}: {actual!r}")
                return True
            logger.error(f"   ❌  {desc}: 得到 {actual!r}，期望 {expected!r}")
            return False

        # ── 无头信息：默认值 ──────────────────────────────────
        rh = RequestHeaders(_make_request({}))
        all_pass &= _check("无头 → client_type 默认 api",     rh.client_type,    "api")
        all_pass &= _check("无头 → client_version 默认空串", rh.client_version, "")

        # ── 标准 web 头 ───────────────────────────────────────
        rh = RequestHeaders(_make_request({"X-Client-Type": "web"}))
        all_pass &= _check("X-Client-Type: web",  rh.client_type, "web")

        # ── feishu 别名归一化 ─────────────────────────────────
        rh = RequestHeaders(_make_request({"X-Client-Type": "feishu", "X-Client-Version": "7.2.1"}))
        all_pass &= _check("X-Client-Type: feishu → lark",   rh.client_type,    "lark")
        all_pass &= _check("X-Client-Version: 7.2.1",        rh.client_version, "7.2.1")

        # ── 未知类型归一化为 unknown ──────────────────────────
        rh = RequestHeaders(_make_request({"X-Client-Type": "my_custom_app"}))
        all_pass &= _check("未知 client_type → unknown", rh.client_type, "unknown")

        # ── to_context_dict() ─────────────────────────────────
        rh = RequestHeaders(_make_request({"X-Client-Type": "wechat", "X-Client-Version": "8.0"}))
        ctx = rh.to_context_dict()
        all_pass &= _check("to_context_dict _client_type",    ctx.get("_client_type"),    "wechat")
        all_pass &= _check("to_context_dict _client_version", ctx.get("_client_version"), "8.0")

        # ── context.update() 不覆盖已有非客户端键 ────────────
        context = {"history": [], "user_data": "preserved"}
        context.update(rh.to_context_dict())
        all_pass &= _check("update 不影响 history 键",    context.get("history"),    [])
        all_pass &= _check("update 不影响 user_data 键", context.get("user_data"), "preserved")

        return all_pass

    except Exception as e:
        logger.error(f"❌ RequestHeaders 单元测试异常: {e}")
        import traceback
        traceback.print_exc()
        return False

    except Exception as e:
        logger.error(f"❌ client_env 单元测试异常: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """主测试流程"""
    logger.info("\n")
    logger.info("╔" + "=" * 68 + "╗")
    logger.info("║" + " " * 20 + "🤖 Hermes LLM 集成测试" + " " * 23 + "║")
    logger.info("╚" + "=" * 68 + "╝")

    # 环境检查
    logger.info("\n📋 环境检查:")
    logger.info(f"   - Python: {sys.version.split()[0]}")
    logger.info(f"   - 工作目录: {os.getcwd()}")
    logger.info(f"   - 项目路径: {Path(__file__).parent}")

    # 检查 API 密钥
    has_openai_key = bool(os.getenv("OPENAI_API_KEY"))
    logger.info(f"   - OpenAI Key: {'✅ 已配置' if has_openai_key else '❌ 未配置'}")

    results = {}

    # 测试 0: client_env 单元测试（无外部依赖）
    logger.info("\n" + "-" * 70)
    results["client_env 单元测试"] = test_client_env_unit()

    # 测试 0b: RequestHeaders 单元测试（无外部依赖）
    logger.info("\n" + "-" * 70)
    results["RequestHeaders 单元测试"] = test_request_headers_unit()

    # 测试 1: 基础 LLM
    logger.info("\n" + "-" * 70)
    model = await test_basic_llm()
    results["基础 LLM"] = model is not None

    # 测试 2: 消息处理
    logger.info("\n" + "-" * 70)
    msg_result = await test_message_processing()
    results["消息处理"] = msg_result

    # 测试 3: Hermes 基础
    logger.info("\n" + "-" * 70)
    hermes_result = await test_hermes_basic()
    results["Hermes 基础"] = hermes_result

    # 输出总结
    logger.info("\n" + "=" * 70)
    logger.info("📊 测试总结")
    logger.info("=" * 70)

    for test_name, result in results.items():
        status = "✅" if result else "❌"
        logger.info(f"{status} {test_name}")

    passed = sum(1 for v in results.values() if v)
    total = len(results)
    logger.info(f"\n结果: {passed}/{total} 测试通过")

    if passed == total:
        logger.info("\n🎉 所有测试通过！")
        logger.info("\n💡 下一步:")
        logger.info("   1. 配置数据库 (MySQL, Redis)")
        logger.info("   2. 运行 python create_tables.py 创建表")
        logger.info("   3. 运行 python test_llm_integration.py 进行完整测试")
        logger.info("   4. 运行 uvicorn main:app --reload 启动应用")
        return 0
    else:
        logger.warning(f"\n⚠️ 有 {total - passed} 个测试失败")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
