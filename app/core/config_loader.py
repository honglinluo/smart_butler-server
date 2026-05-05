"""配置加载模块 - 从 YAML 文件加载系统配置和代理配置"""

import yaml
from pathlib import Path
from typing import Dict, Any, Optional


class ConfigLoader:
    """配置加载器，支持热加载配置文件"""
    
    def __init__(self, config_dir: str = "config"):
        """
        初始化配置加载器
        
        Args:
            config_dir: 配置文件所在目录
        """
        self.config_dir = Path(config_dir)
        self._system_config: Optional[Dict[str, Any]] = None
        self._agents_config: Optional[Dict[str, Any]] = None
    
    def load_system_config(self) -> Dict[str, Any]:
        """加载系统配置文件"""
        config_path = self.config_dir / "system_config.yaml"
        
        if not config_path.exists():
            raise FileNotFoundError(f"系统配置文件不存在: {config_path}")
        
        with open(config_path, "r", encoding="utf-8") as f:
            self._system_config = yaml.safe_load(f) or {}
        
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
        """获取系统配置"""
        if self._system_config is None:
            self.load_system_config()
        return self._system_config
    
    def get_agents_config(self) -> Dict[str, Any]:
        """获取代理配置"""
        if self._agents_config is None:
            self.load_agents_config()
        return self._agents_config
