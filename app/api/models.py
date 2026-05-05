"""模型管理 API - 创建、更改模型"""

from typing import Optional, Literal
from fastapi import APIRouter, HTTPException, Request, status, Depends
from pydantic import BaseModel, Field
from app.database.pool import get_connection, release_connection
from app.api.dependencies import get_current_user


router = APIRouter(prefix="/models", tags=["Models"])


class CreateModel(BaseModel):
    url: str = Field(..., description="LLM API URL")
    api_key: str = Field(..., description="API Key")
    model_name: str = Field(..., description="Model name")
    model_type: Literal["text", "image", "multimodal"] = Field(..., description="Model type")


class ChangeModel(BaseModel):
    model_id: int = Field(..., description="Model ID to switch to")


async def _test_model(url: str, api_key: str, model_name: str) -> tuple[bool, str]:
    """向模型发送一条测试消息，验证配置可用性。返回 (成功, 错误信息)。"""
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
    current_user: dict = Depends(get_current_user)
):
    """创建新模型（创建前自动发送测试消息验证配置）"""
    # 仅对 text/multimodal 类型做接口连通性测试
    if model_data.model_type in ("text", "multimodal"):
        ok, err = await _test_model(model_data.url, model_data.api_key, model_data.model_name)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"模型测试失败，请检查配置：{err}",
            )

    mysql_conn = await get_connection("mysql", "agent_db")
    if not mysql_conn:
        raise HTTPException(status_code=500, detail="Database connection failed")

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


@router.post("/change", response_model=dict)
async def change_model(
    request: Request,
    change_data: ChangeModel,
    current_user: dict = Depends(get_current_user)
):
    """更改当前使用的模型"""
    mysql_conn = await get_connection("mysql", "agent_db")
    if not mysql_conn:
        raise HTTPException(status_code=500, detail="Database connection failed")

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

        is_system_model = str(df.iloc[0]["user_id"]) == "0"

        # 先将当前用户的所有自有模型置为非激活
        await mysql_conn.execute_raw(
            "UPDATE llms SET state = 0 WHERE user_id = :user_id",
            {"user_id": current_user["user_id"]}
        )

        if is_system_model:
            # 切换系统模型时：重置所有系统模型，再激活目标模型
            # 引擎加载时若用户无激活自有模型则回落到 user_id='0' state=1 的模型
            await mysql_conn.execute_raw(
                "UPDATE llms SET state = 0 WHERE user_id = '0'",
                {}
            )

        await mysql_conn.execute_raw(
            "UPDATE llms SET state = 1 WHERE id = :model_id",
            {"model_id": change_data.model_id}
        )

        hermes_engine = getattr(request.app.state, "hermes_engine", None)
        if hermes_engine:
            hermes_engine.clear_llm_cache(current_user["user_id"])

        return {"message": "Model changed successfully"}

    finally:
        await release_connection("mysql", mysql_conn)


@router.get("/list", response_model=list)
async def list_models(current_user: dict = Depends(get_current_user)):
    """列出当前用户的所有模型，同时返回系统内置默认模型（标记 is_system_default=True）"""
    mysql_conn = await get_connection("mysql", "agent_db")
    if not mysql_conn:
        raise HTTPException(status_code=500, detail="Database connection failed")

    try:
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
                    "state": row["state"],
                    "created_at": row["created_at"],
                    "is_system_default": False,
                })

        if sys_df is not None and len(sys_df) > 0:
            for _, row in sys_df.iterrows():
                models.append({
                    "id": row["id"],
                    "url": row["url"],
                    "model_name": row["model_name"],
                    "model_type": row["model_type"],
                    "state": row["state"],
                    "created_at": row["created_at"],
                    "is_system_default": True,
                })

        return models

    finally:
        await release_connection("mysql", mysql_conn)
