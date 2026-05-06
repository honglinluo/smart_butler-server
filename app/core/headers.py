"""HTTP 请求头与响应头管理。

将自定义请求头的读取、校验，以及统一响应头的注入逻辑集中于此，
避免散落在各 API 端点，方便后期统一扩展。

用法::

    # 请求头（FastAPI Depends）
    from app.core.headers import RequestHeaders, ResponseHeaders

    @router.post("/send")
    async def send_message(
        chat_data: ChatMessage,
        response: Response,
        req_headers: RequestHeaders = Depends(RequestHeaders),
    ):
        ResponseHeaders().apply(response)
        context.update(req_headers.to_context_dict())

    # 单个接口追加/覆盖响应头（链式调用）
    ResponseHeaders(extra={"Cache-Control": "no-cache"}).apply(response)
    ResponseHeaders().set("X-Custom", "value").apply(response)
"""

import logging
from typing import ClassVar

from fastapi import Request, Response

from app.core.client_env import normalize_client_type

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# 请求头
# ══════════════════════════════════════════════════════════════════════════════

class RequestHeaders:
    """从 FastAPI Request 中提取并校验自定义请求头。

    可直接作为 FastAPI 依赖（``Depends(RequestHeaders)``）注入端点；
    FastAPI 会自动将当前 ``Request`` 传入构造函数。

    扩展指南
    --------
    新增请求头时，在类中添加头名称常量和对应实例属性::

        _H_NEW_FIELD = "X-New-Field"

        def __init__(self, request: Request) -> None:
            ...
            self.new_field: str = request.headers.get(self._H_NEW_FIELD, "")
    """

    _H_CLIENT_TYPE    = "X-Client-Type"
    _H_CLIENT_VERSION = "X-Client-Version"

    def __init__(self, request: Request) -> None:
        raw_type = request.headers.get(self._H_CLIENT_TYPE, "api")
        self.client_type:    str = normalize_client_type(raw_type)
        self.client_version: str = request.headers.get(self._H_CLIENT_VERSION, "")

        logger.debug(
            "RequestHeaders: client_type=%s client_version=%s",
            self.client_type, self.client_version or "(none)",
        )

    def to_context_dict(self) -> dict:
        """返回可直接 update 进 context 的字典。

        键名以 ``_`` 开头，与用户自定义 context 键隔离::

            context.update(req_headers.to_context_dict())
            # → context["_client_type"]    = "wechat"
            # → context["_client_version"] = "8.0.50"
        """
        return {
            "_client_type":    self.client_type,
            "_client_version": self.client_version,
        }

    def __repr__(self) -> str:
        return (
            f"RequestHeaders(client_type={self.client_type!r}, "
            f"client_version={self.client_version!r})"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 响应头
# ══════════════════════════════════════════════════════════════════════════════

class ResponseHeaders:
    """统一 API 响应头管理。

    默认注入安全与缓存相关 HTTP 响应头；各接口可通过构造参数或链式方法
    新增或覆盖任意头，实现细粒度控制。

    用法::

        # 1. 所有接口通用（使用默认头）
        @router.get("/list")
        async def list_items(response: Response):
            ResponseHeaders().apply(response)
            ...

        # 2. 单个接口追加/覆盖（构造时传入 extra）
        ResponseHeaders(extra={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}).apply(response)

        # 3. 链式调用
        ResponseHeaders().set("X-Request-ID", req_id).remove("X-Frame-Options").apply(response)

        # 4. SSE / StreamingResponse（直接获取 dict）
        StreamingResponse(..., headers=ResponseHeaders(extra={"Cache-Control": "no-cache"}).as_dict())
    """

    _DEFAULTS: ClassVar[dict[str, str]] = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options":        "DENY",
        "X-XSS-Protection":       "1; mode=block",
        "Referrer-Policy":        "strict-origin-when-cross-origin",
        "Cache-Control":          "no-store",
    }

    def __init__(self, extra: dict[str, str] | None = None) -> None:
        self._headers: dict[str, str] = dict(self._DEFAULTS)
        if extra:
            self._headers.update(extra)

    def set(self, name: str, value: str) -> "ResponseHeaders":
        """新增或修改单个响应头，返回 self 支持链式调用。"""
        self._headers[name] = value
        return self

    def remove(self, name: str) -> "ResponseHeaders":
        """移除某个响应头（若存在），返回 self 支持链式调用。"""
        self._headers.pop(name, None)
        return self

    def apply(self, response: Response) -> None:
        """将所有响应头写入 FastAPI Response 对象。"""
        for name, value in self._headers.items():
            response.headers[name] = value

    def as_dict(self) -> dict[str, str]:
        """返回当前响应头字典副本（供 StreamingResponse 等直接传 headers= 参数使用）。"""
        return dict(self._headers)
