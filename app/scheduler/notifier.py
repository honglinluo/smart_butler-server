"""
【模块说明】定时任务通知器（Notifier）— 把任务结果"推送给用户"

定时任务执行完后，用户需要知道结果（"你的周报已生成完毕"或"该开会了"）。
这个模块负责把通知消息放入队列，再通过两种方式送达用户：

【两种获取通知的方式】
  1. 轮询（polling）：前端每隔几秒主动来问"有新消息吗？" → pop_notifications()
  2. SSE 长连接：前端保持一个持续连接，服务器有消息就实时推送 → sse_notification_stream()

【消息队列机制（Redis List）】
  任务执行完 → 消息写入 Redis → 用户下次来取时弹出
  - 消息存活 24 小时，过期自动清除
  - FIFO 顺序（先进先出）：按执行完成顺序排队

【两种通知消息格式】
  make_reminder_payload()    — 提醒类通知（"该做某事了"）
  make_agent_done_payload()  — AI 任务完成通知（附带执行结果摘要）

定时任务通知器 — Redis 消息队列 + SSE/轮询推送。

架构：
  runner 执行完任务 → push_notification() 将消息 RPUSH 到 Redis List（FIFO 队列尾）
  客户端轮询        → pop_notifications() 用 LPOP 从队列头消费
  客户端 SSE 流     → sse_notification_stream() 轮询 Redis，有消息则推送

Redis key 格式（与 redis_keys.py 风格一致）：
  notify:{user_id}:pending   — 通知队列，TTL 24h
"""

import json
import logging
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

from app.database.pool import get_connection, release_connection

logger = logging.getLogger(__name__)

NOTIFY_TTL = 86_400  # 24 h
NOTIFY_KEY = "notify:{user_id}:pending"


def _key(user_id: str) -> str:
    return NOTIFY_KEY.format(user_id=user_id)


async def push_notification(user_id: str, payload: Dict[str, Any]) -> None:
    """向用户通知队列追加一条消息（RPUSH，FIFO 队尾写入）。"""
    redis_conn = None
    try:
        redis_conn = await get_connection("redis", None)
        key  = _key(user_id)
        data = json.dumps(payload, ensure_ascii=False, default=str)
        # 直接使用底层 redis_client（同步 redis-py）
        redis_conn.redis_client.rpush(key, data)
        redis_conn.redis_client.expire(key, NOTIFY_TTL)
    except Exception as e:
        logger.error("推送通知失败 user=%s: %s", user_id, e)
    finally:
        if redis_conn:
            await release_connection("redis", redis_conn)


async def pop_notifications(user_id: str, max_count: int = 20) -> List[Dict[str, Any]]:
    """批量弹出用户待读通知（LPOP 消费，弹出后删除）。"""
    redis_conn = None
    try:
        redis_conn = await get_connection("redis", None)
        key    = _key(user_id)
        client = redis_conn.redis_client
        result = []
        for _ in range(max_count):
            raw = client.lpop(key)
            if raw is None:
                break
            try:
                result.append(json.loads(raw))
            except Exception:
                pass
        return result
    except Exception as e:
        logger.error("弹出通知失败 user=%s: %s", user_id, e)
        return []
    finally:
        if redis_conn:
            await release_connection("redis", redis_conn)


async def peek_notifications(user_id: str, count: int = 20) -> List[Dict[str, Any]]:
    """预览通知（LRANGE，不消费）。"""
    redis_conn = None
    try:
        redis_conn = await get_connection("redis", None)
        key    = _key(user_id)
        items  = redis_conn.redis_client.lrange(key, 0, count - 1)
        result = []
        for raw in items:
            try:
                result.append(json.loads(raw))
            except Exception:
                pass
        return result
    except Exception as e:
        logger.error("预览通知失败 user=%s: %s", user_id, e)
        return []
    finally:
        if redis_conn:
            await release_connection("redis", redis_conn)


def make_reminder_payload(
    task_id:   str,
    task_name: str,
    user_id:   str,
    text:      str,
) -> Dict[str, Any]:
    return {
        "type":      "reminder",
        "task_id":   task_id,
        "task_name": task_name,
        "user_id":   user_id,
        "text":      text,
        "at":        datetime.utcnow().isoformat(),
    }


def make_agent_done_payload(
    task_id:    str,
    task_name:  str,
    user_id:    str,
    agent_name: str,
    output:     str,
    success:    bool,
) -> Dict[str, Any]:
    return {
        "type":       "agent_done",
        "task_id":    task_id,
        "task_name":  task_name,
        "user_id":    user_id,
        "agent_name": agent_name,
        "success":    success,
        "output":     output[:512] if output else "",
        "at":         datetime.utcnow().isoformat(),
    }


async def sse_notification_stream(
    user_id: str,
    poll_interval: float = 3.0,
    max_idle_cycles: int = 200,  # ~10 分钟后断开空闲连接
) -> AsyncIterator[str]:
    """SSE 生成器：轮询 Redis，有消息则推送，超时后自动关闭。"""
    import asyncio

    idle = 0
    while idle < max_idle_cycles:
        msgs = await pop_notifications(user_id, max_count=10)
        if msgs:
            idle = 0
            for msg in msgs:
                data = json.dumps(msg, ensure_ascii=False, default=str)
                yield f"event: notification\ndata: {data}\n\n"
        else:
            idle += 1
            yield ": ping\n\n"  # 保活心跳
        await asyncio.sleep(poll_interval)
    yield "event: close\ndata: {}\n\n"
