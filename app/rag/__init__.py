"""
【模块说明】RAG 检索增强生成模块 — 让 AI 记住用户的历史对话

RAG（Retrieval-Augmented Generation，检索增强生成）是一种让 AI 利用历史信息的技术。
AI 不是全知全能的，它回答问题时只能依赖当前对话内容。但如果加上 RAG，
AI 可以在回答前先"查一查"用户的历史对话，找到相关的旧记忆，再结合当前问题一起回答。

【简单比喻】
  普通 AI = 没有笔记本的人，每次对话都是从零开始
  RAG AI  = 有笔记本的人，回答前会翻一翻之前记录的重要信息

【这个包包含什么】
  pipeline.py  — RAG 的总入口，对外提供统一的调用接口
  retriever.py — 检索器：从历史记忆中找到和当前问题最相关的内容
  indexer.py   — 索引器：把每轮对话向量化，存入搜索索引
  chunker.py   — 切片器：把一轮对话拆分为多个小片段（便于精确检索）
  formatter.py — 格式化器：把找到的记忆整理成 AI 可以理解的提示词格式
  types.py     — 数据结构定义（RagContext 等）

RAG（检索增强生成）模块。

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
