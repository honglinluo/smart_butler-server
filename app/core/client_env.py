"""客户端类型与运行环境定义。

在登录时记录，随聊天请求携带，存入记忆轮次，注入子 Agent 系统提示。
"""

from enum import Enum
from typing import Optional


class ClientType(str, Enum):
    API          = "api"            # 直接调用 API（无前端，默认值）
    WEB          = "web"            # 浏览器 Web 端
    WIN11        = "win11"          # Windows 11 桌面客户端
    WIN10        = "win10"          # Windows 10 桌面客户端
    WINDOWS      = "windows"        # Windows（未指定版本）
    MACOS        = "macos"          # macOS 桌面客户端
    IOS          = "ios"            # iPhone / iPad
    ANDROID      = "android"        # Android 手机/平板
    WECHAT       = "wechat"         # 微信小程序 / 公众号 H5
    WEWORK       = "wework"         # 企业微信
    DINGTALK     = "dingtalk"       # 钉钉
    LARK         = "lark"           # 飞书（Lark）
    HUAWEI       = "huawei"         # 华为鸿蒙 / 华为设备
    XIAOMI       = "xiaomi"         # 小米 MIUI / 小米设备
    LINK         = "link"           # 外部链接直接访问
    UNKNOWN      = "unknown"        # 未知或未上报


# 客户端中文可读名称（用于提示词注入）
_CLIENT_LABELS: dict[str, str] = {
    ClientType.API:      "API 直调",
    ClientType.WEB:      "Web 浏览器",
    ClientType.WIN11:    "Windows 11 桌面",
    ClientType.WIN10:    "Windows 10 桌面",
    ClientType.WINDOWS:  "Windows 桌面",
    ClientType.MACOS:    "macOS 桌面",
    ClientType.IOS:      "iOS 移动端",
    ClientType.ANDROID:  "Android 移动端",
    ClientType.WECHAT:   "微信",
    ClientType.WEWORK:   "企业微信",
    ClientType.DINGTALK: "钉钉",
    ClientType.LARK:     "飞书",
    ClientType.HUAWEI:   "华为设备",
    ClientType.XIAOMI:   "小米设备",
    ClientType.LINK:     "外部链接",
    ClientType.UNKNOWN:  "未知客户端",
}


def normalize_client_type(raw: Optional[str]) -> str:
    """将前端上报的字符串规范化为 ClientType 枚举值（字符串形式）。

    未识别的值统一返回 ClientType.UNKNOWN。
    """
    if not raw:
        return ClientType.UNKNOWN
    val = raw.strip().lower()
    # 别名映射
    _ALIASES: dict[str, str] = {
        "feishu": ClientType.LARK,
        "lark":   ClientType.LARK,
        "mac":    ClientType.MACOS,
        "macos":  ClientType.MACOS,
        "ios":    ClientType.IOS,
        "iphone": ClientType.IOS,
        "ipad":   ClientType.IOS,
    }
    if val in _ALIASES:
        return _ALIASES[val]
    try:
        return ClientType(val)
    except ValueError:
        return ClientType.UNKNOWN


def format_env_for_prompt(client_type: Optional[str], client_version: Optional[str] = None) -> str:
    """将客户端环境格式化为可注入系统提示的文本块。

    示例输出：
        <client-env>
        客户端类型: 微信
        客户端版本: 8.0.50
        </client-env>

    ClientType.UNKNOWN 或空值时返回空字符串（不注入）。
    """
    if not client_type or client_type == ClientType.UNKNOWN:
        return ""
    label   = _CLIENT_LABELS.get(client_type, client_type)
    version = f"\n客户端版本: {client_version}" if client_version else ""
    return f"<client-env>\n客户端类型: {label}{version}\n</client-env>"
