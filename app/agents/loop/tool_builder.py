"""Agent 事件循环 — 工具构建器

通过 LLM 生成专用工具代码，写入 app/tools/dynamic/ 目录，并做语法验证。
生成的工具遵循统一规范：
  - 函数名: tool_{tool_name}
  - 返回值: {"success": bool, "result": Any, "error": str}
  - 只使用 Python 标准库（安全约束）
"""
from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import List

from app.agents.loop.events import BuiltTool, ToolCodeRequest
from app.core.paths import PROJECT_ROOT

logger = logging.getLogger("agent_loop.tool_builder")

_DYNAMIC_DIR = PROJECT_ROOT / "app" / "tools" / "dynamic"

_SYS_PROMPT = (
    "你是专业 Python 工具开发者。"
    "只输出 Python 代码，不输出任何解释文字。"
    "代码用 ```python ... ``` 包裹。"
)

_BUILD_PROMPT = """\
根据以下需求，编写一个 Python 工具函数。

## 工具信息
- 名称：{tool_name}
- 描述：{description}
- 功能需求：{requirements}

## 输入参数
{input_params}

## 输出格式
{output_format}

## 参考示例代码
{example_code}

## 开发约束
1. 函数名必须为：`{function_name}`
2. 添加完整类型注解和单行 docstring
3. 只能使用 Python 标准库：os, json, re, datetime, pathlib, urllib, \
http.client, math, hashlib, base64, collections, itertools
4. 可以是 `async def` 或普通 `def`
5. **必须**返回固定结构：`{{"success": bool, "result": Any, "error": str}}`
   - 成功时 success=True, error=""
   - 异常时 success=False, result=None, error=<错误信息>
6. 函数体顶层用 try/except 捕获所有异常
7. 文件顶部加必要的 import 语句

直接输出代码："""


class ToolBuilder:
    """调用 LLM 生成工具代码，语法检查后写入 dynamic/ 目录。"""

    def __init__(self) -> None:
        _DYNAMIC_DIR.mkdir(parents=True, exist_ok=True)
        init_f = _DYNAMIC_DIR / "__init__.py"
        if not init_f.exists():
            init_f.write_text("# 自动生成的动态工具模块目录\n", encoding="utf-8")

    async def build(self, request: ToolCodeRequest, llm) -> BuiltTool:
        """根据 ToolCodeRequest 生成并写入工具文件。"""
        function_name = f"tool_{request.tool_name}"

        params_str = self._format_params(request.input_params)
        prompt = _BUILD_PROMPT.format(
            tool_name    =request.tool_name,
            description  =request.description,
            requirements =request.requirements,
            input_params =params_str,
            output_format=request.output_format or "dict: {success, result, error}",
            example_code =request.example_code or "（无参考示例）",
            function_name=function_name,
        )

        logger.info("[ToolBuilder] 开始构建工具: %s（请求来自 %s）",
                    request.tool_name, request.requested_by)
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            resp = await llm.ainvoke([
                SystemMessage(content=_SYS_PROMPT),
                HumanMessage(content=prompt),
            ])
            raw_text = resp.content if hasattr(resp, "content") else str(resp)
            code = self._extract_code(raw_text)

            if not code:
                return self._fail(request, function_name, "LLM 未生成有效 Python 代码")

            # 语法校验
            try:
                ast.parse(code)
            except SyntaxError as exc:
                return self._fail(request, function_name, f"代码语法错误: {exc}")

            # 写文件
            out_path = _DYNAMIC_DIR / f"{request.tool_name}.py"
            out_path.write_text(code, encoding="utf-8")
            module_path = f"app.tools.dynamic.{request.tool_name}"

            logger.info("[ToolBuilder] 工具写入完成: %s", out_path)
            return BuiltTool(
                tool_name    =request.tool_name,
                module_path  =module_path,
                function_name=function_name,
                description  =request.description,
                file_path    =str(out_path),
                success      =True,
            )

        except Exception as exc:
            logger.error("[ToolBuilder] 构建工具异常 %s: %s", request.tool_name, exc)
            return self._fail(request, function_name, str(exc))

    # ── 内部 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _format_params(params: List) -> str:
        if not params:
            return "  （无参数）"
        lines = []
        for p in params:
            name = p.get("name", "?")
            typ  = p.get("type", "Any")
            desc = p.get("desc", "")
            lines.append(f"  - {name} ({typ}): {desc}")
        return "\n".join(lines)

    @staticmethod
    def _extract_code(text: str) -> str:
        m = re.search(r"```python\s*([\s\S]*?)\s*```", text)
        if m:
            return m.group(1).strip()
        # 兜底：整段文本看起来像 Python
        t = text.strip()
        if t.startswith(("def ", "async def ", "import ", "from ")):
            return t
        return ""

    @staticmethod
    def _fail(req: ToolCodeRequest, fn: str, err: str) -> BuiltTool:
        return BuiltTool(
            tool_name    =req.tool_name,
            module_path  ="",
            function_name=fn,
            description  =req.description,
            file_path    ="",
            success      =False,
            error        =err,
        )
