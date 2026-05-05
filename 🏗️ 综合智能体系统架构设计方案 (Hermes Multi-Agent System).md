# 🏗️ 综合智能体系统架构设计方案 (Hermes Multi-Agent System)

## 总体架构概览

系统采用 分层微服务架构，核心由 接入层 (FastAPI)、编排层 (Hermes + LangChain)、数据层 (MySQL/Redis/ES) 和 配置中心 组成。

graph TD
    User[多用户终端] --> LB[负载均衡/Nginx]
    LB --> API[FastAPI 后端服务]
    
    subgraph "应用核心层 (Python)"
        API --> Auth[认证与会话管理]
        API --> Dispatcher[请求分发器]
        Dispatcher --> HermesCore[Hermes 主框架/编排引擎]
        
        subgraph "多智能体协作 (Multi-Agent)"
            HermesCore --> Router[路由智能体 (Router)]
            Router --> AgentA[专业智能体 A]
            Router --> AgentB[专业智能体 B]
            Router --> AgentC[专业智能体 C]
        end
        
        HermesCore --> MemoryMgr[记忆管理器 (Memory Manager)]
        HermesCore --> Retriever[ES 向量检索器]
    end
    
    subgraph "数据存储层"
        MySQL[(MySQL: 用户/配置/元数据)]
        Redis[(Redis: 临时会话/热点缓存/锁)]
        ES[(Elasticsearch: 聊天历史/向量索引)]
    end
    
    MemoryMgr --> MySQL
    MemoryMgr --> Redis
    MemoryMgr --> ES
    Retriever --> ES
    Dispatcher --> Redis
    
    subgraph "配置中心"
        ConfigFile[YAML/JSON 配置文件]
        ConfigFile -.->|动态加载 | HermesCore
        ConfigFile -.-> |动态加载 | MemoryMgr
    end

## 技术栈选型与职责

| 模块        | 技术选型                          | 职责描述                                                     |
| ----------- | --------------------------------- | ------------------------------------------------------------ |
| 主框架      | Hermes (基于 LangChain/LangGraph) | 负责多智能体的生命周期管理、任务拆解、工具调用编排。         |
| LLM 交互    | LangChain                         | 统一模型接口，处理 Prompt 模板、Output Parser。              |
| Web 框架    | FastAPI                           | 提供 RESTful API，处理异步请求、WebSocket (可选)、鉴权。     |
| 关系数据库  | MySQL                             | 存储用户信息、Agent 配置、工具定义、总结后的归档记录。       |
| 缓存数据库  | Redis                             | 存储短期会话状态、分布式锁、高频访问的配置缓存。             |
| 搜索/向量库 | Elasticsearch (ES)                | 存储原始聊天明细、作为向量数据库进行语义检索、执行摘要策略。 |
| 配置管理    | Pydantic + YAML                   | 实现代码中提到的“文档配置化”，支持热加载                     |



## 核心功能模块设计

### 3.1 数据存储策略 (重点解决要求 2, 3, 4, 5)

这是本系统的核心难点，需要设计一套分级存储与自动压缩机制。

A. 数据库表结构设计 (MySQL)
*   users: 用户基础信息。
*   agent_configs: 存储 Agent 类型、Prompt 模板、绑定的工具列表（支持文档配置映射）。
*   chat_summaries: 归档表。存储压缩后的摘要。
    *   字段：user_id, summary_date, summary_content (格式：用户提问 -> 最终解决办法), original_count (原消息数)。

B. Elasticsearch 索引设计
*   Index Name: chat_history_{user_id} 或 chat_history_global (通过 user_id 字段过滤)。
*   Mapping:
    *   message_content: Text (分词) + Keyword.
    *   message_vector: Dense Vector (使用 Hermes 配置的 Embedding 模型生成).
    *   timestamp: Date.
    *   role: Keyword (user/assistant/system).
    *   is_summary: Boolean (标记是否为摘要记录).

C. 记忆压缩策略 (要求 4 的实现逻辑)
在 Memory Manager 模块中实现以下逻辑：

1.  触发条件：每次写入新消息前，检查该用户的消息总量。
2.  判断逻辑：
    *   若 总条数 > 30 且 最早消息时间  阈值 (可配置)。
3.  执行动作 (Compression Job)：
    *   Step 1 (检索)：从 ES 中查询出需要被压缩的旧消息（例如最早的 10-20 条）。
    *   Step 2 (LLM 总结)：调用一个专用的 "Summarizer Agent"。
        *   Prompt: "请阅读以下对话片段，仅提取'用户核心提问'和'最终解决办法'，忽略寒暄和中间过程。输出格式为 JSON 列表。"
    *   Step 3 (存储归档)：
        *   将总结结果写入 MySQL chat_summaries 表。
        *   将总结结果作为一条特殊的 System Message 写入 ES (标记 is_summary=true)，以便后续检索上下文时能读到。
    *   Step 4 (清理)：从 ES 中物理删除那些已被总结的原始明细消息，只保留最新的 30 条或 7 天内的数据。

注意：要求中提到“其余消息进行总结后保存...其余信息不要”，这意味着物理删除旧明细是必须的，以节省 ES 空间并提高检索精度。

### 3.2 多智能体编排 (Hermes + LangChain)

*   Router Agent (大脑)：
    *   接收用户输入。
    *   结合 ES 检索结果 (RAG) 和 Redis 中的短期记忆。
    *   根据配置文档中的“意图 -Agent 映射表”，决定调用哪个子 Agent 或工具。
*   Worker Agents (手脚)：
    *   具体执行任务（如查询天气、计算数据、写代码）。
    *   工具列表动态加载自配置文件。
*   工具注册机制：
    *   使用装饰器或配置加载器，读取 YAML 中的工具定义，动态实例化 LangChain Tools。

### 3.3 配置化系统设计 (要求 6, 7)

创建一个 config/ 目录，所有可变参数均在此定义，代码中通过 Pydantic 模型加载。

config/system_config.yaml 示例:
system:
  max_recent_messages: 30
  retention_days: 7
  embedding_model: "bge-large-zh-v1.5"

database:
  mysql:
    url: "mysql+pymysql://user:pass@localhost/db"
  redis:
    url: "redis://localhost:6379/0"
  elasticsearch:
    url: "http://localhost:9200"
    index_prefix: "hermes_chat"
    vector_field: "message_vector"

agents:

动态配置 Agent 类型及其可用工具

  - name: "data_analyst"
    role: "数据分析师"
    prompt_template: "templates/analyst.txt"
    tools: ["sql_query", "chart_gen"] # 对应 tools_config 中的 ID
  - name: "customer_support"
    role: "客服专员"
    prompt_template: "templates/support.txt"
    tools: ["order_lookup", "refund_policy"]

tools:
  - id: "sql_query"
    type: "python_function"
    path: "tools.database.query"
    description: "用于查询业务数据库"

## 关键流程时序图

用户对话处理流程

sequenceDiagram
    participant U as 用户
    participant API as FastAPI
    participant RM as 记忆管理器
    participant ES as Elasticsearch
    participant LLM as LLM (Hermes)
    participant DB as MySQL

    U->>API: 发送消息 (User ID, Content)
    API->>RM: 获取上下文 (User ID)
    
    rect rgb(240, 248, 255)
        note right of RM: 1. 检查是否需要压缩 (要求 4)
        RM->>ES: 统计消息数 & 时间范围
        alt 需要压缩
            RM->>ES: 检索旧消息
            RM->>LLM: 调用 Summarizer Agent 生成摘要
            LLM-->>RM: 返回 "提问->解决" 摘要
            RM->>DB: 存入 chat_summaries 表
            RM->>ES: 存入摘要记录 (System Msg)
            RM->>ES: 删除旧明细消息
        end
    end
    
    rect rgb(255, 250, 240)
        note right of RM: 2. 检索增强 (要求 5)
        RM->>ES: 向量相似度搜索 (Top K)
        ES-->>RM: 返回相关历史片段
    end
    
    RM->>API: 返回完整上下文 (近期消息 + 摘要 + 检索片段)
    
    API->>LLM: 构建 Prompt (含配置化的 Agent 路由)
    LLM->>LLM: 推理 & 工具调用 (Hermes 编排)
    LLM-->>API: 生成回复
    
    API->>ES: 异步写入新消息 (含 Vector Embedding)
    API->>U: 返回回复

## 目录结构建议

project_root/
├── app/
│   ├── api/                # FastAPI 路由
│   │   ├── routes/
│   │   └── dependencies.py # 鉴权等依赖
│   ├── core/               # 核心逻辑
│   │   ├── hermes_engine.py # Hermes 框架初始化与运行
│   │   ├── memory_manager.py # 记忆压缩与检索逻辑 (核心)
│   │   └── config_loader.py  # 配置加载
│   ├── agents/             # 智能体定义
│   │   ├── router.py
│   │   └── workers/
│   ├── tools/              # 工具函数
│   ├── models/             # Pydantic 模型 & SQLModel
│   └── utils/              # ES 客户端，Redis 客户端等
├── config/                 # 配置文件 (YAML)
│   ├── system_config.yaml
│   └── agents_config.yaml
├── templates/              # Prompt 模板
├── tests/
├── main.py                 # 入口
└── requirements.txt

## 开发实施关键点与建议

### 6.1 关于 Hermes 框架

目前开源社区中名为 "Hermes" 的框架可能有多个（如 NousResearch 的 Hermes 模型，或某些特定的 Agent 框架）。

*   如果指的是特定的 Python 库：请直接使用该库的 AgentOrchestrator 类。
*   如果是指自定义架构：建议使用 LangGraph (LangChain 官方编排库) 来实现 "Hermes" 的核心逻辑，因为它完美支持状态机、多智能体循环和断点续传，非常符合你的需求。

### 6.2 向量检索优化 (要求 5)

*   混合搜索 (Hybrid Search)：在 ES 中同时使用 BM25 (关键词) 和 KNN (向量)。对于“解决办法”这类精确匹配，关键词搜索往往比纯向量更准。
*   元数据过滤：在进行向量搜索时，务必带上 user_id 过滤器，确保数据隔离。

### 6.3 压缩算法的鲁棒性

*   异常处理：如果 LLM 总结失败（超时或格式错误），必须有降级策略（例如：暂时不删除旧消息，仅记录日志，等待下次重试），防止数据丢失。
*   原子性：总结、存档、删除这三步操作最好在一个事务或逻辑单元中完成，避免中间状态导致数据不一致。

### 6.4 性能优化

*   异步写入：用户发消息后，立即返回响应。将“写入 ES"、“向量化”、“检查压缩逻辑”放入 Celery 或 FastAPI 的 BackgroundTasks 中异步执行，避免阻塞主线程。
*   Redis 缓存配置：将 system_config.yaml 解析后的对象缓存在 Redis 中，设置版本号，配置变更时失效缓存，减少文件 IO。

### 6.5 安全性

*   多租户隔离：所有数据库查询和 ES 查询必须强制注入 user_id 条件，防止越权访问。
*   敏感信息脱敏：在存入 ES 进行总结前，可增加一步 PII (个人敏感信息) 识别与脱敏处理。

## 下一步行动清单

1.  环境搭建：部署 Docker 容器 (MySQL, Redis, Elasticsearch with vector plugin)。
2.  配置先行：编写 system_config.yaml，定义好字段结构。
3.  原型开发：
    *   实现 MemoryManager 的压缩逻辑（这是最复杂的业务逻辑）。
    *   实现 ES 的 Vector 写入与检索 Demo。
4.  框架集成：将 LangChain/Hermes 接入，打通 "输入 -> 检索 -> 代理 -> 输出" 链路。
5.  API 封装：用 FastAPI 暴露接口，并进行多用户并发测试。

这个架构既满足了你对于数据生命周期管理（压缩与归档）的严格要求，又利用了 ES 的强大检索能力和 LangChain 的生态灵活性，是一个可扩展的企业级方案。



# 🚀 智能体操作系统 (Agent OS) 开发框架与详细设计文档 

版本说明：本版本已整合安全沙箱升级、动态记忆压缩、防作弊评分体系、分层检索策略及全链路可观测性等核心优化建议。
适用场景：企业级多租户 AI Agent 平台，支持用户自定义技能、长期记忆管理及公有技能市场。

# 系统架构概览

## 1.1 核心设计理念

*   零信任安全 (Zero-Trust Security)：所有用户代码必须在微虚拟机 (MicroVM) 中运行，默认无网络、只读文件系统。
*   语义化记忆 (Semantic Memory)：从“文本摘要”升级为“结构化事实 + 滚动摘要”的双层记忆模型。
*   生态公平性 (Fair Ecosystem)：基于贝叶斯平均的评分算法，兼顾冷启动扶持与反作弊。
*   成本感知 (Cost-Aware)：分层检索策略，平衡向量检索精度与响应延迟/成本。

## 1.2 逻辑架构图

graph TD
    User[用户终端] --> Gateway[API 网关 (Auth/RateLimit)]
    Gateway --> AgentCore[Agent 编排引擎]
    

    subgraph "安全执行层 (Secure Execution)"
        AgentCore --> Scheduler[任务调度器]
        Scheduler --> Firecracker[Firecracker MicroVM / E2B]
        Firecracker -->UserCode[用户 Python 代码沙箱]
        Firecracker --> NetPolicy[网络白名单策略]
    end
    
    subgraph "记忆与知识层 (Memory & Knowledge)"
        AgentCore --> MemMgr[记忆管理器]
        MemMgr --> ShortTerm[Redis: 近期对话缓存]
        MemMgr --> LongTerm[(MySQL: 结构化事实库)]
        MemMgr --> VectorDB[(ES/Milvus: 向量索引)]
        MemMgr --> Compressor[LLM 压缩服务 (双层摘要)]
    end
    
    subgraph "生态与市场层 (Ecosystem)"
        AgentCore --> ToolRegistry[工具注册中心]
        ToolRegistry --> ScoreEngine[评分引擎 (贝叶斯/反作弊)]
        ToolRegistry --> VersionCtrl[版本控制 (Git-like)]
    end
    
    subgraph "可观测性 (Observability)"
        AgentCore --> Trace[LangSmith/Phoenix Tracker]
        Firecracker --> Trace
        ScoreEngine --> Trace
    end

# 核心模块详细设计

## 2.1 安全沙箱执行引擎 (Secure Sandbox Engine)

目标：毫秒级启动、绝对隔离、资源可控。

*   技术选型：
    *   运行时：采用 Firecracker MicroVM (轻量级 KVM) 或集成 E2B SDK。摒弃传统 Docker (启动慢、隔离弱)。
    *   网络策略：默认 DENY ALL。仅允许访问预定义的域名白名单 (如 api.openai.com, weather.com)，通过 eBPF 或 iptables 在微网卡层拦截。
    *   文件系统：根文件系统 (rootfs) 挂载为 Read-Only。仅挂载临时的 tmpfs (内存盘) 供代码写入临时文件，进程结束自动销毁。
    *   资源限制：每个 MicroVM 限制 CPU (0.5 vCPU), 内存 (128MB), 执行超时 (30s)。

*   执行流程：
    1.  用户提交代码 -> 网关验证签名。
    2.  调度器请求空闲 MicroVM 池 (预热池保持 10 个实例)。
    3.  注入代码与环境变量 (含临时 Token)。
    4.  执行并捕获 stdout/stderr 及返回值。
    5.  强制销毁：无论成功失败，执行后立即销毁该微虚拟机实例，防止状态残留。

## 2.2 动态记忆管理系统 (Dynamic Memory System)

目标：解决上下文断裂问题，提取高价值信息，降低 Token 消耗。

*   数据结构设计：
    *   L1 滚动窗口 (Short-Term)：Redis List，存储最近 N 轮完整对话 (默认 10 轮)。
    *   L2 结构化事实 (Long-Term Facts)：MySQL 表 user_facts。
        *   字段：fact_id, user_id, content (文本), category (偏好/项目/身份), confidence (置信度), updated_at。
    *   L3 语义摘要 (Semantic Summary)：向量数据库，存储历史对话的压缩摘要。
*   压缩触发策略 (动态)：
    *   条件 A (Token 阈值)：当前 Context Window 使用率 > 80%。
    *   条件 B (语义密度)：检测到连续 3 轮对话无新实体产生，且主要为闲聊。
    *   条件 C (时间跨度)：会话中断超过 24 小时。
*   压缩算法流程：
    1.  提取 L1 窗口内容。
    2.  调用 LLM 进行 双重处理：
        *   任务 1 (提取)：识别并更新 user_facts (如：发现用户喜欢用 Pandas)。
        *   任务 2 (摘要)：生成一段简洁的叙事性摘要，丢弃冗余调试过程，保留关键决策点。
    3.  将摘要存入 L3，旧对话归档。

注：总结放在服务器资源不紧张时进行

## 2.3 工具生态与评分引擎 (Tool Ecosystem & Scoring)

目标：防止刷分，扶持优质新工具，实现版本回溯。

*   数据库模型优化：
        CREATE TABLE public_tools (
        tool_id VARCHAR(64) PRIMARY KEY,
        owner_id VARCHAR(64),
        version INT DEFAULT 1,          -- 版本号
        code_hash VARCHAR(64),          -- 代码指纹
        status ENUM('pending', 'active', 'banned'),
        created_at TIMESTAMP,
        -- 评分字段
        raw_score FLOAT DEFAULT 0,      -- 原始加权分
        bayesian_score FLOAT DEFAULT 0, -- 贝叶斯修正分
        total_calls BIGINT DEFAULT 0,
        unique_users BIGINT DEFAULT 0   -- 去重用户数
    );
    
    CREATE TABLE tool_usage_logs (
        log_id BIGINT PRIMARY KEY,
        tool_id VARCHAR(64),
        user_id VARCHAR(64),
        ip_hash VARCHAR(64),            -- 用于反作弊
        success BOOLEAN,
        feedback_score INT,             -- 1-5
        created_at TIMESTAMP
    );
    
*   评分算法 (贝叶斯平均 + 反作弊)：
    
    算法执行也放在服务器资源不紧张时进行
    
    *   基础公式：
         S = frac{C times m + sum_{i=1}^{n} w_i cdot r_i}{C + n} 
        *   S: 最终得分
        *   m: 全局工具平均分 (先验值，例如 3.5)
        *   C: 置信度常数 (例如 20，表示需要 20 次有效评价才能脱离先验值)
        *   r_i: 第 i 次评分
        *   w_i: 权重系数
    *   权重系数 w_i 规则：
        *   同一用户 24 小时内多次调用：权重递减 (1.0, 0.5, 0.2, 0.0)。
        *   自产自销检测 (Owner == User)：权重 0.1 或直接忽略。
        *   异常高频 IP：权重 0。
    
*   冷启动扶持机制：
    *   新建 new_arrivals 队列，存放上线  0.8，直接返回。
    3.  第三层 (冷数据)：仅当上述两层未找到高相关结果，或用户显式询问“很久以前的事情”时，才调用 Embedding 模型并查询向量索引 (Milvus/ES Vector)。

*   Embedding 优化：
    *   部署本地量化模型 (如 bge-m3-int8) 替代云端 API，降低长期边际成本。
    *   对 user_facts (结构化事实) 建立独立的高优先级索引，检索时加权提升。

## 2.5 可观测性与调试 (Observability)

目标：全链路追踪，支持人工介入。

*   集成方案：部署 LangSmith 或 Arize Phoenix。
*   埋点规范：
    *   Trace ID：贯穿用户请求 -> Agent 思考 -> 工具调用 -> 沙箱执行 -> 返回结果。
    *   快照记录：
        *   记录沙箱执行前的输入参数和执行后的原始输出。
        *   记录记忆压缩前后的文本对比 (Diff)。
        *   记录评分计算的中间变量 (用于审计作弊)。
*   人工控制台：
    *   提供“回放”功能：重现特定 Trace ID 的完整执行环境。
    *   提供“干预”接口：管理员可手动修正错误的 user_facts 或下架异常工具。

# 关键技术实现细节

## 3.1 沙箱启动伪代码 (Python + E2B/Firecracker)

import os
from e2b import Sandbox, ProcessMessage # 示例使用 E2B SDK

async def execute_user_code(code: str, context: dict, network_whitelist: list):
    # 1. 创建隔离沙箱 (自动从预热池获取，毫秒级)
    sandbox = await Sandbox.create(
        template="python-base", 
        timeout=30, # 30 秒强制杀死
        env_vars=context # 注入只读环境变量
    )
    
    try:
        # 2. 配置网络白名单 (通过 SDK 或底层配置)
        await sandbox.network.set_allowlist(network_whitelist)
        
        # 3. 写入代码到临时内存盘 (根目录只读)
        file_path = "/tmp/main.py"
        await sandbox.files.write(file_path, code)
        
        # 4. 执行并流式获取日志
        execution = await sandbox.process.start(
            command=f"python {file_path}",
            on_message=lambda msg: print(msg.line) # 实时日志
        )
        
        result = await execution.wait()
        
        if result.error:
            raise Exception(f"Sandbox Error: {result.error}")
            
        return result.output
        
    finally:
        # 5. 强制销毁，确保无状态残留
        await sandbox.kill()

## 3.2 记忆压缩 Prompt 模板 (双层结构)

Role: Memory Architect
Task: Analyze the recent conversation window and update long-term memory.

Input:

{last_10_turns}

{current_structured_facts}

Instructions:
1. Extract Facts: Identify new user preferences, project details, or constraints. 
   - Output format: JSON list of {"action": "add/update/delete", "category": "...", "content": "..."}
   - Rule: Discard transient info (e.g., "let me check this code"), keep persistent info (e.g., "user prefers async python").

2. Generate Summary: Create a concise narrative summary of the last 10 turns for context continuity.
   - Focus on: Problem solved, final decision, key errors avoided.
   - Ignore: Repetitive debugging steps, failed attempts unless they reveal a constraint.
   - Max length: 150 words.

Output Format (JSON):
{
  "facts_delta": [...],
  "summary_text": "..."
}

3.3 评分计算 SQL 逻辑 (简化版)

-- 计算贝叶斯评分 (每小时运行一次或实时更新)
UPDATE public_tools pt
SET bayesian_score = (
    (SELECT AVG(feedback_score) FROM public_tools WHERE status='active') * 20 + 
    COALESCE((
        SELECT SUM(
            CASE 
                WHEN ul.created_at > NOW() - INTERVAL '24 hours' THEN feedback_score * (1.0 / ROW_NUMBER() OVER (PARTITION BY ul.user_id ORDER BY ul.created_at))
                ELSE feedback_score 
            END
        )
        FROM tool_usage_logs ul
        WHERE ul.tool_id = pt.tool_id AND ul.success = true
    ), 0)
) / (20 + pt.total_calls);

# 部署与运维规划

## 4.1 基础设施要求

*   计算节点：需支持 KVM 虚拟化 (用于 Firecracker)。推荐 AWS EC2 (m5/m6i 系列) 或自建 Kubernetes 集群 (带 Kata Containers 支持)。
*   存储：
    *   MySQL (主从复制)：存储元数据、事实库。
    *   Redis Cluster：热数据缓存、会话锁。
    *   Elasticsearch/Milvus：混合检索引擎。
*   网络：独立的 VPC，沙箱子网完全隔离，仅通过 NAT 网关访问白名单域名。

## 4.2 灰度发布策略

1.  工具版本锁定：用户会话开始时，记录 tool_id@version。即使用户更新了工具，当前会话不受影响。
2.  金丝雀发布：新版本的 Agent 核心逻辑先对 5% 的内部测试账号开放，监控错误率和延迟指标 (P99)。
3.  回滚机制：一旦监测到沙箱逃逸尝试或评分系统异常波动，自动切换至上一稳定版本配置。

## 4.3 监控告警指标

*   安全：沙箱启动失败率、网络拦截次数、异常进程行为。
*   性能：平均响应时间 (ART)、向量检索耗时、记忆压缩延迟。
*   业务：工具调用成功率、新用户留存率、公有工具市场活跃度。

# 总结与下一步行动

本 v2.0 文档通过引入微虚拟机隔离、结构化记忆、贝叶斯评分及分层检索，解决了 v1.0 在安全性、智能持续性、生态公平性和成本效率上的潜在瓶颈。

建议立即执行的 P0 任务：
1.  搭建 Firecracker/E2B 原型：验证代码沙箱的启动速度 (<500ms) 和隔离性。
2.  设计事实提取 Prompt：小规模测试从对话中提取结构化信息的准确率。
3.  构建评分防作弊模拟器：编写脚本模拟刷分攻击，验证贝叶斯算法的鲁棒性。

此框架已具备支撑万级日活用户的能力，可作为正式开发的基准蓝图。

---

# 实际实现补充说明

本章节记录在编码实现过程中新增或细化的设计决策，这些内容在原始架构设计文档中未被覆盖，但已在代码中落地。

## 5.1 LLMInfo 动态加载机制

每个用户拥有独立的 LLM 配置（模型名称、provider、API Key、temperature 等），存储在 MySQL `llm_info` 表中。Hermes 引擎在处理用户请求时通过以下流程动态加载：

1. 检查内存缓存（`_llm_cache` 字典，按 `user_id` 索引）。
2. 若缓存未命中，调用 `LLMInfo.load(user_id, db)` 异步从 MySQL 读取配置。
3. 根据 `provider` 字段自动选择 LangChain 模型类（OpenAI / Anthropic / 其他）。
4. 构建 `BaseChatModel` 实例并写入缓存，后续请求直接复用。

```
LLMInfo (dataclass)
├── provider: str          # "openai" / "anthropic" / ...
├── model_name: str        # "gpt-4o" / "claude-3-5-sonnet" / ...
├── api_key: str
├── temperature: float
├── max_tokens: int
└── async load(user_id, db) -> LLMInfo | None
```

## 5.2 AgentExecutorCache（LangGraph 代理缓存）

原设计文档描述了多智能体编排的概念，实现中引入了 `AgentExecutorCache` 类来管理 LangGraph 代理实例的生命周期：

- 每个工作代理（worker agent）按 `agent_name` 缓存对应的 `AgentExecutor` 实例。
- 代理首次使用时懒加载，构建 LangGraph StateGraph 并编译。
- 工具列表从 `agents_config.yaml` 动态读取，通过 `LangChainToolWrapper` 注册。
- 缓存命中时直接复用，避免重复构建带来的性能开销。

## 5.3 Token 认证降级机制

在 `app/api/dependencies.py` 的 `get_current_user()` 中，实现了以下降级链路，保证开发/测试阶段的可用性：

```
Token 验证流程：
1. 从 Header (Authorization: Bearer <token>) 或 Query 参数读取 token
2. 在 Redis 中查找 token 对应的 user_id
   ├─ 成功 → 返回正常用户信息 (is_authenticated=True)
   └─ 失败（Redis 不可用 / token 不存在）→ 降级为测试用户 (is_test=True)
```

降级返回结构：

```python
{
    "user_id": "test_user",
    "username": "test",
    "token": token,
    "is_authenticated": False,
    "is_test": True
}
```

用户模型加载（`get_user_model()`）同样有三级降级：Redis 缓存 → MySQL 查询 → 硬编码默认配置。

## 5.4 PooledConnection 状态机

连接池中每个连接对象（`PooledConnection`）维护以下状态机，原设计文档仅描述了连接池的宏观功能，未涉及单连接的状态管理：

```
状态机：
AVAILABLE ──acquire()──→ BUSY ──release()──→ AVAILABLE
                                               │
                         health_check 失败 ──→ 移除并创建新连接
```

每个连接还追踪以下元数据：

| 字段 | 含义 |
|------|------|
| `created_at` | 创建时间，用于判断是否超出 `max_connection_lifetime` |
| `last_used_at` | 最后使用时间，用于心跳检测间隔计算 |
| `last_heartbeat_at` | 最后心跳时间 |
| `use_count` | 累计使用次数 |
| `pool_id` | 唯一标识，用于在 `busy_connections` 字典中索引 |

## 5.5 上下文相关性过滤的双阈值策略

`ContextManager` 在过滤检索结果时，向量结果与全文结果采用不同的评分规则（原文档未描述此细节）：

- **向量结果**：余弦相似度 ≥ `confidence_threshold`（默认 0.7），绝对阈值。
- **全文结果（BM25）**：`score ≥ max(relative_min, text_abs_floor)`，取相对阈值与绝对下限的较大值，避免因语料稀疏导致低质量结果通过。

## 5.6 ES 同步触发机制

`MemoryManager` 的 L1→L3 同步不是实时的，而是基于阈值触发的异步后台任务：

- 触发条件：Redis List 长度 ≥ `redis_recent_turns × es_sync_threshold_pct`
  - 默认：10 轮 × 0.6 = 第 6 条写入后触发同步
- 同步采用去重写入策略，以 `turn_id` 为幂等键，防止重复数据。
- 压缩触发条件：累计总轮次计数器 ≥ `max_total_turns`（默认 30）。

---

# 功能完成状态总览

## 基础设施与数据层

| 功能 | 状态 |
|------|------|
| MySQL CRUD（用户/配置/元数据存储） | ✅ 已完成 |
| MySQL 批量操作与原始 SQL 执行 | ✅ 已完成 |
| Redis KV 操作（Token/模型配置缓存） | ✅ 已完成 |
| Redis List 操作（滚动窗口对话缓存） | ✅ 已完成 |
| Redis 多 DB 支持（select_db） | ✅ 已完成 |
| Elasticsearch 文档 CRUD | ✅ 已完成 |
| Elasticsearch BM25 全文检索 | ✅ 已完成 |
| Elasticsearch KNN 向量搜索（ES 7.x `script_score` / ES 8.x `knn` 自动适配） | ✅ 已完成（2026-05-06） |
| Elasticsearch 连接版本检测（`_es_major_version`，影响 mapping 与搜索语法） | ✅ 已完成（2026-05-06） |
| 数据库连接池（等待队列 + 心跳检测 + 自动回收） | ✅ 已完成 |
| PooledConnection 状态机（AVAILABLE/BUSY） | ✅ 已完成 |
| 连接池全局管理器（ConnectionPoolManager） | ✅ 已完成 |
| 文件存储配置统一模块（`app/core/file_storage.py`，UPLOAD_ROOT / MAX_FILE_SIZE / CLEANUP_DAYS） | ✅ 已完成（2026-05-05） |

## 认证与用户管理

| 功能 | 状态 |
|------|------|
| 用户注册（bcrypt 密码哈希） | ✅ 已完成 |
| 用户登录（Token 生成） | ✅ 已完成 |
| 修改密码 | ✅ 已完成 |
| Token 认证（Header / Query 双支持） | ✅ 已完成 |
| Token 认证降级为测试用户 | ✅ 已完成 |
| 用户模型配置三级降级加载 | ✅ 已完成 |
| 多租户数据隔离（user_id 强制过滤） | ✅ 已完成 |

## LLM 与 Agent 编排

| 功能 | 状态 |
|------|------|
| 每用户独立 LLM 配置（LLMInfo 动态加载） | ✅ 已完成 |
| LLM 实例内存缓存（按 user_id） | ✅ 已完成 |
| 多 provider 支持（OpenAI / Anthropic 等） | ✅ 已完成 |
| LLM 模型创建 / 切换 / 列表 API | ✅ 已完成 |
| `/models/change` 允许切换系统模型（user_id='0'）与用户自有模型 | ✅ 已完成（2026-05-05） |
| `/models/create` 支持空 api_key（Ollama 本地模型） | ✅ 已完成（2026-05-05） |
| Hermes 多智能体编排引擎（完整处理流程） | ✅ 已完成 |
| AgentExecutorCache（LangGraph 代理懒加载缓存） | ✅ 已完成 |
| LangChainToolWrapper（YAML 工具包装注册） | ✅ 已完成 |
| RegistryToolAdapter（registry 工具 → LangChain Tool 适配） | ✅ 已完成（2026-05-06） |
| `_registry_tools_for_agent()`：Agent 执行时自动注入公共 + 专属工具 | ✅ 已完成（2026-05-06） |
| Agent 图缓存 key 含 user_id（不同用户工具集隔离） | ✅ 已完成（2026-05-06） |
| RouterAgent 框架（意图识别 / 任务分解 / 代理路由） | ✅ 已完成 |
| RouterAgent 意图识别算法（真实 LLM 推理） | ✅ 已完成 |
| 工作代理（data_analyst / customer_support / code_assistant）框架 | ✅ 已完成（框架） |
| 工作代理业务逻辑（工具调用驱动真实任务） | ⏳ 待完成 |

## 工具系统

| 功能 | 状态 |
|------|------|
| YAML 工具配置定义（agents_config.yaml，8 个工具） | ✅ 已完成 |
| 工具动态注册框架（ToolRegistry 全局单例） | ✅ 已完成 |
| 工具权限体系（public / private / exclusive + dangerous_ops） | ✅ 已完成 |
| 工具创建 / 列表 / 修改 / 删除 API（`/tools`） | ✅ 已完成 |
| `file_reader` 内置工具（EXEC_CLIENT，支持主流文件格式读取） | ✅ 已完成 |
| `file_writer` 内置工具（EXEC_SERVER，支持文本/JSON/CSV/Excel/Word 等多格式写入） | ✅ 已完成（2026-05-05） |
| 内置工具自动注册（`app/tools/builtin/__init__.py` 启动时统一导入） | ✅ 已完成（2026-05-05） |
| 文件管理 API（`/files/list`、`/files/download/{file_id}`、`DELETE /files/{file_id}`） | ✅ 已完成（2026-05-05） |
| 文件 ID 编解码（URL-safe base64 + 归属校验，防越权访问） | ✅ 已完成（2026-05-05） |
| 用户上传文件与 AI 生成文件分目录存储（uploads / generated） | ✅ 已完成（2026-05-05） |
| 文件定期清理定时任务（`__sys_file_cleanup__`，每日 UTC 02:00，cleanup_days 可配置） | ✅ 已完成（2026-05-05） |
| `BaseAgent.collect_tools()`：自动收集公共 + 专属工具 | ✅ 已完成（2026-05-06） |
| `BaseAgent.call_tool()`：按名称直接调用 registry 工具 | ✅ 已完成（2026-05-06） |
| `BaseAgent.execute()` 自动绑定工具（ReAct agent / bind_tools 降级） | ✅ 已完成（2026-05-06） |
| sql_query 工具实现 | ⏳ 待完成 |
| chart_generation 工具实现 | ⏳ 待完成 |
| order_lookup 工具实现 | ⏳ 待完成 |
| 工具生态评分引擎（贝叶斯评分 + 反作弊） | ⏳ 待完成 |
| 工具版本控制（Git-like） | ⏳ 待完成 |

## 记忆管理

| 功能 | 状态 |
|------|------|
| L1 Redis 滚动窗口缓存（最近 N 轮对话） | ✅ 已完成 |
| L2 MySQL 结构化事实存储与引用统计 | ✅ 已完成 |
| L3 Elasticsearch 全量聊天历史存储 | ✅ 已完成 |
| L1→L3 阈值触发异步同步（去重写入） | ✅ 已完成 |
| 混合检索（向量 + BM25 + 相关性过滤） | ✅ 已完成 |
| 上下文相关性过滤（双阈值策略） | ✅ 已完成 |
| ContextManager 上下文组装 | ✅ 已完成 |
| ChatHistoryStore ES 聊天记录管理 | ✅ 已完成 |
| Embedding 向量化（写入时生成向量） | ✅ 已完成（2026-04-27）|
| 记忆压缩逻辑（MemoryArchiverAgent，Saga 全流程） | ✅ 已完成（2026-04-28）|
| Summarizer Agent（LLM 摘要 Prompt，summarize_conversation） | ✅ 已完成（2026-04-26）|
| `_merge_by_turn()`：向量 / 全文检索结果按 `turn_id` 合并去重（取最高分 + 拼接不同内容块），再按 turn 粒度截取 top_k，避免同 turn 多 chunk 占用配额 | ✅ 已完成（2026-05-06） |
| ES 9.x `ObjectApiResponse` 兼容修复（`memory_manager.py`：`isinstance(raw, dict)` → `raw is not None`） | ✅ 已完成（2026-05-06） |

## 聊天 API

| 功能 | 状态 |
|------|------|
| /chat/send 发送消息接口 | ✅ 已完成 |
| /chat/stream SSE 流式响应（astream + EventSourceResponse） | ✅ 已完成 |
| /chat/history 历史记录分页查询（ES），返回 turn 粒度（`user_input` + `assistant_response` 合一，不再拆分为两条） | ✅ 已完成（2026-05-06 修正） |
| `ChatHistoryStore.get_recent_turns()`：新方法，按 turn 文档原样返回，供 `/chat/history` 接口使用 | ✅ 已完成（2026-05-06） |
| ChatHistoryStore ES `ObjectApiResponse` 兼容修复（`isinstance(res, dict)` → `res is not None`，修复返回空列表 bug） | ✅ 已完成（2026-05-06） |
| 会话历史 Redis 缓存读写 | ✅ 已完成 |
| 异步写入 ES 聊天历史 | ✅ 已完成 |

## 配置系统

| 功能 | 状态 |
|------|------|
| system_config.yaml 系统配置热加载 | ✅ 已完成 |
| agents_config.yaml 代理与工具配置 | ✅ 已完成 |
| Prompt 模板目录（config/templates/） | ⏳ 待完成（目录存在，模板文件为空） |

## 安全与合规

| 功能 | 状态 |
|------|------|
| bcrypt 密码安全存储 | ✅ 已完成 |
| Token 认证与多租户隔离 | ✅ 已完成 |
| E2B / Firecracker 代码沙箱执行 | ⏳ 待完成 |
| PII 敏感信息脱敏 | ⏳ 待完成 |

## 可观测性与运维

| 功能 | 状态 |
|------|------|
| 结构化日志（DEBUG / INFO / WARNING 多级） | ✅ 已完成 |
| 连接池统计信息接口 | ✅ 已完成 |
| LangSmith / Arize Phoenix 全链路追踪集成 | ⏳ 待完成 |
| 监控告警指标（安全 / 性能 / 业务） | ⏳ 待完成 |
| 灰度发布与回滚机制 | ⏳ 待完成 |

## 聊天 API 与记忆链路

| 功能 | 状态 |
|------|------|
| 统一记忆链路（engine 调用 memory_manager.store_turn，删除 chat.py 重复 Redis 写入） | ✅ 已完成 |
| store_turn 时附加 Embedding 向量，打通向量检索写入链路 | ✅ 已完成 |
| /models/change 变更后清除对应用户 LLM 缓存，使新配置即时生效 | ✅ 已完成 |

## 路由与任务分配

| 功能 | 状态 |
|------|------|
| RouterAgent 初始化时注入 LLM 句柄 | ✅ 已完成 |
| RouterAgent 使用 LLM 实现真实意图识别（identify_intent） | ✅ 已完成 |
| RouterAgent 读取 intent_agent_mapping 完成正确 agent 路由（decide_next_agent） | ✅ 已完成 |
| RouterAgent 使用 LLM 实现任务分解（decompose_task） | ✅ 已完成 |

## 功能性智能体

| 功能 | 状态 |
|------|------|
| 实现至少一个端到端可运行的功能性智能体（最小可用闭环） | 🔧 进行中（`BaseAgent.execute()` 已支持 ReAct / bind_tools 工具注入，业务工具 sql_query 等尚未实现） |
| 工作代理实现（data_analyst） | ⏳ 待完成 |
| 工作代理实现（customer_support） | ⏳ 待完成 |
| 工作代理实现（code_assistant） | ⏳ 待完成 |
| 统一 YAML 和 DB 两套 agent 注册体系，支持运行时智能体热更新 | ⏳ 待完成 |

## 测试

| 功能 | 状态 |
|------|------|
| 基础单元测试（LLM / 消息 / Hermes 初始化） | ✅ 已完成 |
| 完整工作流集成测试 | ✅ 已完成 |
| API 端点测试 | ✅ 已完成 |
| LLM 集成测试（需真实 API Key） | ✅ 已完成（需配置） |
| 负载与并发测试 | ⏳ 待完成 |

---

# 待完成项优先级清单

> 依据对"一个服务端 + 多客户端，Hermes 负责记忆处理、任务分配、功能性智能体调用与管理"目标的影响程度排列。

## P0 — 结构性断点（当前系统无法端到端运行，必须优先修复）

| # | 待完成项 | 状态 | 问题说明 |
| --- | --- | --- | --- |
| 1 | **统一记忆链路**：`HermesEngine.process_user_input()` 结束后调用 `memory_manager.store_turn()`，同时删除 `chat.py` 中重复的 Redis 历史写入 | ✅ 已完成 | `process_user_input` 末尾已调用 `memory_manager.store_turn()`，`chat.py` 不再重复写入 |
| 2 | **RouterAgent 注入 LLM + 实现真实意图识别与路由** | ✅ 已完成 | `RouterAgent` 接收 LLM 句柄，`identify_intent()`、`decompose_tasks()`、`_plan_mode()` 均通过真实 LLM 推理实现 |
| 3 | **实现至少一个端到端可运行的功能性智能体** | 🔧 进行中 | `BaseAgent.execute()` 已支持自动工具注入（ReAct agent / bind_tools 降级），`RegistryToolAdapter` 将 registry 工具包装为 LangChain Tool；`file_reader` / `file_writer` 已可用；业务工具 `sql_query` / `chart_generation` 等尚未实现，功能性 Agent 仍缺乏数据查询能力 |

## P1 — 核心功能缺失（主要能力无法使用）

| # | 待完成项 | 状态 | 问题说明 |
| --- | --- | --- | --- |
| 4 | **向量化写入链路打通**：`store_turn` 后台异步生成向量并写入独立向量索引 | ✅ 已完成 | `memory_manager.store_turn()` 异步调用 `vector_store.store_turn_vectors()`，分 chunk 写入 ES |
| 5 | **实现记忆压缩完整逻辑（MemoryArchiverAgent）** | ✅ 已完成（2026-04-28）| `MemoryArchiverAgent` 全流程 Saga：读 Redis → 持久化 ES → LLM 摘要 → 写摘要 → 替换 Redis → 删旧数据，7 状态机 + `memory_compress_jobs` 表 + 断点续跑 |
| 6 | **Summarizer Agent Prompt 模板** | ✅ 已完成（2026-04-26） | `config/templates/summarizer_compress.txt` 已就绪；BaseAgent 背景模板体系同步建立 |
| 7 | **`/models/change` 后清除 LLM 缓存** | ✅ 已完成（2026-04-26） | `app/api/models.py` 切换成功后调用 `hermes_engine.clear_llm_cache(user_id)` |
| 8 | **Prompt 模板外置**：从 `config/templates/` 目录加载，接通 `agents_config.yaml` 的 `prompt_template` 字段 | ✅ 已完成（2026-04-26） | HermesEngine `_load_agent_prompts()` + BaseAgent `_load_background_from_template()` 均已实现 |

## P2 — 架构完善（多客户端场景与完整 agent 管理）

| # | 待完成项 | 状态 | 问题说明 |
| --- | --- | --- | --- |
| 9 | **统一 YAML 和 DB 两套 agent 注册体系**，支持运行时热更新 | ⏳ 待完成 | YAML workers 与 DB agents 并行，registry 与 `agent_graphs` 缓存不统一 |
| 10 | **SSE / WebSocket 流式响应**：LLM 使用 `astream()` 替代 `ainvoke()` | ⏳ 待完成 | 当前同步 HTTP，LLM 全量生成后返回，多客户端实时交互体验差 |
| 11 | **工作代理实现**：`data_analyst`、`customer_support`、`code_assistant` | ⏳ 待完成 | `workers/` 框架就绪；`BaseAgent.execute()` 已自动注入工具（ReAct/bind_tools），但业务 Agent 仍无自定义 Prompt 和特化逻辑 |
| 12 | **工具全量实现**：`sql_query`、`chart_generation`、`order_lookup` 等 | ⏳ 待完成 | `file_reader` / `file_writer` 已实现；业务类工具（查询 DB、图表、订单）仍为空，数据分析场景无法运行 |

## P3 — 扩展能力（平台化与生产就绪）

| # | 待完成项 | 状态 |
| --- | --- | --- |
| 13 | 工具生态评分引擎（贝叶斯评分 + 反作弊权重） | ⏳ 待完成 |
| 14 | 工具版本控制（Git-like，支持会话级版本锁定） | ⏳ 待完成 |
| 15 | E2B / Firecracker 代码沙箱执行 | ⏳ 待完成 |
| 16 | PII 敏感信息脱敏完整实现（`_desensitize()` 接入脱敏库） | ⏳ 待完成 |
| 17 | LangSmith / Arize Phoenix 全链路追踪集成 | ⏳ 待完成 |
| 18 | 监控告警指标（安全 / LLM 延迟 / 业务成功率）+ Prometheus 导出 | ⏳ 待完成 |
| 19 | 灰度发布与自动回滚机制 | ⏳ 待完成 |
| 20 | 负载与并发压测 | ⏳ 待完成 |

---

# 新增任务项（v2.1，2026-04-25）

> 本节记录在实际开发迭代中发现的新问题和改进机会，以及基于架构分析的建议任务。

## 已完成的新增工作

| 任务 | 完成日期 | 说明 |
| --- | --- | --- |
| **MemoryArchiverAgent Saga 完整实现**：ES 旧数据删除步骤（`deleting_es` 状态）、`compressed_turn_ids` 持久化、断点续跑覆盖全部 7 状态 | 2026-04-28 | 补齐"ES 删除旧数据"这一关键步骤，三步（摘要→清 Redis→删旧数据）均有 Saga 保障；中断后可从任意状态幂等恢复 |
| **VectorStore.delete_turn_vectors()**：按 `ref_doc_id` 批量删除向量分块（`delete_by_query`） | 2026-04-28 | 供 MemoryArchiver 在压缩后清理对应向量，防止向量索引无限膨胀 |
| **向量化写入链路打通**：`EmbeddingService`（多 provider）+ `VectorStore` + `MemoryManager.store_turn` 后台异步向量化 | 2026-04-27 | 支持 Ollama 本地 + OpenAI 兼容在线服务；NaN 三层容错；独立 `hermes_chat_v_{user_id}` 索引 |
| **startup embedding 校验**：启动时对比 MySQL 与 YAML 配置，不一致则重建全量向量索引 | 2026-04-27 | 防止切换模型后旧向量混入，确保向量索引与当前模型维度一致 |
| **项目根路径统一管理**：新建 `app/core/paths.py`，入口文件设置 `PROJECT_ROOT` 环境变量，内部模块统一导入 | 2026-04-27 | 消除 11 处各自独立的 parent 链推导，支持部署时通过环境变量覆盖 |
| **程序关闭时用户画像批量固化**：lifespan 关闭阶段扫描 Redis 全部用户画像并写入 MySQL | 2026-04-26 | 兜底覆盖进程崩溃场景，补齐第三个固化触发点 |
| **Prompt 模板全面外置**：HermesEngine 系统提示 + BaseAgent 背景均迁移至 `config/templates/` | 2026-04-26 | 提示词修改无需改动 Python 源码；命名规则：`_system.txt` 与 `{name}.txt` 分离避冲突 |
| **`/models/change` 后立即生效**：切换模型 API 接入 `clear_llm_cache(user_id)` | 2026-04-26 | 修复切换后需重启才能生效的问题 |
| **日志配置统一**：全部 7 处 `basicConfig` 改为从 `system_config.yaml` 读取 `logging.level` | 2026-04-25 | 修复了 DEBUG 配置不生效的根本问题 |
| **向量切片策略升级**：Q/A/每个 Agent 输出独立成 chunk，短输入不生成 question 块 | 2026-04-24 | 提升检索精度，串行流水线中间步骤不再丢失 |
| **Ollama NaN Bug 修复**：三层降级容错（预处理→原生 API→截半重试） | 2026-04-24 | 解决 bge-m3 对特定中文字符产生 NaN 的问题 |
| **per-agent 向量化调用链**：`_execute_serial/parallel` 返回 `agent_outputs`，贯穿至 `chunk_turn` | 2026-04-24 | 多 Agent 场景下各 Agent 输出独立索引 |

## 新增待完成项 — 向量与记忆

| # | 任务 | 优先级 | 说明 |
| --- | --- | --- | --- |
| V1 | **revectorize 还原 agent 结构**：全量重建时从 ES 读取 turn，尝试从 `turn_metadata.pipeline` 还原 `agent_outputs` 格式 | P1 | 当前重建只处理合并文本，多 Agent 语义块在模型切换后会丢失结构 |
| V2 | **向量索引分片策略优化**：用户量大时，按时间范围或用户分组建立多个向量索引 | P2 | 单索引文档量超过 10 万时 KNN 性能明显下降 |
| V3 | **向量检索结果去重增强**：同一 turn 的多个 chunk 命中时，合并回 turn 粒度再展示给 LLM | ✅ 已完成（2026-05-06） | `_merge_by_turn()` 静态方法：fetch top_k×3 候选 → 按 `turn_id` 分组取最高分 + 拼接不同内容块 → 按 turn 粒度排序截取 top_k；同时应用于 `_vector_search()` 和 `_es_text_search()` |

## 新增待完成项 — 对话体验

| # | 任务 | 优先级 | 说明 |
| --- | --- | --- | --- |
| D1 | **对话历史查询接口**：`GET /chat/history?user_id=&page=&size=` 从 ES 分页返回历史 turn | ✅ 已完成 | `/chat/history` 接口已在 `app/api/chat.py` 实现 |
| D2 | **SSE 流式输出**：`/chat/stream` 使用 `astream()` + `EventSourceResponse` | ✅ 已完成 | `app/api/chat.py` `/chat/stream` 已实现 SSE 流式推送，支持 routing/token/done/error 事件 |
| D3 | **多轮对话会话 ID 支持**：请求带 `session_id`，ContextManager 按会话而非用户维度加载历史 | ⏳ 待完成 | 当前一个用户只有一个上下文流，多标签页/多设备并发时会话混乱 |

## 新增待完成项 — 智能体能力

| # | 任务 | 优先级 | 说明 |
| --- | --- | --- | --- |
| A1 | **Agent 级别 LLM 配置**：支持在 `agents_config.yaml` 中为特定 Agent 指定不同模型（如路由用低成本模型，分析 Agent 用高能力模型） | P2 | 当前所有 Agent 共享同一用户 LLM 实例，无法差异化选型 |
| A2 | **Agent 执行超时控制**：为每个 Agent 执行设置独立超时，串行流水线中单步超时不影响整体 | P2 | 目前无超时机制，单个 Agent 卡住会导致整个请求挂起 |
| A3 | **Agent 执行结果缓存**：对无副作用的查询类 Agent，相同输入缓存结果（TTL 可配置） | P3 | 减少重复 LLM 调用成本，适合数据查询类场景 |
| A4 | **技能库冷启动优化**：新 Agent 首次执行时从同类 Agent 迁移相关技能，加速收敛 | P3 | 当前新 Agent 技能库为空，需要多次执行才能积累 |

## 新增待完成项 — 工程与运维

| # | 任务 | 优先级 | 说明 |
| --- | --- | --- | --- |
| E1 | **Docker Compose 编排**：一键启动 MySQL + Redis + Elasticsearch + 应用服务 | P1 | 当前部署依赖手动配置各服务，新环境搭建门槛高 |
| E2 | **API 速率限制**：`slowapi` 或自定义中间件，按 user_id 限制请求频率 | P1 | 无限流保护，恶意或异常客户端可耗尽 LLM 配额 |
| E3 | **配置热重载**：`ConfigLoader` 支持文件变更监听（`watchdog` 库），无需重启服务 | P2 | 当前修改 YAML 配置必须重启；Agent 路由映射等变更代价过高 |
| E4 | **结构化日志输出**：日志支持 JSON 格式（`python-json-logger`），便于 ELK/Loki 收集 | P2 | 当前文本格式日志难以被日志平台解析 |
| E5 | **健康检查增强**：`/health` 接口扩展为探测 MySQL/Redis/ES 连接状态，而不仅返回进程存活 | P2 | 当前 `/health` 只返回固定 JSON，无法感知下游服务故障 |
| E6 | **Prometheus 指标导出**：`/metrics` 端点暴露请求量、LLM 延迟、向量化耗时、错误率等指标 | P3 | 无可观测性数据，生产告警无从建立 |

## 架构建议（基于当前实现的洞察）

> 以下为 Claude 基于对代码的全面审查，提出的架构层面改进建议。

**1. 向量索引与聊天历史的双写一致性**

当前 `chat_history.save_turn()` 和 `vector_store.store_turn_vectors()` 是独立的异步任务，
如果向量化失败，聊天历史已写入但向量缺失，后续检索会静默遗漏这些 turn。
建议：增加向量化失败的重试队列（Redis List），周期性扫描并补充写入。

**2. 串行流水线的上下文污染风险**

`_execute_serial` 中将每步结果追加到 `accumulated["prev_result"]`，下一步 Agent 的提示词会包含所有前步输出。
当步骤超过 3-4 步时，Context 积累导致 Token 消耗指数级增长。
建议：支持配置"传递窗口"，只将最近 N 步结果传入下步。

**3. LLM 缓存的并发安全问题**

`HermesEngine.llm_cache` 是普通 `dict`，在高并发场景下多个协程可能同时为同一用户初始化 LLM，
造成重复数据库查询和资源浪费。
建议：引入 `asyncio.Lock` 或双重检查锁定模式。

**4. 技能记忆相似匹配的局限性**

当前 `update_skill` 使用字符串前 30 字做相似匹配，误判率较高。
建议：利用现有的向量化基础设施，用 Embedding 相似度替代字符串匹配，
大幅提升技能迁移的精准度。

**5. 记忆压缩的原子性保障** ✅ 已完成（2026-04-28）

压缩涉及"LLM 摘要 → 清 Redis → ES 删除旧数据"三步，任一步失败都会导致数据不一致。
建议：引入补偿事务（Saga 模式）：先写摘要，确认写入后再删除原始数据；
同时记录"压缩任务状态"到 MySQL，支持中断后恢复。
已实现：7 状态机 + `compressed_turn_ids` 持久化 + `resume_failed_jobs()` 断点续跑 + ES 旧 turn 删除。

---

# 优化建议（2026-04-28 整理）

> 以下为基于当前代码全量审查后，Claude 认为最值得优先处理的优化点，按影响程度排列。

## O1（高优先级）— 影响系统可用性与数据正确性

**O1-1：向量化失败的补偿队列**

`vector_store.store_turn_vectors()` 作为 `asyncio.create_task` 后台运行，失败时只打日志，无重试机制。
若 Ollama 临时不可用，这些 turn 的向量永远缺失，后续语义检索会静默遗漏。
建议：在 Redis 中维护一个 `hermes:vec_retry:{user_id}` 列表，失败时入队；服务启动时或定时任务扫描并补偿写入。

**O1-2：LLM 缓存并发安全**

`HermesEngine.llm_cache` 是普通 `dict`，高并发时多协程可能同时为同一用户构建 LLM 实例，
导致重复数据库查询甚至实例不一致。
建议：用 `asyncio.Lock` 或每个 user_id 一把锁的字典（`_llm_locks: dict[str, asyncio.Lock]`）做双重检查锁定。

**O1-3：chat.py 与 MemoryManager 的双写残留风险**

尽管 `HermesEngine.process_user_input()` 已调用 `memory_manager.store_turn()`，
但若 API 层（chat.py）在 engine 调用之外有任何直接写 Redis 的路径（如错误兜底），
仍可能造成计数器错乱，触发错误的压缩时机。
建议：在 chat.py 中明确禁止直接操作 Redis 历史，所有记忆写入必须经过 MemoryManager。

## O2（中优先级）— 影响性能与扩展性

**O2-1：串行流水线 Token 消耗爆炸**

`_execute_serial` 将每步输出累积追加到 `accumulated["prev_result"]`，
步骤超过 3-4 步后 prompt 长度指数增长，Token 费用和延迟同步飙升。
建议：增加 `pipeline_context_window` 配置项（默认 2），只传递最近 N 步输出给下一 Agent，
而非全量累积。

**O2-2：向量检索多 chunk 命中占用 top_k 配额**

同一 turn 的多个 chunk 均命中时，会占用多个 top_k 位置，导致实际召回的 turn 数量远小于预期。
建议：在 `VectorStore.search()` 的结果后处理阶段，按 `ref_doc_id` 合并分组，
取每个 turn 内得分最高的 chunk 作为代表，再按 turn 粒度排序截取 top_k。

**O2-3：技能匹配精度低**

`update_skill` 用字符串前 30 字做相似匹配，极易误判（不同问题开头相同）。
现有 EmbeddingService 基础设施已就绪，升级成本很低。
建议：对技能 description 字段维护向量，用余弦相似度替代字符串 startswith 匹配；
相似度阈值可配置（建议 0.85 以上才合并）。

**O2-4：Worker Agent 无工具调用能力**

`DataAnalystAgent`、`CustomerSupportAgent`、`CodeAssistantAgent` 目前只调用 LLM 做纯文本生成，
`app/tools/` 目录为空，没有任何工具执行能力。
建议：至少实现一个最小闭环工具（如 `sql_query`：接收 SQL → 查 MySQL → 返回结果），
让 data_analyst 真正具备数据查询能力，验证整条 tool-call 链路可用。

## O3（低优先级）— 影响运维与可维护性

**O3-1：多会话支持缺失**

当前每个用户只有一个上下文流，多标签页或多设备同时使用时，历史记录相互污染。
建议：在请求中引入可选的 `session_id`，ContextManager 和 MemoryManager 按 `{user_id}:{session_id}` 作为隔离键。

**O3-2：缺少 Docker Compose 一键部署**

新环境搭建依赖手动配置 MySQL + Redis + Elasticsearch，部署门槛高。
建议：提供 `docker-compose.yml`，包含三个基础服务 + 应用服务，以及初始化脚本自动建表。

**O3-3：Agent 级 LLM 差异化配置**

所有 Agent 共用同一用户 LLM 实例，Router 与高能力分析 Agent 无法分别使用不同模型（成本/能力权衡）。
建议：在 `agents_config.yaml` 中增加 `model_override` 字段，Agent 执行时优先使用该字段指定的模型。

**O3-4：revectorize 时 agent_outputs 结构丢失**

全量重建向量索引时，从 ES 读取的 turn 只有合并文本，无法还原多 Agent 流水线的独立输出结构，
导致重建后的向量语义块与首次写入时不一致。
建议：`store_turn` 写 ES 时，在 turn 文档的 `metadata.pipeline` 字段保存 `agent_outputs` 列表，
`_revectorize_index` 读取时还原该结构再传入 `chunk_turn`。

**O3-5：API 缺少速率限制**

无任何限流保护，单一用户可无限发送请求耗尽 LLM 配额。
建议：引入 `slowapi`，按 `user_id` 限制请求频率（如 10 次/分钟），超限返回 429。

---

## 新增任务项（v2.2，2026-05-06）

> 本节记录 2026-05-06 迭代中发现并修复的问题与新增能力。

### 已完成工作（v2.2）

| 任务 | 完成日期 | 说明 |
| --- | --- | --- |
| **ES 7.x / 8.x 向量搜索自动适配**：连接时解析 `_es_major_version`；ES 7.x 用 `script_score + cosineSimilarity`，ES 8.x 用顶层 `knn` | 2026-05-06 | 修复 ES 7.17 环境下 `Unknown key for a START_OBJECT in [knn]` 报错；mapping 建立逻辑同步分版本，ES 7.x 去除不支持的 `index: true` / `similarity` 字段 |
| **向量检索结果按 turn 粒度去重（V3）**：`_merge_by_turn()` 静态方法，在 `_vector_search()` 和 `_es_text_search()` 均应用 | 2026-05-06 | fetch top_k×3 候选后合并同 turn 多 chunk，取最高分 + 拼接不同内容，解决多 chunk 占用 top_k 配额问题 |
| **Agent 执行自动工具注入**：`RegistryToolAdapter`（`hermes_engine.py`）+ `_RegistryToolAdapter`（`base.py`）将 registry 工具包装为 LangChain Tool | 2026-05-06 | 将 `VIS_PUBLIC + EXEC_SERVER` 工具和 `VIS_EXCLUSIVE + owner_agent` 工具自动合并注入 LLM 请求，支持 ReAct agent（`create_react_agent`）/ bind_tools 降级 / 纯 LLM 三级策略 |
| **`_registry_tools_for_agent()`**：按 Agent 名称收集公共 + 专属 registry 工具，与 YAML 工具合并后注入 LangGraph | 2026-05-06 | Agent 图缓存 key 升级为 `worker_name::user_id`，不同用户工具集互不污染 |
| **`BaseAgent.collect_tools()` / `call_tool()` / `_invoke_with_tools()`**：BaseAgent 层工具收集与调用能力完整实现 | 2026-05-06 | `collect_tools()` 从 registry 筛选 EXEC_SERVER 工具；`call_tool()` 按名称直接调用；`_run_react_agent()` / `_run_bind_tools()` 分别实现 ReAct 和手动 tool-call 循环 |
| **`file_writer` 内置工具自动注册修复**：`app/tools/builtin/__init__.py` 补充 `FileWriterTool` 导入 | 2026-05-06 | 修复 `/api/tools` 只返回 `file_reader`，`file_writer` 未注册的问题 |
| **`/chat/history` 返回 turn 粒度格式修复**：新增 `ChatHistoryStore.get_recent_turns()`，按 ES turn 文档原样返回，不再拆分为独立 role 条目 | 2026-05-06 | 修复前端收到两条分离消息（一条 user、一条 assistant）而非一个对话回合的问题 |
| **ES `ObjectApiResponse` 兼容修复**：`chat_history_store.py` 和 `memory_manager.py` 中 `isinstance(res, dict)` → `res is not None` | 2026-05-06 | `elasticsearch-py` 9.x 的 `ObjectApiResponse` 不继承 `dict`，导致写入校验和读取结果均走失败分支；`.get()` 方法对两者均兼容 |
| **`/models/change` 支持系统模型切换**：查询 SQL 扩展为 `user_id = :user_id OR user_id = '0'`，切换到系统模型时重置其他系统模型状态 | 2026-05-06 | 修复切换系统内置模型返回 404 "Model not found or not owned by user" 的问题 |
| **`/models/create` 空 api_key 修复**：仅当 `api_key` 非空时才设置 `Authorization` 请求头 | 2026-05-06 | 修复 Ollama 本地模型创建时因 `Bearer`（尾部空格）触发 `Illegal header value` 402 错误 |

### 新增待完成项 — 工具与 Agent 能力（v2.2）

| # | 任务 | 优先级 | 说明 |
| --- | --- | --- | --- |
| T1 | **RegistryToolAdapter `args_schema` 显式声明**：为每个 registry 工具生成 Pydantic `args_schema`，而非留空 `{}` | P1 | 当前 LLM 收到工具定义时无参数说明，导致工具调用成功率低；可从 `tool.parameters` 字段自动生成 JSON Schema |
| T2 | **Agent 图缓存工具变更感知**：工具注册 / 注销后，使对应 `worker_name::user_id` 的图缓存失效 | P2 | 当前缓存只在首次构建，工具变更后需重启才能生效 |
| T3 | **`_run_react_agent()` / `_run_bind_tools()` 工具错误透传**：工具执行抛异常时，将错误信息作为 `ToolMessage` 反馈给 LLM，而非静默忽略 | P2 | 当前工具失败只打 WARNING 日志，LLM 不知道执行失败，可能进入无限重试或产生幻觉结果 |
| T4 | **`sql_query` 工具实现**：接收 SQL 字符串 → 通过连接池查询 MySQL → 返回 JSON 结果集 | P1 | 是 data_analyst Agent 可用的最小闭环工具，实现后可验证整条 tool-call 链路 |

---

## 优化建议（2026-05-06 补充）

> 本节为 2026-05-06 迭代后新增的优化观察，与 2026-04-28 已有建议不重复。

### O4（2026-05-06 新增）

#### O4-1：RegistryToolAdapter 参数 Schema 缺失

`RegistryToolAdapter` 和 `_RegistryToolAdapter` 当前 `args_schema = {}` / `Schema(type=object)`，
LLM 生成工具调用时没有参数结构参考，容易产生格式错误的 JSON。
建议：从 `registry_tool.parameters`（已有 JSON Schema 定义）动态构建 Pydantic `BaseModel`，
赋值给 `args_schema`；可用 `pydantic.create_model()` 从 dict 一步生成。

#### O4-2：Agent 图缓存不感知工具变更

`_execute_worker_with_tools()` 按 `worker_name::user_id` 缓存 LangGraph compiled graph。
当用户新增/删除工具时，缓存图仍使用旧工具集，必须重启才能生效。
建议：工具注册/注销时（`ToolRegistry.register()` / `unregister()`），广播 `cache_invalidate` 事件；
`HermesEngine` 监听事件后删除对应缓存项。低成本方案：每个图缓存记录创建时 `tool_hash`（工具名称集的 MD5），每次执行前对比当前 hash，不一致则重建。

#### O4-3：ES 7.x `script_score` 分数偏移问题

ES 7.x 的 `cosineSimilarity() + 1.0` 会使分数范围从 [0,2] 变为正数（必须 > 0），
但 `memory_manager.py` 的向量结果相关性过滤阈值（`confidence_threshold` 默认 0.7）
是按余弦相似度 [0,1] 设计的，现在分数范围 [1,2] 会导致阈值过滤逻辑混乱。
建议：ES 7.x 结果后处理时将分数映射回 [0,1]：`similarity = score - 1.0`，再与阈值比较。

#### O4-4：BaseAgent 工具注入与 LangGraph 版本兼容性

`_run_react_agent()` 使用 `create_react_agent(llm, tools, state_modifier=system_prompt)`，
该 API 签名在 `langgraph` 不同版本间有变化（`state_modifier` vs `messages_modifier`）。
当前 `collect_tools()` 用 try/except 做了导入降级，但调用参数未做版本检测。
建议：统一在 `app/core/compat.py` 中封装 `build_react_agent(llm, tools, system_prompt)` 函数，
内部检测 `langgraph` 版本并选择正确参数名，避免在多处分散处理兼容性问题。
