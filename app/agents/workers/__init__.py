"""
工作智能体模块 — 自动发现并导出 workers 目录下所有继承 BaseAgent 的智能体类。

新增 Agent 时只需在本目录下新建 Python 文件并继承 BaseAgent，
无需修改本文件或 main.py，启动时会自动注册。
"""

import importlib
import inspect
import pkgutil
from typing import List, Type

from app.agents.base import BaseAgent

__all__: List[str] = []

for _importer, _modname, _ispkg in pkgutil.iter_modules(__path__):
    try:
        _mod = importlib.import_module(f"app.agents.workers.{_modname}")
        for _clsname, _cls in inspect.getmembers(_mod, inspect.isclass):
            if (
                issubclass(_cls, BaseAgent)
                and _cls is not BaseAgent
                and _cls.__module__ == _mod.__name__
                and _clsname not in __all__
            ):
                globals()[_clsname] = _cls
                __all__.append(_clsname)
    except Exception as _e:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "加载 Worker Agent 模块 '%s' 失败: %s", _modname, _e
        )
