"""任务规划与拆分模块

两级拆分架构
────────────
L1（Agent 级，由 RouterAgent 完成）
  将用户目标拆分为若干子任务，每条任务对应一个 Agent，明确"由谁做什么"。
  示例：
    任务1 → data_mining_agent：爬取 XX 平台的销售数据
    任务2 → data_analyst_agent：对爬取的数据做相关性分析
    任务3 → summarizer_agent：将分析结果输出为报告

L2（可执行级，由各执行 Agent 完成）
  Agent 收到 L1 任务后进一步拆分为可单步执行的操作序列，支持失败终止、依赖阻塞。
  示例（data_mining_agent 的 L1 任务细化）：
    步骤1：连接并访问 XX 平台，确认网络正常，失败则终止
    步骤2：分页获取销售数据并缓存到临时存储
    步骤3：对缓存数据做初步清洗，输出标准 JSON

设计目标
────────
- L1/L2 使用独立的 Redis 命名空间，互不干扰
- 支持三种写入模式：replace / merge / patch（patch 可重排序）
- ID 由服务端生成，格式统一为 task-{8hex}
- 依赖关系与 blocked 自动计算
- sync_status() 对齐执行结果与任务状态
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

from app.database.pool import get_connection, release_connection

logger = logging.getLogger(__name__)

# ── Redis key 与 TTL ────────────────────────────────────────────────────────
# L1：路由层按 turn 存储，7 天
TASK_L1_KEY   = "task:{user_id}:l1:{turn_id}"
# L2：执行层按 agent+执行 ID 存储，1 天（执行完即可过期）
TASK_L2_KEY   = "task:{user_id}:l2:{agent_name}:{exec_id}"
TASK_L1_TTL   = 7 * 86_400
TASK_L2_TTL   = 1 * 86_400

# 向后兼容旧键（无分级时使用）
TASK_LIST_KEY = "task:{user_id}:list"
TASK_LIST_TTL = 7 * 86_400


# ════════════════════════════════════════════════════════════════════════════
# 提示词常量（所有面向 LLM 的文本集中在此，方便独立调整）
# ════════════════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════════════════
# L1 提示词：路由层 → Agent 级拆分
# 目标：将用户目标拆解为若干 Agent 子任务，每条任务对应一个 Agent
# ════════════════════════════════════════════════════════════════════════════

L1_DECOMPOSE_SYSTEM = """\
你是一个多智能体任务规划器，负责将用户目标分配给合适的 Agent。

可用 Agent 列表：
{agents_info}

拆分原则
────────
1. 每条子任务对应且仅对应一个 Agent，粒度为"一个 Agent 完整负责的工作块"。
2. 明确任务间的依赖：后续任务依赖前序任务的输出时，在 depends_on_indices 中标注。
3. 合理选 Agent：根据 Agent 的职责描述选择最匹配的；无专门 Agent 时选 general_assistant。
4. 任务描述要具体：写明"用什么数据/来源"、"做什么操作"、"输出什么"，不能只写"分析数据"。
5. 若目标简单、只需一个 Agent，则只输出一条任务。

输出格式（严格 JSON 数组，不要有其他文字）
──────────────────────────────────────────
[
  {{
    "agent_name": "负责此任务的 Agent 名称（必须来自可用列表）",
    "description": "清晰完整的任务描述，包含：做什么、用什么数据/工具、产出什么",
    "tags": ["领域标签，如 data/analysis/report/web/code"],
    "depends_on_indices": [前置任务在本数组中的索引（0-based），无依赖则为空数组]
  }},
  ...
]
"""

L1_DECOMPOSE_USER_TMPL = """\
用户目标：{goal}

{context_block}
请将上述目标拆解为 Agent 级子任务，按执行先后排列，严格输出 JSON 数组。\
"""

# ════════════════════════════════════════════════════════════════════════════
# L2 提示词：执行层 → 可执行步骤拆分
# 目标：Agent 将分配到的任务拆解为最小可执行操作序列
# ════════════════════════════════════════════════════════════════════════════

L2_DECOMPOSE_SYSTEM = """\
你是一个任务执行规划器，负责将分配给你的任务拆解为可逐步执行的操作序列。

拆分原则
────────
1. 粒度：每个步骤是一个原子操作，2 分钟内可完成，不可再拆分。
   正确示例："调用 search_api('关键词') 并将结果缓存到 temp_data"
   错误示例："搜索并分析所有相关数据"（太大，包含多个动作）

2. 失败处理：步骤描述中需注明失败时的行为：
   - 硬性前提（如网络连接）：失败则终止整个任务（标记 on_fail=terminate）
   - 非必要步骤：失败则跳过并记录（标记 on_fail=skip）
   - 可重试步骤：最多重试 N 次（标记 on_fail=retry:N）

3. 数据流：后续步骤依赖前序步骤输出时，在描述中明确说明"使用步骤 X 的输出"，
   并在 depends_on_indices 中标注。

4. 可验证：每个步骤的描述应包含可检查的完成条件（"直到收到 HTTP 200"、"直到缓存非空"等）。

输出格式（严格 JSON 数组，不要有其他文字）
──────────────────────────────────────────
[
  {{
    "description": "具体的操作描述，含完成条件和失败处理说明",
    "on_fail": "terminate | skip | retry:N",
    "tags": ["步骤类型标签，如 network/cache/parse/validate/output"],
    "depends_on_indices": [前置步骤在本数组中的索引，无依赖则为空数组]
  }},
  ...
]
"""

L2_DECOMPOSE_USER_TMPL = """\
分配给你的任务：{task_description}

当前可用工具：{tools_info}

{context_block}
请将上述任务拆解为可执行步骤，按操作顺序排列，严格输出 JSON 数组。\
"""

# ── 通用自动拆分提示词（向后兼容，无分级场景） ──────────────────────────────

# ── 自动拆分提示词 ───────────────────────────────────────────────────────────

DECOMPOSE_SYSTEM = """\
你是一个任务规划专家，负责将用户的目标拆解为可执行的子任务列表。

拆分原则
────────
1. 粒度：每条子任务应为 2-5 分钟内可完成的单一动作，不可再拆分的最小执行单元。
   正确示例："为 User 模型添加 email 字段并编写单元测试"
   错误示例："实现用户认证系统"（太大）或"在第 23 行加一个分号"（太细）

2. 顺序：按执行先后排列，有依赖的任务标注 depends_on。
   基础设施/模型层 → 服务层 → API 层 → 测试/文档。

3. 原子性：每条任务只做一件事。若需要先读后写，拆为两条。

4. 可验证：任务描述应包含隐含的验收标准，让执行者知道何时算完成。

输出格式（严格 JSON，不要有其他文本）
─────────────────────────────────────
[
  {
    "content": "任务描述（完整、可直接执行）",
    "tags": ["类型标签，如 backend/frontend/test/infra/doc"],
    "depends_on_indices": [前置任务在本数组中的索引，0-based，无依赖则为空数组]
  },
  ...
]
"""

DECOMPOSE_USER_TMPL = """\
目标：{goal}

{context_block}
请将上述目标拆解为子任务列表，按执行顺序排列，严格输出 JSON 数组，不要加任何注释或说明文字。\
"""

# ── 增量重规划提示词（任务执行中途信息变化时使用）──────────────────────────

REPLAN_SYSTEM = """\
你是一个任务重规划专家。当前任务列表的执行过程中出现了新情况，需要对未完成的任务进行调整。

调整规则
────────
1. 已完成（completed）和已取消（cancelled）的任务不得修改。
2. 只输出需要变更的任务，未变更的任务不输出。
3. 可以新增任务（action=add）、修改未完成任务的描述或优先级（action=update）、取消任务（action=cancel）。
4. 不允许删除任务，只能取消（cancelled 状态）。
5. 所有修改必须有明确理由（reason 字段）。

输出格式（严格 JSON）
─────────────────────
[
  {
    "action": "add | update | cancel",
    "task_id": "现有任务的 ID（add 时省略）",
    "content": "新的任务描述（cancel 时省略）",
    "priority": 新的优先级整数（越小越优先，update 时可选）,
    "reason": "变更原因"
  },
  ...
]
"""

REPLAN_USER_TMPL = """\
当前任务列表：
{task_snapshot}

新情况说明：
{new_info}

请输出需要变更的操作列表，严格 JSON，不要加任何说明文字。\
"""

# ── 上下文压缩后的任务注入前缀 ──────────────────────────────────────────────

INJECT_HEADER = "[任务列表已从上下文压缩中恢复，以下为仍需完成的任务]"

# ── 任务状态同步检查提示词（可选：用 LLM 辅助核对状态） ───────────────────

SYNC_CHECK_SYSTEM = """\
你是一个任务状态核查员。根据执行日志核对任务列表中每条任务的完成状态，只输出状态需要修正的任务。

输出格式（严格 JSON）
─────────────────────
[
  {
    "task_id": "任务 ID",
    "correct_status": "completed | cancelled | in_progress | pending | blocked",
    "reason": "状态修正依据"
  },
  ...
]
如所有任务状态均正确，输出空数组 []。
"""

SYNC_CHECK_USER_TMPL = """\
任务列表：
{task_snapshot}

执行日志摘要：
{execution_log}

请输出需要修正状态的任务，严格 JSON。\
"""


# ════════════════════════════════════════════════════════════════════════════
# 数据模型
# ════════════════════════════════════════════════════════════════════════════

class TaskStatus(str, Enum):
    PENDING     = "pending"      # 等待执行
    IN_PROGRESS = "in_progress"  # 执行中（同时只允许一条）
    COMPLETED   = "completed"    # 已完成
    CANCELLED   = "cancelled"    # 已取消
    BLOCKED     = "blocked"      # 因依赖未完成而阻塞


_STATUS_MARKER = {
    TaskStatus.COMPLETED:   "[x]",
    TaskStatus.IN_PROGRESS: "[>]",
    TaskStatus.PENDING:     "[ ]",
    TaskStatus.CANCELLED:   "[~]",
    TaskStatus.BLOCKED:     "[!]",
}

_IMMUTABLE_STATUSES = {TaskStatus.COMPLETED, TaskStatus.CANCELLED}


@dataclass
class TaskItem:
    """单条任务。ID 由服务端 `_new_id()` 生成，LLM 不直接赋值。"""

    task_id:    str
    content:    str
    status:     TaskStatus   = TaskStatus.PENDING
    priority:   int          = 0          # 数值越小优先级越高，列表按此排序
    tags:       List[str]    = field(default_factory=list)
    depends_on: List[str]    = field(default_factory=list)   # task_id 列表
    created_at: str          = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str          = field(default_factory=lambda: datetime.now().isoformat())

    # ── 序列化 ──────────────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TaskItem":
        d = dict(d)
        d["status"] = TaskStatus(d.get("status", "pending"))
        return cls(**d)

    # ── 展示 ────────────────────────────────────────────────────────────────

    def format_line(self) -> str:
        marker = _STATUS_MARKER.get(self.status, "[?]")
        tag_str = f" [{'/'.join(self.tags)}]" if self.tags else ""
        return f"{marker} {self.task_id}.{tag_str} {self.content}"


# ════════════════════════════════════════════════════════════════════════════
# 任务存储（Redis 持久化 + 内存写透缓存）
# ════════════════════════════════════════════════════════════════════════════

class TaskStore:
    """用户维度的任务列表，持久化到 Redis。

    写入模式
    ────────
    replace：全量替换，适用于新建计划或彻底推翻重写。
    merge  ：按 task_id 匹配更新内容/状态，不存在的 task_id 追加末尾；
             不改变未提交任务的顺序（优先级），修复了 hermes-agent merge 不能重排的问题
             → 如需改顺序请使用 patch 模式。
    patch  ：只更新指定字段（content / status / priority），支持 priority 重排序，
             其余字段保持不变。

    在 replace/merge 时，新任务的 task_id 由本类生成，LLM 提供的 id 字段被忽略。
    patch 时 task_id 必须已存在。
    """

    def __init__(
        self,
        user_id:   str,
        redis_key: Optional[str] = None,
        ttl:       int           = TASK_LIST_TTL,
    ) -> None:
        self.user_id = user_id
        self._key    = redis_key or TASK_LIST_KEY.format(user_id=user_id)
        self._ttl    = ttl
        self._cache: Optional[List[TaskItem]] = None   # 写透缓存

    # ── 读取 ─────────────────────────────────────────────────────────────────

    async def read(self) -> List[TaskItem]:
        if self._cache is not None:
            return list(self._cache)
        return await self._load()

    # ── 写入（replace 模式） ─────────────────────────────────────────────────

    async def replace(self, items: Sequence[Dict[str, Any]]) -> List[TaskItem]:
        """全量替换任务列表，忽略传入的 task_id，全部重新分配。"""
        now = datetime.now().isoformat()
        new_list: List[TaskItem] = []
        for i, raw in enumerate(items):
            new_list.append(TaskItem(
                task_id    = _new_id(),
                content    = str(raw.get("content", "")).strip() or "(无描述)",
                status     = TaskStatus.PENDING,
                priority   = i,
                tags       = list(raw.get("tags", [])),
                depends_on = [],   # replace 时依赖关系在拆分阶段已处理
                created_at = now,
                updated_at = now,
            ))
        await self._save(new_list)
        return list(new_list)

    # ── 写入（merge 模式）────────────────────────────────────────────────────

    async def merge(self, items: Sequence[Dict[str, Any]]) -> List[TaskItem]:
        """按 task_id 合并：存在的更新 content/status，不存在的追加到末尾。
        已完成/已取消的任务不可通过 merge 修改状态。
        传入项若无 task_id（或 task_id 不在列表中），视为新任务并分配新 ID。
        """
        current = await self._load()
        index   = {t.task_id: t for t in current}
        now     = datetime.now().isoformat()

        for raw in items:
            tid = str(raw.get("task_id", "")).strip()
            if tid and tid in index:
                existing = index[tid]
                if existing.status in _IMMUTABLE_STATUSES:
                    continue
                if "content" in raw and raw["content"]:
                    existing.content = str(raw["content"]).strip()
                if "status" in raw:
                    try:
                        new_st = TaskStatus(raw["status"])
                        if new_st not in _IMMUTABLE_STATUSES:
                            existing.status = new_st
                    except ValueError:
                        pass
                existing.updated_at = now
            else:
                # 新任务：分配新 ID，优先级排到末尾
                new_item = TaskItem(
                    task_id    = _new_id(),
                    content    = str(raw.get("content", "")).strip() or "(无描述)",
                    status     = TaskStatus.PENDING,
                    priority   = max((t.priority for t in current), default=-1) + 1,
                    tags       = list(raw.get("tags", [])),
                    depends_on = [],
                    created_at = now,
                    updated_at = now,
                )
                current.append(new_item)
                index[new_item.task_id] = new_item

        _sort_by_priority(current)
        _recompute_blocked(current)
        await self._save(current)
        return list(current)

    # ── 写入（patch 模式）────────────────────────────────────────────────────

    async def patch(self, patches: Sequence[Dict[str, Any]]) -> List[TaskItem]:
        """只更新指定字段，支持修改 priority 实现重排序。
        task_id 不存在的补丁被跳过并记录 warning。
        已完成/已取消的任务：允许修改 priority（不影响逻辑状态），其余字段拒绝。
        """
        current = await self._load()
        index   = {t.task_id: t for t in current}
        now     = datetime.now().isoformat()

        for p in patches:
            tid = str(p.get("task_id", "")).strip()
            if tid not in index:
                logger.warning("[TaskStore] patch: task_id=%s 不存在，跳过", tid)
                continue

            task = index[tid]
            immutable = task.status in _IMMUTABLE_STATUSES

            if "priority" in p:
                try:
                    task.priority   = int(p["priority"])
                    task.updated_at = now
                except (TypeError, ValueError):
                    pass

            if immutable:
                continue

            if "content" in p and p["content"]:
                task.content    = str(p["content"]).strip()
                task.updated_at = now
            if "status" in p:
                try:
                    new_st = TaskStatus(p["status"])
                    if new_st not in _IMMUTABLE_STATUSES:
                        task.status     = new_st
                        task.updated_at = now
                except ValueError:
                    pass
            if "tags" in p:
                task.tags       = list(p["tags"])
                task.updated_at = now
            if "depends_on" in p:
                task.depends_on = list(p["depends_on"])
                task.updated_at = now

        _sort_by_priority(current)
        _recompute_blocked(current)
        await self._save(current)
        return list(current)

    # ── 单任务状态更新（快捷方法）────────────────────────────────────────────

    async def set_status(self, task_id: str, status: TaskStatus) -> bool:
        """将单条任务设为指定状态，返回是否找到并更新了该任务。"""
        current = await self._load()
        found   = False
        for t in current:
            if t.task_id == task_id:
                if t.status not in _IMMUTABLE_STATUSES or status == TaskStatus.CANCELLED:
                    t.status     = status
                    t.updated_at = datetime.now().isoformat()
                    found = True
                break
        if found:
            _recompute_blocked(current)
            await self._save(current)
        return found

    # ── 依赖关系同步（执行结果反向对齐）─────────────────────────────────────

    async def sync_status(self, completed_ids: Sequence[str], failed_ids: Sequence[str]) -> List[TaskItem]:
        """根据实际执行结果更正任务状态，解决状态与代码实际进度脱节的问题。

        completed_ids：实际已完成的 task_id 列表
        failed_ids   ：实际执行失败、应回退为 pending 的 task_id 列表
        """
        current = await self._load()
        now = datetime.now().isoformat()
        done_set = set(completed_ids)
        fail_set = set(failed_ids)

        for t in current:
            if t.task_id in done_set and t.status != TaskStatus.COMPLETED:
                t.status     = TaskStatus.COMPLETED
                t.updated_at = now
            elif t.task_id in fail_set and t.status in (TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED):
                t.status     = TaskStatus.PENDING
                t.updated_at = now

        _recompute_blocked(current)
        await self._save(current)
        return list(current)

    # ── 上下文压缩注入 ───────────────────────────────────────────────────────

    async def format_for_injection(self) -> Optional[str]:
        """生成适合注入到压缩后上下文的任务摘要，只包含未完成任务。
        与 hermes-agent 相同：completed/cancelled 不注入，避免 LLM 重做已完成工作。
        """
        current = await self._load()
        active  = [t for t in current if t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)]
        if not active:
            return None

        lines = [INJECT_HEADER]
        for t in active:
            lines.append(t.format_line())
        return "\n".join(lines)

    # ── 查询辅助 ────────────────────────────────────────────────────────────

    async def get_active(self) -> List[TaskItem]:
        """返回 pending + in_progress + blocked 的任务列表（按 priority 排序）。"""
        return [t for t in await self.read() if t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)]

    async def summary(self) -> Dict[str, int]:
        """返回各状态的任务计数。"""
        items = await self.read()
        counts: Dict[str, int] = {s.value: 0 for s in TaskStatus}
        for t in items:
            counts[t.status.value] += 1
        counts["total"] = len(items)
        return counts

    # ── Redis I/O（私有）────────────────────────────────────────────────────

    async def _load(self) -> List[TaskItem]:
        if self._cache is not None:
            return list(self._cache)

        conn = None
        try:
            conn = await get_connection("redis", None)
            if not conn or not conn.redis_client:
                self._cache = []
                return []
            raw = conn.redis_client.get(self._key)
            if not raw:
                self._cache = []
                return []
            data = json.loads(raw)
            items = [TaskItem.from_dict(d) for d in data]
            self._cache = items
            return list(items)
        except Exception as e:
            logger.warning("[TaskStore] 从 Redis 加载任务失败 user=%s: %s", self.user_id, e)
            self._cache = []
            return []
        finally:
            if conn:
                await release_connection("redis", conn)

    async def _save(self, items: List[TaskItem]) -> None:
        self._cache = list(items)
        conn = None
        try:
            conn = await get_connection("redis", None)
            if not conn or not conn.redis_client:
                return
            payload = json.dumps([t.to_dict() for t in items], ensure_ascii=False)
            conn.redis_client.set(self._key, payload, ex=self._ttl)
        except Exception as e:
            logger.warning("[TaskStore] 保存任务到 Redis 失败 user=%s: %s", self.user_id, e)
        finally:
            if conn:
                await release_connection("redis", conn)


# ════════════════════════════════════════════════════════════════════════════
# 任务自动分解器（LLM 驱动）
# ════════════════════════════════════════════════════════════════════════════

class TaskDecomposer:
    """使用 LLM 将复杂目标拆分为子任务列表，并写入 TaskStore。

    解决了 hermes-agent 原方案中"拆分质量完全依赖模型主动行为"的问题：
    通过专用提示词约束粒度、顺序、依赖关系，并由服务端分配 ID。
    """

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    async def decompose(
        self,
        goal:    str,
        llm:     Any,
        context: Optional[str] = None,
    ) -> List[TaskItem]:
        """将 goal 拆解为子任务，写入 TaskStore（replace 模式）后返回任务列表。

        Args:
            goal:    用户描述的目标（自然语言）
            llm:     LangChain BaseChatModel 实例
            context: 可选的背景信息（项目结构、已知约束等）
        """
        context_block = f"背景信息：\n{context}" if context else ""
        prompt        = DECOMPOSE_USER_TMPL.format(goal=goal, context_block=context_block)

        raw_items = await self._call_llm_structured(
            system=DECOMPOSE_SYSTEM,
            user=prompt,
            llm=llm,
        )
        if not raw_items:
            logger.warning("[TaskDecomposer] 分解结果为空，goal=%s", goal[:60])
            return []

        # 处理 depends_on_indices → depends_on task_id
        # 先 replace 写入（获取 task_id），再 patch 写入依赖
        tasks = await self._store.replace(raw_items)
        id_map = [t.task_id for t in tasks]

        patches = []
        for i, raw in enumerate(raw_items):
            dep_indices = raw.get("depends_on_indices", [])
            if dep_indices and i < len(id_map):
                dep_ids = [id_map[j] for j in dep_indices if isinstance(j, int) and 0 <= j < len(id_map)]
                if dep_ids:
                    patches.append({"task_id": id_map[i], "depends_on": dep_ids})

        if patches:
            tasks = await self._store.patch(patches)

        logger.info(
            "[TaskDecomposer] 目标拆解完成 user=%s tasks=%d goal=%s",
            self._store.user_id, len(tasks), goal[:60],
        )
        return tasks

    async def replan(
        self,
        new_info: str,
        llm:      Any,
    ) -> List[TaskItem]:
        """根据新情况对未完成任务进行增量调整，已完成/取消的任务不受影响。

        new_info：新情况描述（错误信息、需求变更、外部约束变化等）
        """
        current = await self._store.read()
        snapshot = _format_snapshot(current)

        prompt = REPLAN_USER_TMPL.format(task_snapshot=snapshot, new_info=new_info)
        actions = await self._call_llm_structured(
            system=REPLAN_SYSTEM,
            user=prompt,
            llm=llm,
        )
        if not actions:
            return current

        patches_update: List[Dict[str, Any]] = []
        new_items: List[Dict[str, Any]] = []

        for action in actions:
            act = str(action.get("action", "")).strip()
            if act == "add":
                new_items.append({
                    "content": action.get("content", ""),
                    "tags":    action.get("tags", []),
                })
            elif act == "update":
                p: Dict[str, Any] = {"task_id": action.get("task_id", "")}
                if "content"  in action: p["content"]  = action["content"]
                if "priority" in action: p["priority"] = action["priority"]
                if p["task_id"]:
                    patches_update.append(p)
            elif act == "cancel":
                tid = action.get("task_id", "")
                if tid:
                    patches_update.append({"task_id": tid, "status": TaskStatus.CANCELLED.value})

        if patches_update:
            await self._store.patch(patches_update)
        if new_items:
            await self._store.merge(new_items)

        result = await self._store.read()
        logger.info(
            "[TaskDecomposer] 重规划完成 user=%s actions=%d",
            self._store.user_id, len(actions),
        )
        return result

    async def sync_check(
        self,
        execution_log: str,
        llm:           Any,
    ) -> List[TaskItem]:
        """借助 LLM 核查执行日志，修正任务状态与实际进度的偏差。（可选步骤）"""
        current  = await self._store.read()
        snapshot = _format_snapshot(current)

        prompt = SYNC_CHECK_USER_TMPL.format(
            task_snapshot=snapshot,
            execution_log=execution_log,
        )
        corrections = await self._call_llm_structured(
            system=SYNC_CHECK_SYSTEM,
            user=prompt,
            llm=llm,
        )
        if not corrections:
            return current

        patches = [
            {"task_id": c["task_id"], "status": c["correct_status"]}
            for c in corrections if c.get("task_id") and c.get("correct_status")
        ]
        if patches:
            await self._store.patch(patches)

        return await self._store.read()

    # ── LLM 调用（私有）──────────────────────────────────────────────────────

    @staticmethod
    async def _call_llm_structured(
        system: str,
        user:   str,
        llm:    Any,
    ) -> List[Dict[str, Any]]:
        """调用 LLM，要求返回 JSON 数组。解析失败时返回空列表。"""
        from langchain_core.messages import HumanMessage, SystemMessage

        try:
            resp = await llm.ainvoke([
                SystemMessage(content=system),
                HumanMessage(content=user),
            ])
            text = resp.content if hasattr(resp, "content") else str(resp)
            return _parse_json_array(text)
        except Exception as e:
            logger.error("[TaskDecomposer] LLM 调用失败: %s", e)
            return []


# ════════════════════════════════════════════════════════════════════════════
# 便捷工厂函数（供 hermes_engine / agents 调用）
# ════════════════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════════════════
# L1 分解器：路由层使用，将用户目标拆解为 Agent 级子任务
# ════════════════════════════════════════════════════════════════════════════

class L1Decomposer:
    """路由层调用，产出 [TaskItem(agent_name=..., description=...)]。
    分解粒度 = 一个 Agent 完整负责的工作块。
    """

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    async def decompose(
        self,
        goal:        str,
        agents_info: str,
        llm:         Any,
        context:     Optional[str] = None,
    ) -> List[TaskItem]:
        """将用户目标拆分为 Agent 级子任务，写入 L1 TaskStore 后返回。

        Args:
            goal:        用户目标（自然语言）
            agents_info: 可用 Agent 的描述字符串（name | role | 职责摘要）
            llm:         LangChain BaseChatModel
            context:     可选背景信息
        """
        context_block = f"背景信息：\n{context}" if context else ""
        system = L1_DECOMPOSE_SYSTEM.format(agents_info=agents_info)
        user   = L1_DECOMPOSE_USER_TMPL.format(goal=goal, context_block=context_block)

        raw_items = await TaskDecomposer._call_llm_structured(system, user, llm)
        if not raw_items:
            logger.warning("[L1Decomposer] 分解结果为空，goal=%s", goal[:60])
            return []

        # replace 写入，获取服务端分配的 task_id
        tasks  = await self._store.replace(raw_items)
        id_map = [t.task_id for t in tasks]

        # 写入 agent_name 标签（存入 tags 字段，供 router 读取）
        patches = []
        for i, (raw, t) in enumerate(zip(raw_items, tasks)):
            agent = str(raw.get("agent_name", "")).strip()
            dep_indices = raw.get("depends_on_indices", [])
            dep_ids = [id_map[j] for j in dep_indices if isinstance(j, int) and 0 <= j < len(id_map)]

            p: Dict[str, Any] = {"task_id": t.task_id}
            new_tags = [f"agent:{agent}"] + list(raw.get("tags", []))
            p["tags"]       = new_tags
            p["depends_on"] = dep_ids
            patches.append(p)

        if patches:
            tasks = await self._store.patch(patches)

        logger.info(
            "[L1Decomposer] 目标拆解为 %d 个 Agent 任务 user=%s goal=%s",
            len(tasks), self._store.user_id, goal[:60],
        )
        return tasks

    def get_agent_name(self, task: TaskItem) -> str:
        """从 tags 中解析 agent_name（格式为 agent:{name}）。"""
        for tag in task.tags:
            if tag.startswith("agent:"):
                return tag[len("agent:"):]
        return "general_assistant"


# ════════════════════════════════════════════════════════════════════════════
# L2 分解器：执行层使用，将 Agent 任务拆解为可执行操作步骤
# ════════════════════════════════════════════════════════════════════════════

class L2Decomposer:
    """执行 Agent 调用，产出 [TaskItem(description=步骤描述, tags=[on_fail:...])]。
    分解粒度 = 单一原子操作。
    """

    _ON_FAIL_TAG_PREFIX = "on_fail:"

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    async def decompose(
        self,
        task_description: str,
        llm:              Any,
        tools_info:       str           = "（无可用工具列表）",
        context:          Optional[str] = None,
    ) -> List[TaskItem]:
        """将 Agent 任务拆分为可执行步骤，写入 L2 TaskStore 后返回。

        Args:
            task_description: Agent 收到的任务描述（来自 L1 任务的 content）
            llm:              LangChain BaseChatModel
            tools_info:       当前可用工具列表描述（供 LLM 参考粒度）
            context:          可选额外背景
        """
        context_block = f"额外背景：\n{context}" if context else ""
        user = L2_DECOMPOSE_USER_TMPL.format(
            task_description=task_description,
            tools_info=tools_info,
            context_block=context_block,
        )
        raw_items = await TaskDecomposer._call_llm_structured(L2_DECOMPOSE_SYSTEM, user, llm)
        if not raw_items:
            logger.warning("[L2Decomposer] 步骤拆解为空，task=%s", task_description[:60])
            return []

        tasks  = await self._store.replace(raw_items)
        id_map = [t.task_id for t in tasks]

        patches = []
        for i, (raw, t) in enumerate(zip(raw_items, tasks)):
            on_fail = str(raw.get("on_fail", "skip")).strip()
            dep_indices = raw.get("depends_on_indices", [])
            dep_ids = [id_map[j] for j in dep_indices if isinstance(j, int) and 0 <= j < len(id_map)]

            patches.append({
                "task_id":    t.task_id,
                "tags":       [f"{self._ON_FAIL_TAG_PREFIX}{on_fail}"] + list(raw.get("tags", [])),
                "depends_on": dep_ids,
            })

        if patches:
            tasks = await self._store.patch(patches)

        logger.info(
            "[L2Decomposer] 任务拆解为 %d 个执行步骤 user=%s task=%s",
            len(tasks), self._store.user_id, task_description[:60],
        )
        return tasks

    def get_on_fail(self, task: TaskItem) -> str:
        """从 tags 中解析 on_fail 策略（terminate / skip / retry:N）。"""
        for tag in task.tags:
            if tag.startswith(self._ON_FAIL_TAG_PREFIX):
                return tag[len(self._ON_FAIL_TAG_PREFIX):]
        return "skip"

    def should_terminate(self, task: TaskItem) -> bool:
        """任务失败时是否应终止整个执行链。"""
        return self.get_on_fail(task) == "terminate"

    def get_retry_count(self, task: TaskItem) -> int:
        """返回重试次数，非 retry 策略时返回 0。"""
        policy = self.get_on_fail(task)
        if policy.startswith("retry:"):
            try:
                return int(policy.split(":")[1])
            except (IndexError, ValueError):
                return 1
        return 0


def make_task_planner(user_id: str) -> tuple[TaskStore, TaskDecomposer]:
    """返回 (TaskStore, TaskDecomposer) 二元组，绑定到指定 user_id（向后兼容）。"""
    store = TaskStore(user_id)
    return store, TaskDecomposer(store)


def make_l1_store(user_id: str, turn_id: str) -> tuple[TaskStore, "L1Decomposer"]:
    """L1（Agent 级）任务存储 + 分解器。由 RouterAgent 在路由阶段调用。"""
    key   = TASK_L1_KEY.format(user_id=user_id, turn_id=turn_id)
    store = TaskStore(user_id, redis_key=key, ttl=TASK_L1_TTL)
    return store, L1Decomposer(store)


def make_l2_store(user_id: str, agent_name: str, exec_id: str) -> tuple[TaskStore, "L2Decomposer"]:
    """L2（可执行级）任务存储 + 分解器。由各执行 Agent 在任务开始时调用。"""
    key   = TASK_L2_KEY.format(user_id=user_id, agent_name=agent_name, exec_id=exec_id)
    store = TaskStore(user_id, redis_key=key, ttl=TASK_L2_TTL)
    return store, L2Decomposer(store)


# ════════════════════════════════════════════════════════════════════════════
# 私有工具函数
# ════════════════════════════════════════════════════════════════════════════

def _new_id() -> str:
    """生成格式为 task-{8位十六进制} 的任务 ID，服务端统一分配，避免 LLM 随意命名。"""
    return f"task-{uuid.uuid4().hex[:8]}"


def _sort_by_priority(items: List[TaskItem]) -> None:
    """原地按 priority 升序排序（priority 值越小越靠前）。"""
    items.sort(key=lambda t: t.priority)


def _recompute_blocked(items: List[TaskItem]) -> None:
    """根据 depends_on 重新计算 blocked 状态。
    若某任务的所有前置依赖均已 completed，则解除 blocked → pending。
    若依赖未完成且任务当前为 pending，则标记为 blocked。
    """
    completed = {t.task_id for t in items if t.status == TaskStatus.COMPLETED}
    for t in items:
        if not t.depends_on:
            if t.status == TaskStatus.BLOCKED:
                t.status = TaskStatus.PENDING
            continue
        all_done = all(dep in completed for dep in t.depends_on)
        if all_done and t.status == TaskStatus.BLOCKED:
            t.status = TaskStatus.PENDING
        elif not all_done and t.status == TaskStatus.PENDING:
            t.status = TaskStatus.BLOCKED


def _format_snapshot(items: List[TaskItem]) -> str:
    """将任务列表序列化为适合传给 LLM 的文本摘要。"""
    if not items:
        return "(空)"
    lines = []
    for t in items:
        dep_str = f" 依赖: {t.depends_on}" if t.depends_on else ""
        lines.append(f"[{t.task_id}] {_STATUS_MARKER.get(t.status, '?')} {t.content}{dep_str}")
    return "\n".join(lines)


def _parse_json_array(text: str) -> List[Dict[str, Any]]:
    """从 LLM 响应文本中提取第一个 JSON 数组，解析失败返回空列表。"""
    import re
    # 优先匹配 ```json ... ``` 围栏
    m = re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", text)
    if m:
        text = m.group(1)
    else:
        # 找第一个 [ 到最后一个 ]
        start = text.find("[")
        end   = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return []
        text = text[start:end + 1]
    try:
        result = json.loads(text)
        return result if isinstance(result, list) else []
    except json.JSONDecodeError as e:
        logger.warning("[TaskDecomposer] JSON 解析失败: %s | text=%s", e, text[:200])
        return []
