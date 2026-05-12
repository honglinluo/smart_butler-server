"""
【模块说明】AI 模型管理 — 添加、切换、查看模型列表

这个文件负责管理用户可以使用的 AI 模型。用户可以：
  - 添加自己的 AI 模型（填写模型地址、API Key 和模型名称）
  - 切换当前使用的模型
  - 查看所有可用模型列表

【模型测试机制】
  添加新模型时，系统会先自动发一条"Hi"测试能否正常连接，
  连不上或模型响应异常则拒绝保存，避免配置了一个用不了的模型。

【系统默认模型】
  user_id 为 '0' 的模型是系统预置的默认模型，所有用户都可以切换使用，
  在模型列表中显示为"系统默认"标签。
"""


from typing import Optional, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, Response, status, Depends
from pydantic import BaseModel, Field, field_validator
from app.database.pool import get_connection, release_connection
from app.api.dependencies import get_current_user
from app.utils.headers import ResponseHeaders


router = APIRouter(prefix="/models", tags=["Models"])


def _assert_valid_url(url: str) -> str:
    """校验 URL 必须为合法的 http/https 地址，否则抛出 ValueError。"""
    if not url or not url.strip():
        raise ValueError("URL 不能为空")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"URL 必须以 http:// 或 https:// 开头，当前值: {url!r}")
    if not parsed.netloc:
        raise ValueError(f"URL 缺少主机地址，当前值: {url!r}")
    return url.strip()


class CreateModel(BaseModel):
    """
    添加新模型时提交的信息。
    url：模型服务地址（如 http://localhost:11434 或 https://api.openai.com）
    api_key：访问密钥（本地模型可留空）
    model_name：模型名称（如 qwen2.5、gpt-4o）
    model_type：模型用途 — text=纯文字对话 / image=图像生成 / multimodal=图文混合
    """
    url: str = Field(..., description="LLM API URL")
    api_key: str = Field(..., description="API Key")
    model_name: str = Field(..., description="Model name")
    model_type: Literal["text", "image", "multimodal"] = Field(..., description="Model type")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        try:
            return _assert_valid_url(v)
        except ValueError as e:
            raise ValueError(str(e)) from e


class UpdateModel(BaseModel):
    """修改已有模型信息时提交的数据，所有字段均可选。"""
    url: Optional[str] = Field(None, description="LLM API URL")
    api_key: Optional[str] = Field(None, description="API Key")
    model_name: Optional[str] = Field(None, description="Model name")
    model_type: Optional[Literal["text", "image", "multimodal"]] = Field(None, description="Model type")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        try:
            return _assert_valid_url(v)
        except ValueError as e:
            raise ValueError(str(e)) from e


class ChangeModel(BaseModel):
    """切换当前模型时提交的信息，只需要提供目标模型的数字 ID。"""
    model_id: int = Field(..., description="Model ID to switch to")


async def _test_model(url: str, api_key: str, model_name: str) -> tuple[bool, str]:
    """
    向模型发送一条简短测试消息（"Hi"），检验模型地址和 API Key 是否有效。
    返回 (True, "") 表示测试通过；(False, 错误描述) 表示连接失败或响应异常。
    超时时间为 15 秒。
    """
    import httpx

    base_url = url.rstrip("/")
    # 兼容 /v1 结尾和不带 /v1 的 URL
    if not base_url.endswith("/v1"):
        endpoint = f"{base_url}/v1/chat/completions"
    else:
        endpoint = f"{base_url}/chat/completions"

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 8,
        "temperature": 0,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            # 只要返回 choices 列表即视为成功
            if data.get("choices"):
                return True, ""
            return False, f"模型响应格式异常: {resp.text[:200]}"
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except httpx.TimeoutException:
        return False, "连接超时，请检查模型 URL 是否可访问"
    except Exception as e:
        return False, str(e)


@router.post("/create", response_model=dict)
async def create_model(
    model_data: CreateModel,
    response: Response,
    current_user: dict = Depends(get_current_user)
):
    """
    添加新模型。创建前会自动测试模型是否可用，测试失败则不保存。
    新添加的模型默认状态为"激活（state=1）"，即立即可用。
    """
    ResponseHeaders().apply(response)
    # 仅对 text/multimodal 类型做接口连通性测试
    if model_data.model_type in ("text", "multimodal"):
        ok, err = await _test_model(model_data.url, model_data.api_key, model_data.model_name)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"模型测试失败，请检查配置：{err}",
            )

    mysql_conn = await get_connection("mysql", "agent_db")
    try:
        sql = """
        INSERT INTO llms (url, api_key, user_id, model_name, model_type, state)
        VALUES (:url, :api_key, :user_id, :model_name, :model_type, 1)
        """
        await mysql_conn.execute_raw(sql, {
            "url": model_data.url,
            "api_key": model_data.api_key,
            "user_id": current_user["user_id"],
            "model_name": model_data.model_name,
            "model_type": model_data.model_type
        })

        return {"message": "Model created and tested successfully"}

    finally:
        await release_connection("mysql", mysql_conn)


@router.put("/update/{model_id}", response_model=dict)
async def update_model(
    model_id: int,
    model_data: UpdateModel,
    response: Response,
    current_user: dict = Depends(get_current_user)
):
    """
    修改已有模型信息。仅允许修改自己的模型。
    若修改了 url / api_key / model_name，会先在沙箱中测试连通性，失败则不保存。
    """
    ResponseHeaders().apply(response)

    mysql_conn = await get_connection("mysql", "agent_db")
    try:
        # 校验模型归属
        df = await mysql_conn.execute_raw(
            "SELECT id, url, api_key, model_name, model_type FROM llms "
            "WHERE id = :mid AND user_id = :uid AND is_deleted = 0",
            {"mid": model_id, "uid": current_user["user_id"]},
        )
        if df is None or len(df) == 0:
            raise HTTPException(status_code=404, detail="Model not found or not owned by user")

        existing = df.iloc[0]
        new_url        = model_data.url        or str(existing["url"])
        new_api_key    = model_data.api_key    if model_data.api_key is not None else str(existing["api_key"])
        new_model_name = model_data.model_name or str(existing["model_name"])
        new_model_type = model_data.model_type or str(existing["model_type"])

        # 只要涉及连接参数变更，先沙箱测试
        conn_changed = (
            model_data.url is not None
            or model_data.api_key is not None
            or model_data.model_name is not None
        )
        if conn_changed and new_model_type in ("text", "multimodal"):
            ok, err = await _test_model(new_url, new_api_key, new_model_name)
            if not ok:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"模型测试失败，请检查配置：{err}",
                )

        # 构建动态 UPDATE
        set_parts = []
        params: dict = {"mid": model_id}
        if model_data.url is not None:
            set_parts.append("url = :url")
            params["url"] = new_url
        if model_data.api_key is not None:
            set_parts.append("api_key = :api_key")
            params["api_key"] = new_api_key
        if model_data.model_name is not None:
            set_parts.append("model_name = :model_name")
            params["model_name"] = new_model_name
        if model_data.model_type is not None:
            set_parts.append("model_type = :model_type")
            params["model_type"] = new_model_type

        if not set_parts:
            return {"message": "No fields to update"}

        await mysql_conn.execute_raw(
            f"UPDATE llms SET {', '.join(set_parts)} WHERE id = :mid",
            params,
        )
        return {"message": "Model updated successfully"}

    finally:
        await release_connection("mysql", mysql_conn)


@router.post("/change", response_model=dict)
async def change_model(
    request: Request,
    response: Response,
    change_data: ChangeModel,
    current_user: dict = Depends(get_current_user)
):
    """
    切换当前使用的模型。
    会先把该用户所有自有模型设为"未激活"，再把目标模型设为"激活"，
    并立即清除引擎缓存，确保下一条消息就用新模型回答。
    只能切换自己的模型或系统默认模型，不能使用其他人的私有模型。
    """
    ResponseHeaders().apply(response)
    mysql_conn = await get_connection("mysql", "agent_db")
    try:
        # 允许选择当前用户自己的模型，或系统模型（user_id='0'），拒绝其他用户的私有模型
        check_sql = """
            SELECT id, user_id FROM llms
            WHERE id = :model_id AND is_deleted = 0
              AND (user_id = :user_id OR user_id = '0')
        """
        df = await mysql_conn.execute_raw(check_sql, {
            "model_id": change_data.model_id,
            "user_id": current_user["user_id"]
        })
        if df is None or len(df) == 0:
            raise HTTPException(status_code=404, detail="Model not found or not owned by user")

        # 更新 users.current_llm_id，记录用户当前选择的模型
        await mysql_conn.execute_raw(
            "UPDATE users SET current_llm_id = :model_id WHERE user_id = :user_id",
            {"model_id": change_data.model_id, "user_id": current_user["user_id"]}
        )

        hermes_engine = getattr(request.app.state, "hermes_engine", None)
        if hermes_engine:
            hermes_engine.clear_llm_cache(current_user["user_id"])

        return {"message": "Model changed successfully"}

    finally:
        await release_connection("mysql", mysql_conn)


@router.get("/list", response_model=list)
async def list_models(response: Response, current_user: dict = Depends(get_current_user)):
    """
    查询所有可用模型列表：
      - 当前用户自己添加的模型（is_system_default=False）
      - 系统预置的默认模型（is_system_default=True，所有用户共用）
    state=1 表示当前正在使用，state=0 表示未激活。
    """
    ResponseHeaders().apply(response)
    mysql_conn = await get_connection("mysql", "agent_db")
    try:
        # 获取用户当前选择的模型 ID
        uid_df = await mysql_conn.execute_raw(
            "SELECT current_llm_id FROM users WHERE user_id = :uid LIMIT 1",
            {"uid": current_user["user_id"]},
        )
        current_llm_id = None
        if uid_df is not None and len(uid_df) > 0:
            val = uid_df.iloc[0].get("current_llm_id")
            current_llm_id = int(val) if val is not None else None

        user_sql = """
        SELECT id, url, model_name, model_type, state, created_at
        FROM llms
        WHERE user_id = :user_id AND is_deleted = 0
        ORDER BY created_at DESC
        """
        user_df = await mysql_conn.execute_raw(user_sql, {"user_id": current_user["user_id"]})

        # 系统默认模型（user_id = '0'，排除 embedding 类型）
        sys_sql = """
        SELECT id, url, model_name, model_type, state, created_at
        FROM llms
        WHERE user_id = '0' AND is_deleted = 0 AND model_type != 'embedding'
        ORDER BY created_at DESC
        """
        sys_df = await mysql_conn.execute_raw(sys_sql, {})

        models = []

        if user_df is not None and len(user_df) > 0:
            for _, row in user_df.iterrows():
                models.append({
                    "id": row["id"],
                    "url": row["url"],
                    "model_name": row["model_name"],
                    "model_type": row["model_type"],
                    "state": 1 if current_llm_id is not None and int(row["id"]) == current_llm_id else 0,
                    "created_at": row["created_at"],
                    "is_system_default": False,
                })

        if sys_df is not None and len(sys_df) > 0:
            for _, row in sys_df.iterrows():
                # 系统默认模型：若用户未选任何模型（current_llm_id=NULL），则标记第一条为激活
                is_active = current_llm_id is not None and int(row["id"]) == current_llm_id
                models.append({
                    "id": row["id"],
                    "url": row["url"],
                    "model_name": row["model_name"],
                    "model_type": row["model_type"],
                    "state": 1 if is_active else 0,
                    "created_at": row["created_at"],
                    "is_system_default": True,
                })

        return models

    finally:
        await release_connection("mysql", mysql_conn)
