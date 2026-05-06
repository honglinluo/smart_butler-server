"""RAG（检索增强生成）模块。

对外暴露接口：
  RagPipeline — 唯一入口类，覆盖检索、向量索引、重向量化、预取四条路径。
  RagContext  — RAG 上下文快照，调用 .to_prompt_context() 可直接传入 LLM。

使用示例::

    from app.rag import RagPipeline, RagContext

    # 初始化（main.py lifespan）
    rag = RagPipeline(embedding_service, vector_store, memory_manager, config)
    hermes_engine.set_rag_pipeline(rag)

    # 推理前组装上下文
    ctx: RagContext = await rag.build_context(user_id, user_input, base_context)
    context = ctx.to_prompt_context()

    # 对话存储后异步索引
    asyncio.create_task(rag.index_turn(user_id, turn_id, user_input, response))

    # 管理员重向量化
    stats = await rag.revectorize(user_id=None, date_str=None)
"""

from app.rag.pipeline import RagPipeline
from app.rag.types    import RagContext

__all__ = ["RagPipeline", "RagContext"]
