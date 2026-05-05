"""项目根目录路径常量 — 统一来源，避免各模块重复推导 parent 链。

入口文件（main.py / create_tables.py / scripts / tests）在最顶部设置
``PROJECT_ROOT`` 环境变量；所有内部模块从此处导入。
未设置环境变量时兜底使用 __file__ 计算（适合单独运行子模块的场景）。
"""
import os
from pathlib import Path

PROJECT_ROOT: Path = (
    Path(os.environ["PROJECT_ROOT"])
    if "PROJECT_ROOT" in os.environ
    else Path(__file__).parent.parent.parent
)
