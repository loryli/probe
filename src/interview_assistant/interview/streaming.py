"""实时通道：内存 LiveStream、后台决策任务与 SSE 帧转发。

当前实现是**单进程内存通道**。多 worker 部署时只需把 StreamBroker 换成
共享实现（Redis、事件表等），路由与服务层不需要改动；断线重放中间文字
需要额外的事件持久化，本模块不承诺该能力。

这里只处理传输与任务存活，不决定面试状态：所有业务写入仍由
InterviewService 在自己的事务里完成。为避免模块循环导入，服务类型在
函数内部延迟导入。
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from sqlalchemy.exc import SQLAlchemyError

from interview_assistant.ai.qwen import ModelError
from interview_assistant.config import Settings
from interview_assistant.database import Database
from interview_assistant.interview.errors import InterviewError
from interview_assistant.interview.schemas import SessionRead

logger = logging.getLogger(__name__)

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
LIVE_TTL_SEC = 120


def sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


@dataclass(slots=True)
class LiveStream:
    """一次决策的实时状态：已产出的文本、是否结束、最终快照或错误。

    客户端可以在任意时刻接入：先读已有的 text，再继续等增量。
    这样客户端断开重连不会丢进度，也不会重复调用模型。
    """

    text: str = ""
    running: bool = False
    done: bool = False
    snapshot: SessionRead | None = None
    error: ModelError | None = None

    def write(self, piece: str) -> None:
        self.text += piece


class StreamBroker:
    """进程内流式状态与后台任务的唯一持有者，也是将来替换实现的接口点。"""

    def __init__(self) -> None:
        self._live: dict[str, LiveStream] = {}
        # 后台任务必须有强引用，否则可能被回收；完成后自动移除。
        self._tasks: set[asyncio.Task[None]] = set()

    def stream(self, session_id: str, turn_id: str) -> LiveStream:
        key = f"{session_id}:{turn_id}"
        live = self._live.get(key)
        if live is None:
            live = LiveStream()
            self._live[key] = live
            try:
                loop = asyncio.get_running_loop()
                loop.call_later(LIVE_TTL_SEC, self._live.pop, key, None)
            except RuntimeError:
                pass
        return live

    def spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


broker = StreamBroker()


def live_stream(session_id: str, turn_id: str) -> LiveStream:
    return broker.stream(session_id, turn_id)


async def run_decision(
    database: Database, settings: Settings, session_id: str, answer_turn_id: str,
) -> None:
    """后台跑一次决策：用独立会话，异常也写回错误码，避免前端一直等不到结果。"""
    from interview_assistant.interview.service import InterviewService

    try:
        async with database.sessions() as session:
            await InterviewService(session, settings).decide(session_id, answer_turn_id)
    except Exception:
        logger.exception("Interview decision task failed; session_id=%s", session_id)
        try:
            async with database.sessions() as session:
                await InterviewService(session, settings).set_error(
                    session_id,
                    ModelError("decision_task_failed", "生成下一步失败，请点「重试下一步」"),
                )
        except Exception:
            logger.exception(
                "Interview decision failure not recorded; session_id=%s", session_id,
            )


async def run_decision_stream(
    database: Database, settings: Settings, session_id: str, answer_turn_id: str,
) -> None:
    """后台流式决策：文本写进 LiveStream 供 SSE 转发，结束时补齐最终快照。"""
    from interview_assistant.interview.service import InterviewService

    live = live_stream(session_id, answer_turn_id)
    if live.running or live.done:
        return
    live.running = True
    try:
        async with database.sessions() as session:
            await InterviewService(session, settings).decide_stream(
                session_id, answer_turn_id, emit=live.write,
            )
    except Exception:
        logger.exception("Interview stream decision failed; session_id=%s", session_id)
        try:
            async with database.sessions() as session:
                await InterviewService(session, settings).set_error(
                    session_id,
                    ModelError("decision_task_failed", "生成下一步失败，请点「重试下一步」"),
                )
        except Exception:
            logger.exception(
                "Interview decision failure not recorded; session_id=%s", session_id,
            )
    finally:
        live.running = False
        try:
            async with database.sessions() as session:
                live.snapshot = await InterviewService(session, settings).get(session_id)
        except Exception:
            logger.exception("Interview stream snapshot failed; session_id=%s", session_id)
        # 快照就绪后才置为结束，避免订阅方在 snapshot 为空时提前收尾。
        live.done = True


async def replay_frames(read: SessionRead) -> AsyncIterator[str]:
    yield sse("ack", read.model_dump_json())
    yield sse("state", read.model_dump_json())


async def answer_frames(
    request: Request, read: SessionRead, session_id: str, turn_id: str,
) -> AsyncIterator[str]:
    """先确认保存，再把面试官的话按增量推出去；断开不取消后台任务。"""
    yield sse("ack", read.model_dump_json())
    live = live_stream(session_id, turn_id)
    offset = 0
    heartbeat = time.monotonic()
    deadline = heartbeat + 180
    while True:
        if len(live.text) > offset:
            chunk = live.text[offset:]
            offset = len(live.text)
            yield sse("delta", json.dumps({"text": chunk}, ensure_ascii=False))
            heartbeat = time.monotonic()
        if live.done:
            break
        if await request.is_disconnected():
            # 断开只结束本次转发；后台继续跑，重进页面能看到落库结果。
            return
        now = time.monotonic()
        if now > deadline:
            yield sse("error", json.dumps(
                {"code": "stream_timeout", "message": "生成超时，可点「重试下一步」"},
                ensure_ascii=False,
            ))
            return
        if now - heartbeat > 15:
            yield ": ping\n\n"
            heartbeat = now
        await asyncio.sleep(0.04)
    if live.snapshot is not None:
        yield sse("state", live.snapshot.model_dump_json())
    else:
        yield sse("error", json.dumps(
            {"code": "no_snapshot", "message": "本步未能生成，可点「重试下一步」"},
            ensure_ascii=False,
        ))


async def stream_events(
    request: Request, database: Database, settings: Settings, session_id: str,
) -> AsyncIterator[str]:
    """降级式轮询：只在快照变化时推送，收到可作答状态或出错后关闭。"""
    from interview_assistant.interview.service import InterviewService

    deadline = time.monotonic() + 180
    heartbeat = time.monotonic()
    last: tuple[object, ...] | None = None
    while not await request.is_disconnected():
        try:
            async with database.sessions() as session:
                read = await InterviewService(session, settings).get(session_id)
        except InterviewError as error:
            payload = json.dumps(
                {"code": error.code, "message": error.message}, ensure_ascii=False,
            )
            yield f"event: error\ndata: {payload}\n\n"
            return
        except SQLAlchemyError:
            yield 'event: error\ndata: {"code":"database_unavailable","message":"数据库暂不可用"}\n\n'
            return
        signature = (
            read.status, read.revision, len(read.turns),
            read.current_turn.id if read.current_turn else None,
            read.pending_turn_id, read.error_code,
        )
        if signature != last:
            last = signature
            yield f"event: state\ndata: {read.model_dump_json()}\n\n"
            heartbeat = time.monotonic()
            if read.status != "active" or read.current_turn is not None:
                return
            if read.error_code is not None:
                return
        elif time.monotonic() - heartbeat > 15:
            yield ": ping\n\n"
            heartbeat = time.monotonic()
        if time.monotonic() > deadline:
            return
        await asyncio.sleep(0.35)
