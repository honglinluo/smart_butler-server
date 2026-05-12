"""
内置工具包 — 自动发现并注册 builtin 目录下的所有工具。

新增内置工具时只需在本目录下新建 Python 文件并在其中调用 registry.register()，
无需修改本文件，启动时导入此包即可自动完成注册。
"""

import importlib
import pkgutil
import logging

_logger = logging.getLogger(__name__)

for _importer, _modname, _ispkg in pkgutil.iter_modules(__path__):
    try:
        importlib.import_module(f"app.tools.builtin.{_modname}")
    except Exception as _e:
        _logger.warning("加载内置工具模块 '%s' 失败: %s", _modname, _e)
