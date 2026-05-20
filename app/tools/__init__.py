"""
【模块说明】工具系统公共接口（Tools）— 对外暴露工具系统的所有核心组件

这个包是工具系统的统一入口，外部代码通过这里导入所需的工具相关组件：
  BaseTool             — 所有工具的基类（内置工具和用户工具都继承它）
  @tool 装饰器          — 声明式注册工具
  registry             — 全局工具注册表（查找和管理所有已注册工具）
  consent_manager      — 危险操作授权管理器
  ToolLoader           — 动态工具加载器（支持运行时创建新工具）

【关键常量】
  EXEC_SERVER / EXEC_CLIENT  — 工具执行位置（服务器端 / 客户端浏览器）
  VIS_PUBLIC / VIS_EXCLUSIVE — 工具可见性（所有人可用 / 仅创建者可用）
  SRC_CODE / SRC_USER / SRC_AGENT — 工具来源（内置代码 / 用户创建 / AI 创建）
  CONSENT_ONCE ... CONSENT_ALWAYS — 授权有效期级别

工具系统公共接口。
"""

from app.tools.base import (
    BaseTool,
    ClientExecRequest,
    ConsentRequiredException,
    EXEC_SERVER,
    EXEC_CLIENT,
    VIS_PUBLIC,
    VIS_PRIVATE,
    VIS_EXCLUSIVE,
    SRC_CODE,
    SRC_USER,
    SRC_AGENT,
    CONSENT_ONCE,
    CONSENT_SESSION,
    CONSENT_PROJECT,
    CONSENT_ALWAYS,
    DANGEROUS_OPS,
    CRITICAL_OPS,
)
from app.tools.decorators import tool
from app.tools.registry import registry
from app.tools.permission import consent_manager
from app.tools.loader import ToolLoader

__all__ = [
    "BaseTool",
    "ClientExecRequest",
    "ConsentRequiredException",
    "tool",
    "registry",
    "consent_manager",
    "ToolLoader",
    "EXEC_SERVER", "EXEC_CLIENT",
    "VIS_PUBLIC", "VIS_PRIVATE", "VIS_EXCLUSIVE",
    "SRC_CODE", "SRC_USER", "SRC_AGENT",
    "CONSENT_ONCE", "CONSENT_SESSION", "CONSENT_PROJECT", "CONSENT_ALWAYS",
    "DANGEROUS_OPS",
    "CRITICAL_OPS",
]
