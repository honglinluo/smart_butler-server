"""聊天 API - 消息交互与文件上传"""

import asyncio
import logging
from typing import List, Optional, AsyncIterator

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile, status, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from app.api.dependencies import get_current_user, get_user_model, require_local_or_auth
from app.core.hermes_engine import LLMInfo

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/chat", tags=["Chat"])


class ChatMessage(BaseModel):
    message:    str
    context:    dict           = {}
    agent_name: Optional[str]  = None  # 指定 Agent（可选）


async def get_hermes_engine(request: Request):
    """获取全局 Hermes 引擎实例"""
    hermes_engine = getattr(request.app.state, "hermes_engine", None)
    if not hermes_engine:
        raise HTTPException(status_code=500, detail="Hermes engine not initialized")
    return hermes_engine


@router.post("/send", response_model=dict)
async def send_message(
    chat_data: ChatMessage,
    request: Request,
    current_user: dict = Depends(get_current_user),
    user_model = Depends(get_user_model),
    engine = Depends(get_hermes_engine)
):
    """发送聊天消息"""
    user_id = current_user["user_id"]
    logger.debug("收到聊天请求: user_id=%s message=%r", user_id, chat_data.message)

    try:
        # 历史对话由 ContextManager 从 memory:{user_id}:turns 加载，此处无需重复读取
        context = chat_data.context or {}

        # 如果 user_model 是配置字典而不是已构建的 LLM 实例，尝试用 engine 构建模型
        llm_instance = None
        try:
            if isinstance(user_model, dict):
                llm_info = LLMInfo(
                    user_id=user_id,
                    url=user_model.get("url", ""),
                    api_key=user_model.get("api_key", ""),
                    model_name=user_model.get("model_name", ""),
                    model_type=user_model.get("model_type", "chat"),
                    temperature=float(user_model.get("temperature", 0.7)) if user_model.get("temperature") is not None else 0.7,
                )
                llm_instance = await engine._build_llm_from_config(llm_info)
            else:
                llm_instance = user_model
        except Exception:
            llm_instance = None

        logger.debug("调用引擎: user_id=%s context_keys=%s", user_id, list(context.keys()))
        response = await engine.process_user_input(
            user_id    =user_id,
            user_input =chat_data.message,
            context    =context,
            llm        =llm_instance,
            agent_name =chat_data.agent_name,
        )
        logger.debug(
            "引擎响应: user_id=%s response_len=%d preview=%r",
            user_id, len(response), response[:100],
        )

        return {
            "response": response,
            "user_id": user_id,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Message processing failed: {str(e)}"
        )


@router.post("/stream")
async def stream_message(
    chat_data: ChatMessage,
    request: Request,
    current_user: dict = Depends(get_current_user),
    user_model = Depends(get_user_model),
    engine = Depends(get_hermes_engine)
):
    """
    流式聊天接口，使用 SSE (Server-Sent Events) 格式实时推送 token。

    事件类型：
    - event: routing  — 路由决策完成（含 intent/mode/agent）
    - event: token    — LLM 输出 token 块（data.text）
    - event: done     — 完成（data.turn_id）
    - event: error    — 发生错误（data.message）
    """
    user_id = current_user["user_id"]
    logger.debug("收到流式聊天请求: user_id=%s message=%r", user_id, chat_data.message)

    llm_instance = None
    try:
        if isinstance(user_model, dict):
            llm_info = LLMInfo(
                user_id=user_id,
                url=user_model.get("url", ""),
                api_key=user_model.get("api_key", ""),
                model_name=user_model.get("model_name", ""),
                model_type=user_model.get("model_type", "chat"),
                temperature=float(user_model.get("temperature", 0.7)) if user_model.get("temperature") is not None else 0.7,
            )
            llm_instance = await engine._build_llm_from_config(llm_info)
        else:
            llm_instance = user_model
    except Exception:
        llm_instance = None

    async def event_generator() -> AsyncIterator[str]:
        try:
            async for sse_event in engine.process_user_input_stream(
                user_id=user_id,
                user_input=chat_data.message,
                context=chat_data.context or {},
                llm=llm_instance,
                agent_name=chat_data.agent_name,
            ):
                yield sse_event
        except asyncio.CancelledError:
            logger.debug("SSE 流被客户端断开: user_id=%s", user_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/history")
async def get_chat_history(
    request: Request,
    current_user: dict = Depends(get_current_user),
    size: int = Query(default=20, ge=1, le=100, description="返回条数"),
    page: int = Query(default=1, ge=1, description="页码（从 1 开始）"),
):
    """
    查询当前用户的聊天历史记录，从 Elasticsearch 分页返回。
    """
    user_id = current_user["user_id"]
    engine = getattr(request.app.state, "hermes_engine", None)
    if not engine or not hasattr(engine, "chat_history"):
        raise HTTPException(status_code=500, detail="Chat history service unavailable")

    try:
        from_ = (page - 1) * size
        turns = await engine.chat_history.get_recent_turns(user_id, size=size, from_=from_)
        total = await engine.chat_history.count_index_docs(user_id)
        return {
            "user_id": user_id,
            "page": page,
            "size": size,
            "total": total,
            "history": turns,
        }
    except Exception as e:
        logger.error("获取聊天历史失败 user=%s: %s", user_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to retrieve history: {str(e)}")


# ══════════════════════════════════════════════════════════════════════════════
# POST /chat/upload  — 上传文件 / 代码块 / 长文本，沙箱验证后保存
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/upload",
    summary="上传文件、代码块或长文本（沙箱验证）",
    description=(
        "所有上传内容先经沙箱检测，通过后保存到 `data/{user_id}/` 目录。\n\n"
        "**处理规则：**\n"
        "- **文件 / 代码文件**：在沙箱中执行，失败则拒绝保存\n"
        "- **图片**：校验真实类型 + Polyglot 检测\n"
        "- **长文本（text 参数）**：自动检测内嵌代码块并在沙箱中运行\n\n"
        "可同时携带 `message` 参数，保存完成后将文件摘要 + 消息一并传入 Hermes 引擎。\n\n"
        "**限制：**最多 5 个文件，单文件 ≤ 10 MB，支持扩展名见 scanner.py `_EXT_MAP`。"
    ),
)
async def upload_content(
    request:      Request,
    current_user: dict = Depends(get_current_user),
    user_model         = Depends(get_user_model),
    files:  List[UploadFile] = File(default=[], description="上传文件列表（文件、图片、代码等）"),
    text:   Optional[str]    = Form(default=None, description="长文本或代码片段"),
    message: Optional[str]   = Form(default=None, description="附带消息，处理完后发给 Hermes"),
    agent_name: Optional[str]= Form(default=None, description="指定 Agent（可选）"),
) -> dict:
    """
    上传入口。文件和长文本均经沙箱安全检查，通过后保存到用户目录。
    若同时携带 message，则将沙箱摘要注入上下文后调用 Hermes 引擎。
    """
    from app.sandbox.file_handler import file_handler, MAX_FILES_PER_REQUEST

    user_id = current_user["user_id"]

    # ── 文件数量限制 ──────────────────────────────────────────────────────────
    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"最多同时上传 {MAX_FILES_PER_REQUEST} 个文件，当前提交了 {len(files)} 个",
        )

    processed = []

    # ── 处理上传文件 ──────────────────────────────────────────────────────────
    for upload in files:
        if not upload.filename:
            continue
        try:
            content = await upload.read()
            result  = await file_handler.process_upload(
                filename = upload.filename,
                content  = content,
                user_id  = user_id,
            )
            processed.append(result.to_dict())
            if not result.safe:
                logger.warning(
                    "文件未通过沙箱 user=%s file=%s reason=%s",
                    user_id, upload.filename, result.error,
                )
        except Exception as e:
            logger.error("处理上传文件异常 user=%s file=%s: %s", user_id, upload.filename, e)
            processed.append({
                "original_name": upload.filename,
                "safe":          False,
                "error":         str(e),
            })

    # ── 处理长文本 ────────────────────────────────────────────────────────────
    text_result = None
    if text and text.strip():
        try:
            tr         = await file_handler.process_text(text=text, user_id=user_id)
            text_result = tr.to_dict()
            if not tr.safe:
                logger.warning(
                    "长文本未通过沙箱 user=%s reason=%s", user_id, tr.error
                )
        except Exception as e:
            logger.error("处理长文本异常 user=%s: %s", user_id, e)
            text_result = {"safe": False, "error": str(e)}

    # ── 构造摘要并（可选）转发给 Hermes ──────────────────────────────────────
    hermes_response: Optional[str] = None

    if message:
        engine = getattr(request.app.state, "hermes_engine", None)
        if engine:
            # 将沙箱摘要注入 context，让 Hermes 感知已处理的内容
            sandbox_summary = _build_sandbox_summary(processed, text_result)
            extra_context   = {"sandbox_results": sandbox_summary}

            llm_instance = None
            try:
                if isinstance(user_model, dict):
                    llm_info = LLMInfo(
                        user_id    = user_id,
                        url        = user_model.get("url", ""),
                        api_key    = user_model.get("api_key", ""),
                        model_name = user_model.get("model_name", ""),
                        model_type = user_model.get("model_type", "chat"),
                        temperature= float(user_model.get("temperature", 0.7)),
                    )
                    llm_instance = await engine._build_llm_from_config(llm_info)
            except Exception:
                pass

            try:
                hermes_response = await engine.process_user_input(
                    user_id    = user_id,
                    user_input = message,
                    context    = extra_context,
                    llm        = llm_instance,
                    agent_name = agent_name,
                )
            except Exception as e:
                logger.error("Hermes 处理上传消息失败 user=%s: %s", user_id, e)
                hermes_response = f"消息处理失败: {e}"

    return {
        "user_id":        user_id,
        "files":          processed,
        "text_result":    text_result,
        "hermes_response": hermes_response,
        "summary": {
            "total_files":  len(processed),
            "safe_files":   sum(1 for f in processed if f.get("safe")),
            "blocked_files": sum(1 for f in processed if not f.get("safe")),
            "text_safe":    text_result.get("safe") if text_result else None,
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# POST /chat/admin/revectorize  — 重新向量化聊天历史
# ══════════════════════════════════════════════════════════════════════════════

class RevectorizeRequest(BaseModel):
    user_id: Optional[str] = Field(None, description="指定用户 ID，不填则处理所有用户")
    date:    Optional[str] = Field(None, description="指定日期 YYYY-MM-DD，不填则不限日期")


@router.post(
    "/admin/revectorize",
    response_model=dict,
    summary="重新向量化聊天历史",
    description=(
        "重新向量化聊天历史并更新 ES 向量索引，任务在后台运行，立即返回。\n\n"
        "- 不传参数：全量重向量化所有用户所有历史\n"
        "- 仅 user_id：指定用户全量重向量化\n"
        "- 仅 date：所有用户指定日期重向量化\n"
        "- user_id + date：指定用户指定日期重向量化"
    ),
)
async def revectorize(
    req:     RevectorizeRequest,
    request: Request,
    _:       Optional[dict] = Depends(require_local_or_auth),
):
    """重新向量化聊天历史（本机无鉴权，远程需 Token）。"""
    vector_store = getattr(request.app.state, "vector_store", None)
    if not vector_store:
        raise HTTPException(status_code=503, detail="VectorStore 未初始化")

    embedding_service = getattr(request.app.state, "embedding_service", None)
    if not embedding_service or not embedding_service.enabled:
        raise HTTPException(status_code=503, detail="Embedding 服务未启用")

    asyncio.create_task(
        vector_store.revectorize_filtered(user_id=req.user_id, date_str=req.date)
    )

    return {
        "status":  "started",
        "user_id": req.user_id or "*（全部用户）",
        "date":    req.date    or "*（全部日期）",
        "message": "重向量化任务已在后台启动，请查看服务日志获取进度",
    }


def _build_sandbox_summary(processed: list, text_result: Optional[dict]) -> str:
    """将沙箱处理结果浓缩为 Hermes 可理解的文本摘要。"""
    lines = []
    for f in processed:
        status_str = "✅ 已保存" if f.get("safe") else f"❌ 拒绝（{f.get('error', '未知')}）"
        saved      = f"→ {f['saved_path']}" if f.get("saved_path") else ""
        blocks     = f.get("code_blocks_found", 0)
        blocks_str = f"，含 {blocks} 个代码块" if blocks else ""
        lines.append(f"- 文件 {f['original_name']!r}: {status_str}{blocks_str} {saved}".strip())
    if text_result:
        status_str = "✅ 已保存" if text_result.get("safe") else f"❌ 拒绝（{text_result.get('error', '未知')}）"
        blocks     = text_result.get("code_blocks_found", 0)
        lines.append(
            f"- 长文本: {status_str}"
            + (f"，检测到 {blocks} 个代码块并在沙箱中运行" if blocks else "")
        )
    return "\n".join(lines) if lines else "无上传内容"
