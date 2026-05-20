# Hermes Multi-Agent System

基于 LangChain + LangGraph 构建的企业级多智能体协作平台，支持多用户隔离、长期记忆管理、语义检索、动态 Agent 编排、定时任务调度与安全沙箱执行。

---

## 快速开始

### 1. 依赖安装

```bash
pip install -r requirements.txt
```

### 2. 配置文件

编辑 `config/system_config.yaml`（数据库地址、Embedding 配置、日志级别等）：

```yaml
logging:
  level: "DEBUG"   # DEBUG / INFO / WARNING / ERROR

database:
  mysql:
    url: "mysql+pymysql://root:password@localhost/agent_db"
  redis:
    url: "redis://localhost:6379/0"
  elasticsearch:
    url: "http://localhost:9200"

embedding:
  provider: "ollama"           # ollama / openai
  api_url: "http://localhost:11434"
  model_name: "bge-m3:latest"
  model_dim: 1024
```

编辑 `config/agents_config.yaml` 配置路由逻辑和工作 Agent。

### 3. 初始化数据库

```bash
python create_tables.py
```

### 4. 启动服务

```bash
# 开发模式（热重载）
uvicorn main:app --reload --port 8000

# 生产模式
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
```

### 5. 验证

```bash
# 健康检查
curl http://localhost:8000/health

# 查看 API 文档
open http://localhost:8000/docs
```

---

## 系统架构

```text
┌──────────────────────────────────────────────────────────┐
│            FastAPI 接入层 (main.py)                       │
│  /auth  /models  /chat  /agents  /tools  /scheduler      │
│  /decisions                                               │
└────────────────────────┬─────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────┐
│           Hermes 编排引擎 (HermesEngine)                  │
│  ┌──────────────┐   ┌──────────────────────┐             │
│  │ RouterAgent  │   │ ContextManager       │             │
│  │ 意图识别     │   │ 近期历史 + 记忆检索  │             │
│  │ 任务分解     │   │ 预取缓存             │             │
│  │ 流水线规划   │   └──────────────────────┘             │
│  └──────┬───────┘                                        │
│         │  single / serial / parallel                     │
│  ┌──────▼──────────────────────────────────────┐         │
│  │ AgentEventLoop（事件循环 + 工具构建门控）    │         │
│  │ registry → MySQL agents → YAML              │         │
│  │ LangGraph ReAct Agent + Tools + Sandbox      │         │
│  └─────────────────────────────────────────────┘         │
└────────────────────────┬─────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────┐
│                   数据存储层                               │
│  Redis  — L1 近期对话 / 预取缓存 / 委托链 / 决策门控      │
│  MySQL  — 用户/模型/Agent/技能/引用统计/调度任务          │
│  ES     — 聊天历史（全文）+ 向量索引（KNN）               │
└──────────────────────────────────────────────────────────┘
```

---

## API 总览

| 前缀 | 模块 | 说明 |
| --- | --- | --- |
| `/auth` | auth.py | 注册、登录、Token 校验、登出 |
| `/models` | models.py | LLM 模型配置 CRUD + 切换 |
| `/chat` | chat.py | 消息发送（同步/SSE流式）、历史查询、文件上传、重向量化、危险操作授权响应 |
| `/scoring` | scoring_api.py | Agent/Tool 评分查询、权重管理、统计重置 |
| `/agents` | agents_api.py | Agent CRUD + 评分 + 热重载 |
| `/tools` | tools_api.py | 工具查询与创建、危险操作类型开关管理 |
| `/scheduler` | scheduler_api.py | 定时任务管理 + 通知 SSE |
| `/decisions` | decision_api.py | 工具构建决策门控 + 策略配置 |

---

## 核心特性

### 多模式 Agent 编排

| 模式 | 说明 |
| --- | --- |
| `single` | 单 Agent 处理，直接返回 |
| `serial` | 串行流水线，前步结果传入后步，中途可校验结果质量 |
| `parallel` | 多 Agent 并行执行，asyncio.gather 合并结果 |

### Agent 事件循环（AgentEventLoop）

每个 Agent 执行内嵌一个事件循环，支持运行时动态构建工具：

1. Agent 调用 → 检测 `ToolCodeRequest` JSON 输出
2. 通过 `UserDecisionGate` 读取用户决策策略（`allow` / `ask` / `deny`）
3. `ask` 时挂起协程等待 `POST /decisions/{id}/resolve` 确认
4. Code Agent 生成工具代码 → 写入 MySQL → 热加载到 registry
5. 更新任务描述重新调用 Agent，最多 6 次迭代

### 三层记忆架构

| 层 | 存储 | 作用 |
| --- | --- | --- |
| L1 | Redis | 最近 10 轮对话快速加载 |
| L2 | MySQL | 引用计数，长期管理 |
| L3 | ES | 全文 + 向量语义检索 |

### 记忆预取

每轮回复结束后，后台异步以当前 `user_input` 为 query 预取下一轮可能用到的记忆，
写入 Redis（TTL 5 min）。下一轮 `ContextManager` 原子 `GETDEL` 消费，命中则跳过完整检索。
ES 未就绪时自动降级到 Redis 近期对话。

### 月度 / 年度归档

| 归档类型 | 触发时机 | 归档条件 | 摘要节数 |
| --- | --- | --- | --- |
| 月度归档 | 每月月底 UTC 23:00 | > 365 天的对话，按自然月 | 4 节 |
| 年度归档 | 每年 12 月 31 日 | > 3 年的月度摘要，按自然年 | 4 节 |

归档使用 Saga 三步模式：LLM 生成摘要 → ES checkpoint → 删除原始 turn，任意步失败可续接。

### 安全沙箱（Sandbox）

在隔离临时目录中运行用户/Agent 生成的代码：

- `executor.py` — subprocess 执行，强制超时，Linux 下 resource 限制 CPU/内存
- `scanner.py` — 静态扫描高危调用模式（`os.system`、`subprocess`、`eval` 等）
- 支持 Python（实际执行）/ Shell / Node.js（仅语法检查）

### 工具系统（Tools）

三种来源：`code`（开发者继承）/ `user`（用户 API 创建）/ `agent`（运行时动态生成）

可见性：`public`（全局）/ `private`（仅创建者）/ `exclusive`（仅归属 Agent）

`dangerous_ops` 声明危险操作类型，框架自动通过 `ConsentManager` 核查授权。

### 危险操作授权系统

工具声明 `dangerous_ops`（如 `["modify", "delete"]`）后，执行时自动触发授权流程：

- **SSE 流式场景**：通过 `asyncio.Future` 原地暂停工具执行，向前端推送 `consent_required` 事件，用户选择后调用 `POST /chat/consent` 恢复执行
- **三种决策**：允许（仅此次）/ 拒绝 / 当前对话允许（本轮 `turn_id` 全量放行）
- **并发串行化**：`_consent_lock` 确保并行 agent 同时触发时依次弹窗，获取锁后自动复查已授权状态避免重复弹窗
- **用户级开关**：`dangerous_op_configs` 表允许用户关闭特定操作类型的授权要求（关闭后自动放行）
- **授权优先级**：op_disabled > once > conversation（blanket） > session > project/always
- **性能**：`_is_op_enabled()` 结果按 60s TTL 缓存；设置页切换后立即失效

### 向量语义检索

- 支持 Ollama 本地（bge-m3 等）和 OpenAI 兼容在线服务
- 用户问题、模型回复、各 Agent 输出独立向量化存储
- 启动时自动检测 Embedding 模型变更，必要时重建全量向量索引
- 针对 Ollama bge-m3 NaN bug 实现三层容错

### 定时任务调度

支持 `once` / `daily` / `weekly` / `monthly` / `workday` / `weekend` / `cron` 七种类型。
时间存储 UTC，API 层接收 CST(UTC+8) 自动转换。接入中国法定节假日数据（含调休补班）。
SSE 接口实时推送任务完成通知。

---

## 目录结构

```text
smart_butler-server/
├── main.py                    # FastAPI 应用入口 + lifespan 初始化（10 步启动序列）
├── create_tables.py           # 数据库建表脚本
├── requirements.txt           # Python 依赖
├── config/
│   ├── system_config.yaml     # 系统配置（数据库、Embedding、日志、沙箱等）
│   ├── agents_config.yaml     # Agent/工具配置（路由、workers、工具）
│   └── templates/             # Prompt 模板目录
│       ├── *_system.txt       # HermesEngine 系统提示（含 LangChain 占位符）
│       ├── <agent_name>.txt   # BaseAgent 背景描述（纯文本）
│       └── summarizer_compress.txt / archiver_extract_tags.txt
├── app/
│   ├── api/                   # FastAPI 路由层
│   │   ├── auth.py            # 用户认证
│   │   ├── models.py          # LLM 模型配置
│   │   ├── chat.py            # 聊天接口（同步/SSE/历史/上传/重向量化）
│   │   ├── agents_api.py      # Agent 管理
│   │   ├── tools_api.py       # 工具管理
│   │   ├── scheduler_api.py   # 定时任务
│   │   ├── decision_api.py    # 工具构建决策门控
│   │   ├── scoring_api.py     # Agent/Tool 评分查询与管理
│   │   └── dependencies.py    # 依赖注入
│   ├── core/                  # 核心引擎
│   │   ├── hermes_engine.py   # 主编排引擎
│   │   ├── task_planner.py    # 两级任务规划（L1 Agent 级 / L2 步骤级）
│   │   ├── vector_store.py    # ES 向量索引
│   │   ├── file_storage.py    # 文件上传管理
│   │   └── config_loader.py   # YAML 配置加载（含环境变量占位符解析）
│   ├── memory/                # 记忆系统（抽象层 + 后端实现）
│   │   ├── base.py            # MemoryBackend / RagBackend ABC
│   │   ├── factory.py         # 根据 MEMORY_BACKEND 环境变量选择后端
│   │   └── backends/
│   │       ├── vectordb/      # Redis+MySQL+ES 后端（原 memory_manager）
│   │       └── filesystem/    # 纯文件系统后端（无外部依赖）
│   ├── scoring/               # Agent/Tool 评分系统
│   │   ├── models.py          # AgentStats / ToolStats / ScoreWeights 数据模型
│   │   ├── algorithm.py       # 成功率×延迟×质量×频率加权评分算法
│   │   ├── store.py           # 基于文件系统的评分持久化
│   │   └── manager.py         # ScoringManager 单例（内存缓冲 + 异步落盘）
│   ├── agents/                # Agent 体系
│   │   ├── base.py            # BaseAgent 基类 + 技能记忆系统
│   │   ├── router.py          # RouterAgent
│   │   ├── registry.py        # 全局注册表
│   │   ├── decorators.py      # @agent 装饰器
│   │   ├── loop/              # Agent 内部事件循环
│   │   │   ├── event_loop.py  # AgentEventLoop 主控
│   │   │   ├── decision_gate.py # 用户决策门控
│   │   │   ├── tool_builder.py  # 运行时工具构建
│   │   │   ├── events.py      # 事件数据类
│   │   │   └── loop_logger.py # 调用日志（Redis + 标准日志）
│   │   ├── workers/           # 业务 Worker Agent
│   │   │   ├── general_assistant.py
│   │   │   ├── data_analyst.py
│   │   │   ├── customer_support.py
│   │   │   ├── code_assistant.py
│   │   │   ├── summarizer.py  # 对话压缩（8 节结构 + 月/年摘要）
│   │   │   └── skill_builder.py
│   │   └── system/            # 系统内置 Agent
│   │       ├── memory_archiver.py  # 日常摘要归档
│   │       ├── monthly_archiver.py # 月度归档
│   │       └── yearly_archiver.py  # 年度归档
│   ├── sandbox/               # 代码沙箱
│   │   ├── executor.py        # subprocess 隔离执行
│   │   ├── scanner.py         # 静态高危模式扫描
│   │   └── file_handler.py    # 沙箱文件管理
│   ├── tools/                 # 工具框架
│   │   ├── base.py            # BaseTool + 权限/可见性/危险操作 + ConsentRequiredException
│   │   ├── registry.py        # 工具注册表
│   │   ├── loader.py          # 动态加载（code/user/agent 三来源）
│   │   ├── permission.py      # ConsentManager（授权核查/缓存/blanket 放行）
│   │   ├── decorators.py      # @tool 装饰器
│   │   ├── file_tools.py      # 文件操作工具
│   │   └── builtin/
│   │       └── file_reader.py # 多格式文件读取（17 类格式）
│   ├── scheduler/             # 定时任务调度
│   │   ├── runner.py          # 异步调度主循环（30s tick）
│   │   ├── store.py           # MySQL 持久化
│   │   ├── notifier.py        # Redis 通知队列 + SSE
│   │   ├── models.py          # 数据模型（TaskType/ActionType 枚举）
│   │   ├── holiday.py         # 中国法定节假日
│   │   └── system_tasks.py    # 系统级 cron（月度/年度归档注册）
│   ├── database/              # 数据库连接池与基础设施
│   │   ├── pool.py            # 统一连接池管理
│   │   ├── redis_keys.py      # Redis Key / TTL 常量（全局唯一）
│   │   └── mysql.py / redis.py / elasticsearch.py
│   └── utils/                 # 工具类
│       ├── paths.py           # PROJECT_ROOT 统一入口
│       ├── crypto.py          # 加解密工具
│       └── log_bus.py / progress_bus.py  # 结构化日志与进度事件总线
├── scripts/
│   ├── setup_embedding.py     # Embedding 模型初始化
│   └── check_es_history.py    # ES 历史数据检查
└── tests/                     # 测试脚本
```

---

## 配置说明

### 日志配置

```yaml
logging:
  level: "DEBUG"   # 开发时推荐 DEBUG；生产推荐 INFO
  format: "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
```

### Embedding 配置

```bash
# 使用 Ollama 本地模型时，先拉取模型
ollama pull bge-m3

# 初始化 Embedding 配置（将模型信息写入 MySQL）
python scripts/setup_embedding.py
```

---

## API 认证

```bash
# 注册
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username": "user1", "password": "pass123"}'

# 登录获取 Token
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "user1", "password": "pass123"}' | jq -r '.token')

# 发送消息（同步）
curl -X POST http://localhost:8000/chat/send \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "分析一下最近的销售数据"}'

# 流式输出（SSE）
curl -N http://localhost:8000/chat/stream \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "写一篇关于 AI 的文章"}'
```

---

## 测试

```bash
# 基础功能测试（无需数据库）
python tests/test_basic.py

# 完整工作流测试（需要数据库）
python tests/test_full_workflow.py

# LLM 集成测试
python tests/test_llm_integration.py

# 系统演示
python tests/run_demo.py
```

---

## 依赖服务

| 服务 | 版本要求 | 用途 |
| --- | --- | --- |
| MySQL | 5.7+ / 8.0+ | 用户、模型、Agent、技能、调度任务、危险操作配置（`dangerous_op_configs`）、工具授权记录（`tool_consent_records`） |
| Redis | 6.0+ | 近期对话、预取缓存、分布式锁、通知队列 |
| Elasticsearch | 8.x | 聊天历史、向量索引 |
| Ollama（可选） | 0.1.24+ | 本地 Embedding 模型服务 |

---

## 许可证

本项目仅供内部使用。
