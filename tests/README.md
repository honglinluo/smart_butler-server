# tests — 测试文件说明

所有测试文件统一管理在 `tests/` 目录下。

## 文件结构

```text
tests/
├── test_basic.py          # 基础功能测试（无需数据库）
├── test_llm_integration.py # LLM 集成测试
├── test_full_workflow.py   # 完整工作流测试（需全服务）
├── test_api.py             # API 端点测试
├── run_demo.py             # 系统演示脚本
├── debug_row_format.py     # 数据格式调试工具
└── README.md               # 此文件
```

## 运行测试

```bash
# 无数据库基础测试（推荐先跑）
python tests/test_basic.py

# 完整工作流（需 MySQL + Redis + ES）
python tests/test_full_workflow.py

# LLM 集成测试
python tests/test_llm_integration.py

# API 端点测试（需服务已启动）
python tests/test_api.py

# 系统演示
python tests/run_demo.py
```

## 新增测试文件规范

```python
#!/usr/bin/env python3
import os
import sys
from pathlib import Path

# 设置项目根路径（优先使用环境变量）
os.environ.setdefault("PROJECT_ROOT", str(Path(__file__).parent.parent))
sys.path.insert(0, os.environ["PROJECT_ROOT"])

import asyncio
from app.core.hermes_engine import HermesEngine

async def main():
    pass

if __name__ == "__main__":
    asyncio.run(main())
```

## 配置要求

- MySQL: `localhost`，数据库 `agent_db`，用户 `root`
- Redis: `localhost:6379`
- Elasticsearch: `localhost:9200`
- LLM 配置需在 MySQL `llms` 表中存在
