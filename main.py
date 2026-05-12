"""智能管家 - 服务端主入口"""

import os
from pathlib import Path

# 项目启动时设置项目根目录环境变量（其他模块通过 app.core.paths.PROJECT_ROOT 读取）
os.environ.setdefault("PROJECT_ROOT", str(Path(__file__).parent.resolve()))

import asyncio
import logging
from contextlib import asynccontextmanager

import yaml
from fastapi import FastAPI, Depends

from app.core.config_loader import ConfigLoader
from app.core.embedding_service import EmbeddingService
from app.core.hermes_engine import HermesEngine
from app.core.memory_manager import MemoryManager
from app.core.vector_store import VectorStore
from app.rag import RagPipeline
from app.database.pool import initialize_pools, close_all_pools, get_connection, release_connection
from app.api import auth, models, chat, agents_api, tools_api, scheduler_api, decision_api, files_api, skills_api
from app.api.dependencies import get_current_user
from app.api.auth import _flush_profile_to_mysql
from app.core.redis_keys import USER_INIT


def _read_log_cfg() -> dict:
    """同步读取日志配置（模块加载阶段执行，在 ConfigLoader 异步初始化之前）。"""
    try:
        p = Path(os.environ["PROJECT_ROOT"]) / "config" / "system_config.yaml"
        if p.exists():
            with open(p, encoding="utf-8") as f:
                return (yaml.safe_load(f) or {}).get("logging", {})
    except Exception:
        pass
    return {}

_log_cfg = _read_log_cfg()
from app.utils.log_bus import init_log_bus
init_log_bus(_log_cfg)
logger = logging.getLogger(__name__)


# 全局组件（由 lifespan 管理生命周期）
config_loader:     ConfigLoader              = None
hermes_engine:     HermesEngine              = None
memory_manager:    MemoryManager             = None
embedding_service: EmbeddingService          = None
vector_store:      VectorStore               = None
rag_pipeline:      RagPipeline               = None


# ═══════════════════════════════════════════════════════════════
# Embedding 配置校验与同步
# ═══════════════════════════════════════════════════════════════

async def _ensure_llms_table_has_embedding_type() -> None:
    """确保 llms 表的 model_type 枚举包含 'embedding'，不含则 ALTER TABLE。"""
    conn = None
    try:
        conn = await get_connection("mysql", None)
        # 查询枚举定义
        df = await conn.execute_raw(
            "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'llms' AND COLUMN_NAME = 'model_type'",
            {}
        )
        if df is None or len(df) == 0:
            return
        col_type: str = df.iloc[0]["COLUMN_TYPE"] or ""
        if "embedding" not in col_type.lower():
            await conn.execute_raw(
                "ALTER TABLE llms MODIFY COLUMN model_type "
                "ENUM('text','image','multimodal','embedding') NOT NULL",
                {}
            )
            logger.info("llms.model_type 枚举已扩展，添加 'embedding'")
    except Exception as e:
        logger.warning(f"扩展 llms.model_type 枚举失败（可忽略）: {e}")
    finally:
        if conn:
            await release_connection("mysql", conn)


async def _get_db_embedding_config(system_user_id: str) -> dict:
    """从 MySQL llms 表读取系统用户的 embedding 模型记录。"""
    conn = None
    try:
        conn = await get_connection("mysql", None)
        # 兼容 llms 和 llm_info 两个可能的表名
        for table in ("llms", "llm_info"):
            try:
                df = await conn.execute_raw(
                    f"SELECT model_name, url FROM {table} "
                    "WHERE user_id = :uid AND model_type = 'embedding' AND state = 1 "
                    "ORDER BY id DESC LIMIT 1",
                    {"uid": system_user_id}
                )
                if df is not None and len(df) > 0:
                    row = df.iloc[0]
                    return {
                        "model_name": row.get("model_name", ""),
                        "url":        row.get("url", ""),
                        "table":      table,
                    }
            except Exception:
                continue
        return {}
    except Exception as e:
        logger.warning(f"读取 DB embedding 配置失败: {e}")
        return {}
    finally:
        if conn:
            await release_connection("mysql", conn)


async def _upsert_db_embedding_config(
    system_user_id: str, model_name: str, api_url: str
) -> None:
    """将 embedding 模型信息写入 MySQL llms 表（不存在则插入，存在则更新）。"""
    conn = None
    try:
        conn = await get_connection("mysql", None)
        # 确保系统用户存在（user_id = "0"）
        await conn.execute_raw(
            "INSERT IGNORE INTO user (user_id, username, password) "
            "VALUES (:uid, 'system', 'N/A')",
            {"uid": system_user_id}
        )
        await conn.execute_raw(
            """
            INSERT INTO llms (user_id, url, api_key, model_name, model_type, temperature, state)
            VALUES (:uid, :url, '', :model, 'embedding', 0, 1)
            ON DUPLICATE KEY UPDATE url = :url, model_name = :model, state = 1
            """,
            {"uid": system_user_id, "url": api_url, "model": model_name}
        )
        logger.info(f"DB embedding 配置已更新: model={model_name} url={api_url}")
    except Exception as e:
        logger.warning(f"写入 DB embedding 配置失败: {e}")
    finally:
        if conn:
            await release_connection("mysql", conn)


async def validate_embedding_config(config: dict, vs: VectorStore, rag=None) -> bool:
    """校验配置文件中的 embedding 模型与 MySQL 是否一致。

    不一致时：
      1. 更新 MySQL llms 表
      2. 删除 ES 所有向量索引
      3. 后台启动全量重向量化任务

    返回 True 表示配置一致（无需重建），False 表示触发了重建。
    """
    embed_cfg = config.get("embedding", {})
    cfg_model = embed_cfg.get("model_name", "").strip()
    cfg_url   = embed_cfg.get("api_url", "http://localhost:11434").rstrip("/")
    sys_uid   = embed_cfg.get("system_user_id", "0")

    if not cfg_model:
        logger.warning("embedding.model_name 未配置，跳过向量化。请运行 scripts/setup_embedding.py")
        return True

    await _ensure_llms_table_has_embedding_type()
    db_cfg = await _get_db_embedding_config(sys_uid)

    db_model = db_cfg.get("model_name", "").strip()
    db_url   = db_cfg.get("url", "").rstrip("/")

    if db_model == cfg_model and db_url == cfg_url:
        logger.info(f"向量模型配置一致 ({cfg_model})，无需重建索引")
        return True

    logger.info(
        f"向量模型配置变更: [{db_model}@{db_url}] → [{cfg_model}@{cfg_url}]，"
        "开始重建向量索引..."
    )

    # 1. 更新 MySQL
    await _upsert_db_embedding_config(sys_uid, cfg_model, cfg_url)

    # 2. 删除 ES 向量索引
    deleted = await (rag.delete_all_vector_indices() if rag else vs.delete_all_vector_indices())
    logger.info(f"已删除旧向量索引 {deleted} 个")

    # 3. 后台重向量化（不阻塞启动）
    asyncio.create_task(rag.revectorize() if rag else vs.revectorize_all_history())
    logger.info("已启动全量历史向量化后台任务")
    return False


# ═══════════════════════════════════════════════════════════════
# 关闭时画像固化
# ═══════════════════════════════════════════════════════════════

async def _flush_all_profiles_on_shutdown() -> None:
    """程序关闭时将 Redis 中所有用户画像批量固化到 MySQL。"""
    redis_conn = None
    try:
        redis_conn = await get_connection("redis", None)
    except Exception:
        logger.warning("[shutdown] Redis 不可用，跳过画像固化")
        return

    try:
        pattern = USER_INIT.format(user_id="*")  # "user:*:init"
        keys = await redis_conn.scan_keys(pattern)
        if not keys:
            logger.info("[shutdown] Redis 中无用户画像缓存，无需固化")
            return

        logger.info(f"[shutdown] 开始固化 {len(keys)} 个用户画像到 MySQL...")
        success = 0
        for key in keys:
            try:
                # 从 key "user:{user_id}:init" 提取 user_id
                parts = key.split(":")
                if len(parts) != 3:
                    continue
                user_id = parts[1]

                init_data = await redis_conn.read(key)
                if not isinstance(init_data, dict):
                    continue
                profile = init_data.get("profile")
                if not profile:
                    continue

                await _flush_profile_to_mysql(user_id, profile)
                success += 1
            except Exception as e:
                logger.warning(f"[shutdown] 固化用户 {key} 画像失败: {e}")

        logger.info(f"[shutdown] 画像固化完成，成功 {success}/{len(keys)} 个用户")
    except Exception as e:
        logger.error(f"[shutdown] 批量固化画像异常: {e}")
    finally:
        await release_connection("redis", redis_conn)


# ═══════════════════════════════════════════════════════════════
# FastAPI Lifespan
# ═══════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(application: FastAPI):
    from app.scheduler.runner import scheduler as task_scheduler
    global config_loader, hermes_engine, memory_manager, embedding_service, vector_store, rag_pipeline

    logger.info("🚀 启动智能管家服务端...")
    try:
        # 1. 加载配置
        config_loader = ConfigLoader("config")
        system_config = config_loader.load_system_config()
        config_loader.load_agents_config()
        logger.info("✅ 系统配置加载成功")

        # 2. 初始化数据库连接池
        await initialize_pools(system_config.get('database', {}))
        logger.info("✅ 数据库连接池初始化成功")

        # 2b. 数据库结构迁移（幂等，仅在枚举不包含时执行）
        await _ensure_llms_table_has_embedding_type()

        # 3. 初始化记忆管理器
        memory_manager = MemoryManager(system_config)
        application.state.memory_manager = memory_manager
        logger.info("✅ 记忆管理器初始化成功")

        # 4. 初始化 Hermes 引擎，注入 MemoryManager
        hermes_engine = HermesEngine(system_config)
        await hermes_engine.initialize()
        hermes_engine.set_memory_manager(memory_manager)
        application.state.hermes_engine = hermes_engine
        logger.info("✅ Hermes 引擎初始化成功")

        # 5. 初始化 Embedding 服务与 VectorStore
        embedding_service = EmbeddingService(system_config)
        vector_store      = VectorStore(embedding_service, system_config)

        # 5b. 创建 RagPipeline（统一 RAG 检索/索引/重向量化接口）
        rag_pipeline = RagPipeline(embedding_service, vector_store, memory_manager, system_config)
        hermes_engine.set_rag_pipeline(rag_pipeline)
        logger.info("✅ RagPipeline 初始化成功")

        if embedding_service.enabled:
            available = await embedding_service.is_available()
            if available:
                logger.info(
                    f"✅ Embedding 服务可用: provider={embedding_service.provider} "
                    f"model={embedding_service.model_name}"
                )
                # 6. 校验 embedding 配置，必要时重建向量索引
                await validate_embedding_config(system_config, vector_store, rag_pipeline)
            else:
                logger.warning(
                    f"⚠️  Embedding 服务不可达 ({embedding_service.api_url})，"
                    "向量化功能暂停，请检查服务配置后重启。"
                )
        else:
            logger.warning("⚠️  embedding.model_name 未配置，向量化功能关闭。")

        # 7. 自动发现并注册 workers/ 目录下所有 Worker Agent 到 registry
        try:
            import importlib
            import inspect
            import pkgutil
            import app.agents.workers as _workers_pkg
            from app.agents.base import BaseAgent as _BaseAgent
            from app.agents.registry import registry

            _count = 0
            for _importer, _modname, _ispkg in pkgutil.iter_modules(_workers_pkg.__path__):
                try:
                    _mod = importlib.import_module(f"app.agents.workers.{_modname}")
                    for _clsname, _cls in inspect.getmembers(_mod, inspect.isclass):
                        if (
                            issubclass(_cls, _BaseAgent)
                            and _cls is not _BaseAgent
                            and _cls.__module__ == _mod.__name__
                        ):
                            registry.register(_cls())
                            _count += 1
                except Exception as _mod_err:
                    logger.warning(f"⚠️  加载 Worker Agent 模块 '{_modname}' 失败: {_mod_err}")
            logger.info(f"✅ 已自动注册 {_count} 个 Worker Agent 到 registry")
        except Exception as e:
            logger.warning(f"⚠️  注册 Worker Agent 失败（可忽略）: {e}")

        # 7b. 加载 DB Agent 到 registry
        try:
            db_agent_count = await agents_api._load_db_agents_to_registry()
            logger.info(f"✅ 已加载 {db_agent_count} 个 DB Agent 到 registry")
        except Exception as e:
            logger.warning(f"⚠️  加载 DB Agent 失败（可忽略）: {e}")

        # 7c. 加载内置工具（触发自动注册）
        try:
            import app.tools.builtin  # noqa: F401
            logger.info("✅ 内置工具已注册")
        except Exception as e:
            logger.warning(f"⚠️  内置工具注册失败（可忽略）: {e}")

        # 8. 将 VectorStore 注入 MemoryManager（Redis/MySQL 直写路径仍需要）
        memory_manager.set_vector_store(vector_store)

        application.state.vector_store      = vector_store
        application.state.embedding_service = embedding_service
        application.state.rag_pipeline      = rag_pipeline

        # 9. 启动定时任务调度器
        try:
            task_scheduler.set_hermes_engine(hermes_engine)
            await task_scheduler.start()
            application.state.task_scheduler = task_scheduler
            logger.info("✅ 定时任务调度器启动成功")
        except Exception as e:
            logger.warning(f"⚠️  定时任务调度器启动失败（可忽略）: {e}")

        # 10. 注册系统级定时任务（月度/年度记忆归档，用户不可见）
        try:
            from app.scheduler.system_tasks import register_system_tasks
            await register_system_tasks(task_scheduler)
            logger.info("✅ 系统定时任务注册完成")
        except Exception as e:
            logger.warning(f"⚠️  系统定时任务注册失败（可忽略）: {e}")

        logger.info("🎉 系统启动完成！")

    except Exception as e:
        logger.error(f"❌ 启动失败: {e}")
        raise

    yield  # 应用运行期间在此挂起

    logger.info("🛑 正在关闭智能管家服务端...")
    try:
        # 停止定时任务调度器
        try:
            await task_scheduler.stop()
        except Exception as e:
            logger.warning(f"⚠️  停止调度器失败: {e}")

        if embedding_service:
            await embedding_service.close()

        if hermes_engine:
            await hermes_engine.shutdown()
            if hasattr(application.state, "hermes_engine"):
                application.state.hermes_engine = None

        # 关闭前将 Redis 中所有用户画像固化到 MySQL
        await _flush_all_profiles_on_shutdown()

        await close_all_pools()
        logger.info("✅ 系统关闭完成")
    except Exception as e:
        logger.error(f"❌ 关闭失败: {e}")


# ═══════════════════════════════════════════════════════════════
# FastAPI App
# ═══════════════════════════════════════════════════════════════

app = FastAPI(
    title="智能管家",
    description="智能管家服务端 - 支持多代理协作、记忆管理、工具编排",
    version="1.0.0",
    debug=True,
    lifespan=lifespan,
)

app.include_router(auth.router)
app.include_router(models.router)
app.include_router(chat.router)
app.include_router(agents_api.router)
app.include_router(tools_api.router)
app.include_router(scheduler_api.router)
app.include_router(decision_api.router)
app.include_router(files_api.router)
app.include_router(skills_api.router)


@app.get("/health")
async def health_check():
    return {
        "status":   "healthy",
        "service":  "智能管家",
        "version":  "1.0.0",
        "embedding": {
            "enabled":    embedding_service.enabled if embedding_service else False,
            "model":      embedding_service.model_name if embedding_service else "",
        },
    }


@app.get("/config/system")
async def get_system_config(_: dict = Depends(get_current_user)):
    if config_loader:
        return config_loader.get_system_config()
    return {"error": "配置加载器未初始化"}


@app.get("/config/agents")
async def get_agents_config(_: dict = Depends(get_current_user)):
    if config_loader:
        return config_loader.get_agents_config()
    return {"error": "配置加载器未初始化"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="debug")
