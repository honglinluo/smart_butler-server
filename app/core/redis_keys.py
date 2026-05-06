"""Redis 命名空间规范 — 所有 key 在此统一定义。

三层命名空间：
  session:token:{token}     登录会话（Token → 用户身份）
  user:{user_id}:init       用户初始化缓存（仅存用户画像；模型配置始终从 MySQL 实时读取）
  memory:{user_id}:*        对话记忆（轮次列表 / 计数器 / 同步锁）

TTL 说明
  SESSION_TTL    86400     (24 h) — 登录会话过期
  INIT_TTL       604800    (7 d)  — 用户初始化数据缓存，跨会话复用
  INIT_TTL_WARN  21600     (6 h)  — 剩余 TTL 低于此值时触发 MySQL 回写
  MEMORY_TTL     2592000   (30 d) — 对话记忆缓存
"""

# ── TTL（秒）─────────────────────────────────────────────────────
SESSION_TTL   = 86_400     # 24 h
INIT_TTL      = 604_800    # 7  d
INIT_TTL_WARN = 21_600     # 6  h
MEMORY_TTL    = 2_592_000  # 30 d

# ── 登录会话（按 token 索引）──────────────────────────────────────
# 字段：user_id / username / token / is_authenticated
SESSION_TOKEN = "session:token:{token}"

# ── 用户活跃 session 集合（按 user_id 索引）────────────────────────
# 值：Set<token>，用于多平台登录管理
# 登录时 SADD，退出时 SREM；Set 为空则触发画像固化与 ES 同步
USER_SESSIONS     = "user:{user_id}:sessions"
USER_SESSIONS_TTL = 2_592_000  # 30 d（远大于单个 SESSION_TTL，确保多平台场景不丢失）

# ── 用户初始化缓存（按 user_id 索引）────────────────────────────
# 字段（存储为 JSON 对象）：
#   profile → dict  — 用户画像（preferences / personal_info / work_content / last_updated）
# 注：模型配置不在此缓存，每次调用从 MySQL llms 表读取 state=1 的最新记录
USER_INIT = "user:{user_id}:init"

# ── 对话记忆（按 user_id 索引）───────────────────────────────────
MEMORY_TURNS           = "memory:{user_id}:turns"           # list  — 全局最近 N 轮（ES 同步用）
MEMORY_SESSION_TURNS   = "memory:{user_id}:turns:{client_type}"  # list  — 按客户端隔离的会话轮次
MEMORY_TOTAL           = "memory:{user_id}:total_count"     # str   — 历史累计轮次数
MEMORY_LOCK            = "memory:{user_id}:sync_lock"       # str   — ES 同步互斥锁
MEMORY_LAST_ACTIVITY   = "memory:{user_id}:last_activity"   # str   — 最近一次对话 ISO 时间戳
MEMORY_COMPRESS_PENDING= "memory:{user_id}:compress_pending"# JSON  — 挂起压缩任务 {reason, scheduled_at}

# ── 定时任务通知队列（按 user_id 索引）──────────────────────────
# 每条消息为 JSON 字符串，RPUSH 写入，LPOP 消费，TTL 24h
NOTIFY_PENDING = "notify:{user_id}:pending"                 # list  — 待读通知列表

# ── 多智能体委派记录（按 user_id 索引）──────────────────────────
# 每条记录为 {agent_name, task_desc, result, timestamp}，最多保留 50 条，TTL 7 天
MEMORY_DELEGATIONS = "memory:{user_id}:delegations"         # list  — Agent 委派记录

# ── 背景预取（按 user_id 索引）───────────────────────────────────
# 上一轮结束后异步预取的记忆，下一轮 build_context() 优先消费，TTL 5 分钟
MEMORY_PREFETCH_RESULT = "memory:{user_id}:prefetch_result" # JSON  — 预取记忆结果列表

# ── 密码加密传输 nonce（防重放）─────────────────────────────────────────────
# 客户端请求公钥时颁发；提交加密密码时消费（一次性）；TTL 5 min
AUTH_NONCE     = "auth:nonce:{nonce}"   # str — 存在即有效
AUTH_NONCE_TTL = 300                    # 5 min

# ── Agent 事件循环（按 user_id + session_id 索引）────────────────────────────
# 每次事件循环的调用日志，TTL 1 h（RPUSH 写入，LRANGE / GET /decisions/logs 消费）
LOOP_LOGS        = "user:{user_id}:loop_logs:{session_id}"  # list — LoopLogEntry JSON
LOOP_LOGS_TTL    = 3_600        # 1 h

# 用户工具构建授权策略（allow / ask / deny），默认 ask
DECISION_POLICY  = "user:{user_id}:decision_policy"         # str

# ── 周期归档调度锁（按 user_id 索引）─────────────────────────────
# 防止同一用户在同一月/年内重复触发归档任务
# TTL 设为 32 天（月度锁）/ 370 天（年度锁），覆盖一个完整周期
ARCHIVE_MONTHLY_LOCK     = "archive:{user_id}:monthly:{year}:{month}" # str — 月度归档锁
ARCHIVE_MONTHLY_LOCK_TTL = 32 * 86_400     # 32 天
ARCHIVE_YEARLY_LOCK      = "archive:{user_id}:yearly:{year}"          # str — 年度归档锁
ARCHIVE_YEARLY_LOCK_TTL  = 370 * 86_400    # 370 天
