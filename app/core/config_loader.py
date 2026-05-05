"""配置加载模块 - 从 YAML 文件加载系统配置，敏感字段通过环境变量注入"""

import os
import re
import yaml
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv


class ConfigLoader:
    """配置加载器，支持 ${ENV_VAR} 占位符替换和 .env 文件加载"""

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
