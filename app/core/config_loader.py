"""
【模块说明】配置加载器 — 读取 YAML 配置文件并自动替换敏感信息

系统的各种配置（数据库地址、API Key 等）存放在 config/system_config.yaml 中。
但密码、密钥这类敏感信息不能直接写在文件里（防止泄露），
所以配置文件里用 ${变量名} 占位符代替，实际值存在 .env 文件或系统环境变量里。

本模块负责：
  1. 读取 YAML 配置文件
  2. 把配置里的 ${MYSQL_URL}、${REDIS_URL} 等占位符替换为真实的环境变量值
  3. 提供统一接口让其他模块获取配置

示例：
  YAML 里写：url: "${MYSQL_URL}"
  .env  里写：MYSQL_URL=mysql+pymysql://root:password@localhost/agent_db
  加载后得到：url: "mysql+pymysql://root:password@localhost/agent_db"
"""


import os
import re
import yaml
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv


class ConfigLoader:
    """
    配置加载器。
    读取 config/ 目录下的 YAML 文件，并自动把 ${变量名} 替换为环境变量的实际值。
    同时支持加载项目根目录的 .env 文件（不会覆盖系统已有的环境变量）。
    """

    def __init__(self, config_dir: str = "config"):
        self.config_dir = Path(config_dir)
        self._system_config: Optional[Dict[str, Any]] = None
        self._agents_config: Optional[Dict[str, Any]] = None

        # 加载 .env 文件（不覆盖已存在的系统环境变量）
        project_root = Path(os.environ.get("PROJECT_ROOT", self.config_dir.parent.resolve()))
        env_path = project_root / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)

    # ── 环境变量替换 ─────────────────────────────────────────────

    def _resolve_env_vars(self, obj: Any) -> Any:
        """递归将配置中的 ${VAR_NAME} 替换为对应的环境变量值。"""
        if isinstance(obj, str):
            def _replace(match: re.Match) -> str:
                var_name = match.group(1)
                value = os.environ.get(var_name)
                if value is None:
                    raise EnvironmentError(
                        f"配置中引用了未设置的环境变量: ${{{var_name}}}，"
                        f"请在 .env 文件或系统环境中设置该变量。"
                    )
                return value
            return re.sub(r'\$\{([^}]+)\}', _replace, obj)
        elif isinstance(obj, dict):
            return {k: self._resolve_env_vars(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._resolve_env_vars(item) for item in obj]
        return obj

    # ── 配置加载 ─────────────────────────────────────────────────

    def load_system_config(self) -> Dict[str, Any]:
        """加载系统配置文件，并解析其中的环境变量占位符"""
        config_path = self.config_dir / "system_config.yaml"

        if not config_path.exists():
            raise FileNotFoundError(f"系统配置文件不存在: {config_path}")

        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        self._system_config = self._resolve_env_vars(raw)
        return self._system_config

    def load_agents_config(self) -> Dict[str, Any]:
        """加载代理配置文件"""
        config_path = self.config_dir / "agents_config.yaml"

        if not config_path.exists():
            raise FileNotFoundError(f"代理配置文件不存在: {config_path}")

        with open(config_path, "r", encoding="utf-8") as f:
            self._agents_config = yaml.safe_load(f) or {}

        return self._agents_config

    def get_system_config(self) -> Dict[str, Any]:
        if self._system_config is None:
            self.load_system_config()
        return self._system_config

    def get_agents_config(self) -> Dict[str, Any]:
        if self._agents_config is None:
            self.load_agents_config()
        return self._agents_config
