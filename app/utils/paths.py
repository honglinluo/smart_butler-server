"""
【模块说明】项目根目录路径常量

提供统一的 PROJECT_ROOT 常量，指向项目的根目录。
全项目所有模块都从这里导入路径，而不是各自去计算，避免在不同启动方式下路径出错。

使用方式：
  from app.utils.paths import PROJECT_ROOT
  config_file = PROJECT_ROOT / "config" / "system_config.yaml"

路径来源优先级：
  1. 环境变量 PROJECT_ROOT（推荐生产环境设置）
  2. 根据当前文件位置自动推算（开发/测试场景回退方案）
"""
import os
from pathlib import Path

PROJECT_ROOT: Path = (
    Path(os.environ["PROJECT_ROOT"])
    if "PROJECT_ROOT" in os.environ
    else Path(__file__).parent.parent.parent
)
