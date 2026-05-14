"""
【模块说明】聊天接口 — 用户与 AI 对话的入口

这个文件是用户和 AI 进行对话的核心通道，提供以下功能：

1. 普通对话（/chat/send）
   用户发一条消息，AI 处理完后把完整回复一次性返回。

2. 流式对话（/chat/stream）
   AI 回复像打字机一样逐字实时显示给用户，无需等待全部生成完毕。
   技术上使用"SSE（Server-Sent Events）"协议实现：服务器主动向浏览器推送数据。

3. 危险操作授权（/chat/consent）
   当 AI 准备执行"危险动作"（如修改文件、运行命令）时，
   会先暂停并弹出确认框等待用户批准，此接口用于接收用户的批准/拒绝决定。

4. 取消对话（/chat/cancel）
   用户可以随时中断正在生成的 AI 回复。

5. 历史记录（/chat/history）
   查询当前用户的历史聊天记录，支持分页。

6. 文件上传（/chat/upload）
   上传文件或代码，系统先在沙箱（隔离环境）中安全检验，通过后保存并可附带消息让 AI 处理。
"""



import asyncio
import logging
from typing import List, Literal, Optional, AsyncIterator

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, Response, UploadFile, status, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from app.api.dependencies import get_current_user, get_user_model, require_local_or_auth
from app.utils.headers import RequestHeaders, ResponseHeaders
from app.agents.registry import registry as agent_registry

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/chat", tags=["Chat"])


class ChatMessage(BaseModel):
    """
    用户发送消息的数据结构。
    message：用户输入的文字内容（必填）
    context：附加上下文信息，如当前环境、设备信息等（可为空）
    agent_name：指定由哪个 AI 助手来回答，不指定则由系统自动选择最合适的
    """
    message:    str
    context:    dict          = {}
    agent_name: Optional[str] = None  # 指定 Agent（可选）


async def get_hermes_engine(request: Request):
    """
    从服务器应用状态中取出"赫尔墨斯引擎"（AI 调度核心）。
    每个请求都需要先拿到这个引擎才能处理对话，如果引擎未初始化则报错。
    """
    hermes_engine = getattr(request.app.state, "hermes_engine", None)
    if not hermes_engine:
        raise HTTPException(status_code=500, detail="Hermes engine not initialized")
    return hermes_engine


@router.post("/send", response_model=dict)
async def send_message(
    chat_data: ChatMessage,
    request: Request,
    response: Response,
    current_user: dict = Depends(get_current_user),
    user_model = Depends(get_user_model),
    engine = Depends(get_hermes_engine),
    req_headers: RequestHeaders = Depends(RequestHeaders),
):
    """
    【普通对话接口】用户发送一条消息，等 AI 完全处理完后返回完整回复。

    流程：
      1. 验证用户登录状态
      2. 准备好 AI 模型配置（用户自己选的模型）
      3. 把消息交给"赫尔墨斯引擎"处理（它负责理解意图、调度 Agent、生成回复）
      4. 返回 AI 的完整回复文本
    """
    ResponseHeaders().apply(response)
    user_id = current_user["user_id"]
    logger.debug("收到聊天请求: user_id=%s message=%r", user_id, chat_data.message)

    # 校验 agent_name 是否当前用户可用
    if chat_data.agent_name:
        available = {ag.name for ag in agent_registry.list_available_for_user(user_id)}
        if chat_data.agent_name not in available:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Agent '{chat_data.agent_name}' 不存在或当前用户无权限使用",
            )

    try:
        # 历史对话由 ContextManager 从 memory:{user_id}:turns 加载，此处无需重复读取
        context = chat_data.context or {}

        # user_model 为 LLMInfo 对象，构建 BaseChatModel 实例
        try:
            llm_instance = await engine._build_llm_from_config(user_model)
        except Exception:
            llm_instance = None

        # 从请求头注入客户端环境（无 header 时默认 api，engine 内部会传递给子 Agent）
        context.update(req_headers.to_context_dict())

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
    response: Response,
    current_user: dict = Depends(get_current_user),
    user_model = Depends(get_user_model),
    engine = Depends(get_hermes_engine),
    req_headers: RequestHeaders = Depends(RequestHeaders),
):
    """
    流式聊天接口，使用 SSE (Server-Sent Events) 格式实时推送 token。

    事件类型：
    - event: routing  — 路由决策完成（含 intent/mode/agent）
    - event: token    — LLM 输出 token 块（data.text）
    - event: done     — 完成（data.turn_id）
    - event: error    — 发生错误（data.message）
    """
    ResponseHeaders(extra={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}).apply(response)
    user_id = current_user["user_id"]
    logger.debug("收到流式聊天请求: user_id=%s message=%r", user_id, chat_data.message)

    # 校验 agent_name 是否当前用户可用
    if chat_data.agent_name:
        available = {ag.name for ag in agent_registry.list_available_for_user(user_id)}
        if chat_data.agent_name not in available:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Agent '{chat_data.agent_name}' 不存在或当前用户无权限使用",
            )

    # user_model 为 LLMInfo 对象，构建 BaseChatModel 实例
    try:
        llm_instance = await engine._build_llm_from_config(user_model)
    except Exception:
        llm_instance = None

    stream_context = dict(chat_data.context or {})
    # 从请求头注入客户端环境（无 header 时默认 api）
    stream_context.update(req_headers.to_context_dict())

    async def event_generator() -> AsyncIterator[str]:
        try:
            async for sse_event in engine.process_user_input_stream(
                user_id=user_id,
                user_input=chat_data.message,
                context=stream_context,
                llm=llm_instance,
                agent_name=chat_data.agent_name,
            ):
                yield sse_event
        except asyncio.CancelledError:
            logger.debug("SSE 流被客户端断开: user_id=%s", user_id)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


class ConsentResponse(BaseModel):
    """
    用户对"危险操作授权"的回复数据结构。
    request_id：对应哪一次危险操作请求的 ID
    decision（决策选项）：
      - allow        → 仅允许这一次，下次同类操作还会再问
      - deny         → 拒绝，AI 不执行该操作
      - conversation → 本轮对话内所有危险操作都自动放行，不再弹窗
    """
    request_id: str
    decision:   Literal["allow", "deny", "conversation"] = "deny"


@router.post("/consent")
async def respond_consent(
    body:         ConsentResponse,
    request:      Request,
    response:     Response,
    current_user: dict = Depends(get_current_user),
    engine             = Depends(get_hermes_engine),
):
    """响应危险操作授权请求。

    前端在收到 `consent_required` SSE 事件后，用户选择后调用此接口。
    decision 取值：
      - allow        — 仅本次允许
      - deny         — 拒绝
      - conversation — 当前对话（用户消息轮次）内全部允许
    """
    ResponseHeaders().apply(response)

    resolved = engine.consent_respond(body.request_id, body.decision)
    return {"resolved": resolved, "request_id": body.request_id, "decision": body.decision}


class CancelRequest(BaseModel):
    """取消流式对话时提交的信息。stream_id 由 stream_start 事件携带。"""
    stream_id: str = Field(..., description="要取消的流式会话 ID（由 stream_start 事件返回）")


@router.post("/cancel")
async def cancel_stream(
    body: CancelRequest,
    response: Response,
    current_user: dict = Depends(get_current_user),
    engine = Depends(get_hermes_engine),
):
    """终止指定 stream_id 的流式对话，丢弃本轮未保存的内容。

    前端在用户点击"停止"按钮时调用此接口，传入 stream_start 事件返回的 stream_id。
    同一用户多终端同时对话时，取消操作仅影响对应的那一个流。
    服务端设置取消信号 → stream 生成器在下一个检查点停止 → 本轮不写入存储层。
    """
    ResponseHeaders().apply(response)
    cancelled = engine.request_cancel(body.stream_id)
    return {
        "cancelled":  cancelled,
        "stream_id":  body.stream_id,
        "message":    "取消信号已发送" if cancelled else "未找到对应的活跃流式对话",
    }


@router.get("/history")
async def get_chat_history(
    request: Request,
    response: Response,
    current_user: dict = Depends(get_current_user),
    req_headers: RequestHeaders = Depends(RequestHeaders),
    size: int = Query(default=20, ge=1, le=100, description="返回条数"),
    page: int = Query(default=1, ge=1, description="页码（从 1 开始）"),
    all_clients: bool = Query(default=False, description="是否返回所有客户端历史"),
):
    """
    查询当前用户的聊天历史记录，从 Elasticsearch 分页返回。
    all_clients=false（默认）时只返回当前客户端的历史；all_clients=true 返回全部。
    """
    ResponseHeaders().apply(response)
    user_id = current_user["user_id"]
    engine = getattr(request.app.state, "hermes_engine", None)
    if not engine or not hasattr(engine, "chat_history"):
        raise HTTPException(status_code=500, detail="Chat history service unavailable")

    client_type_filter: Optional[str] = None if all_clients else req_headers.client_type

    try:
        from_ = (page - 1) * size
        turns = await engine.chat_history.get_recent_turns(
            user_id, size=size, from_=from_, client_type=client_type_filter
        )
        total = await engine.chat_history.count_index_docs(user_id, client_type=client_type_filter)
        return {
            "user_id": user_id,
            "page": page,
            "size": size,
            "total": total,
            "all_clients": all_clients,
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
    response:     Response,
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
    ResponseHeaders().apply(response)
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
            # 校验 agent_name 是否当前用户可用
            if agent_name:
                available = {ag.name for ag in agent_registry.list_available_for_user(user_id)}
                if agent_name not in available:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Agent '{agent_name}' 不存在或当前用户无权限使用",
                    )

            # 将沙箱摘要注入 context，让 Hermes 感知已处理的内容
            sandbox_summary = _build_sandbox_summary(processed, text_result)
            extra_context   = {"sandbox_results": sandbox_summary}

            try:
                llm_instance = await engine._build_llm_from_config(user_model)
            except Exception:
                llm_instance = None

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
    req:      RevectorizeRequest,
    request:  Request,
    response: Response,
    _:        Optional[dict] = Depends(require_local_or_auth),
):
    """重新向量化聊天历史（本机无鉴权，远程需 Token）。"""
    ResponseHeaders().apply(response)
    rag_pipeline = getattr(request.app.state, "rag_pipeline", None)
    if not rag_pipeline:
        raise HTTPException(status_code=503, detail="RagPipeline 未初始化")

    embedding_service = getattr(request.app.state, "embedding_service", None)
    if not embedding_service or not embedding_service.enabled:
        raise HTTPException(status_code=503, detail="Embedding 服务未启用")

    asyncio.create_task(
        rag_pipeline.revectorize(user_id=req.user_id, date_str=req.date)
    )

    return {
        "status":  "started",
        "user_id": req.user_id or "*（全部用户）",
        "date":    req.date    or "*（全部日期）",
        "message": "重向量化任务已在后台启动，请查看服务日志获取进度",
    }


def _build_sandbox_summary(processed: list, text_result: Optional[dict]) -> str:
    """
    把"沙箱安全检查"的结果整理成一段简洁的文字摘要，
    方便 AI 引擎了解本次上传了哪些内容、哪些通过了检查、哪些被拒绝了。
    """
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
