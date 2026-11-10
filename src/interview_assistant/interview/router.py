import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.exc import SQLAlchemyError

from interview_assistant.api.dependencies import (
    DatabaseDependency,
    SessionDependency,
    SettingsDependency,
)
from interview_assistant.config import Settings
from interview_assistant.database import Database
from interview_assistant.interview.schemas import (
    AnswerSubmit,
    SessionControl,
    SessionCreate,
    SessionList,
    SessionOptions,
    SessionRead,
)
from interview_assistant.interview.service import (
    InterviewError,
    InterviewService,
    live_stream,
    run_decision,
    run_decision_stream,
)

router = APIRouter(
    prefix="/interviews",
    tags=["interview-runtime"],
    responses={
        404: {"description": "场次或计划不存在"},
        409: {"description": "状态冲突、重复提交或资料引用已失效"},
        422: {"description": "输入不符合要求"},
        503: {"description": "数据库未配置、不可用或尚未迁移"},
    },
)
Limit = Annotated[int, Query(ge=1, le=50)]
Offset = Annotated[int, Query(ge=0)]

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
# 后台流式任务必须有强引用，否则可能被回收；完成后自动移除。
_BACKGROUND: set[asyncio.Task[None]] = set()
logger = logging.getLogger(__name__)


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


async def interview_error_handler(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, InterviewError)
    return JSONResponse(
        status_code=error.status,
        content={"detail": {"code": error.code, "message": error.message}},
    )


@router.get("/options", response_model=SessionOptions,
            summary="读取可用的已确认计划与模型配置，不调用模型")
async def session_options(
    session: SessionDependency, settings: SettingsDependency,
) -> SessionOptions:
    return await InterviewService(session, settings).options()


@router.post(
    "", response_model=SessionRead, status_code=status.HTTP_201_CREATED,
    summary="按已确认计划开始一场面试并生成首问；相同请求标识只读取原场次",
    responses={200: {"model": SessionRead, "description": "相同请求标识重放，返回原场次"}},
)
async def create_session(
    payload: SessionCreate,
    response: Response,
    session: SessionDependency,
    settings: SettingsDependency,
) -> SessionRead:
    interview, created = await InterviewService(session, settings).create(payload)
    if not created:
        response.status_code = status.HTTP_200_OK
    response.headers["Location"] = f"/interviews/{interview.id}"
    return interview


@router.get("", response_model=SessionList, summary="分页读取历史场次，不加载完整资料")
async def list_sessions(
    session: SessionDependency,
    settings: SettingsDependency,
    limit: Limit = 10,
    offset: Offset = 0,
) -> SessionList:
    return await InterviewService(session, settings).list_sessions(limit, offset)


@router.get("/{session_id}", response_model=SessionRead, summary="读取场次、目标进度与全部轮次")
async def get_session(
    session_id: UUID, session: SessionDependency, settings: SettingsDependency,
) -> SessionRead:
    return await InterviewService(session, settings).get(str(session_id))


@router.post("/{session_id}/answers", response_model=SessionRead,
             summary="提交回答：先落库并立即返回，再由后台生成追问或下一题")
async def submit_answer(
    session_id: UUID,
    payload: AnswerSubmit,
    background: BackgroundTasks,
    session: SessionDependency,
    settings: SettingsDependency,
    database: DatabaseDependency,
) -> SessionRead:
    interview, pending_id = await InterviewService(session, settings).answer(
        str(session_id), payload,
    )
    if pending_id is not None and database is not None:
        # 决策在响应之后执行：回答已保存，用户不必盯着一个可能超时的请求。
        background.add_task(
            run_decision, database, settings, str(session_id), pending_id,
        )
    return interview


@router.post(
    "/{session_id}/answers/stream",
    summary="提交回答：立即落库，随后逐字推送面试官的下一句",
    response_class=StreamingResponse,
)
async def submit_answer_stream(
    session_id: UUID,
    payload: AnswerSubmit,
    request: Request,
    settings: SettingsDependency,
    database: DatabaseDependency,
) -> StreamingResponse:
    if database is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "database_not_configured", "message": "数据库尚未配置"},
        )
    try:
        # 落库在返回流之前完成：即使客户端立刻断开，回答也不会丢。
        async with database.sessions() as db:
            interview, pending_id = await InterviewService(db, settings).answer(
                str(session_id), payload,
            )
    except SQLAlchemyError as error:
        logger.warning("Database operation failed; error_type=%s", type(error).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "database_unavailable", "message": "数据库暂不可用，请检查连接及迁移状态"},
        ) from None
    if pending_id is None:
        # 幂等重放：没有新的待决策回答，只回一个终态。
        return StreamingResponse(
            _replay_frames(interview), media_type="text/event-stream", headers=_SSE_HEADERS,
        )
    live = live_stream(str(session_id), pending_id)
    if not live.running and not live.done:
        task = asyncio.create_task(
            run_decision_stream(database, settings, str(session_id), pending_id)
        )
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
    return StreamingResponse(
        _answer_frames(request, interview, str(session_id), pending_id),
        media_type="text/event-stream", headers=_SSE_HEADERS,
    )


async def _replay_frames(read: SessionRead) -> AsyncIterator[str]:
    yield _sse("ack", read.model_dump_json())
    yield _sse("state", read.model_dump_json())


async def _answer_frames(
    request: Request, read: SessionRead, session_id: str, turn_id: str,
) -> AsyncIterator[str]:
    """先确认保存，再把面试官的话按增量推出去；断开不取消后台任务。"""
    yield _sse("ack", read.model_dump_json())
    live = live_stream(session_id, turn_id)
    offset = 0
    heartbeat = time.monotonic()
    deadline = heartbeat + 180
    while True:
        if len(live.text) > offset:
            chunk = live.text[offset:]
            offset = len(live.text)
            yield _sse("delta", json.dumps({"text": chunk}, ensure_ascii=False))
            heartbeat = time.monotonic()
        if live.done:
            break
        if await request.is_disconnected():
            # 断开只结束本次转发；后台继续跑，重进页面能看到落库结果。
            return
        now = time.monotonic()
        if now > deadline:
            yield _sse("error", json.dumps(
                {"code": "stream_timeout", "message": "生成超时，可点「重试下一步」"},
                ensure_ascii=False,
            ))
            return
        if now - heartbeat > 15:
            yield ": ping\n\n"
            heartbeat = now
        await asyncio.sleep(0.04)
    if live.snapshot is not None:
        yield _sse("state", live.snapshot.model_dump_json())
    else:
        yield _sse("error", json.dumps(
            {"code": "no_snapshot", "message": "本步未能生成，可点「重试下一步」"},
            ensure_ascii=False,
        ))


@router.get("/{session_id}/events", summary="订阅场次进度：生成完成、状态变化或出错时推送完整快照")
async def stream_session(
    session_id: UUID,
    request: Request,
    settings: SettingsDependency,
    database: DatabaseDependency,
) -> StreamingResponse:
    if database is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "database_not_configured", "message": "数据库尚未配置"},
        )
    return StreamingResponse(
        _stream_events(request, database, settings, str(session_id)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stream_events(
    request: Request, database: Database, settings: Settings, session_id: str,
) -> AsyncIterator[str]:
    """降级式轮询：只在快照变化时推送，收到可作答状态或错误后关闭。"""
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


@router.post("/{session_id}/retry", response_model=SessionRead,
             summary="续接未完成的一步：补首问或重跑上次失败的决策，不重复保存回答")
async def retry_step(
    session_id: UUID, session: SessionDependency, settings: SettingsDependency,
) -> SessionRead:
    return await InterviewService(session, settings).retry(str(session_id))


@router.post("/{session_id}/pause", response_model=SessionRead, summary="暂停场次")
async def pause_session(
    session_id: UUID,
    payload: SessionControl,
    session: SessionDependency,
    settings: SettingsDependency,
) -> SessionRead:
    return await InterviewService(session, settings).pause(str(session_id), payload)


@router.post("/{session_id}/resume", response_model=SessionRead, summary="继续已暂停的场次")
async def resume_session(
    session_id: UUID,
    payload: SessionControl,
    session: SessionDependency,
    settings: SettingsDependency,
) -> SessionRead:
    return await InterviewService(session, settings).resume(str(session_id), payload)


@router.post("/{session_id}/skip", response_model=SessionRead,
             summary="跳过当前目标并生成下一问")
async def skip_objective(
    session_id: UUID,
    payload: SessionControl,
    session: SessionDependency,
    settings: SettingsDependency,
) -> SessionRead:
    return await InterviewService(session, settings).skip(str(session_id), payload)


@router.post("/{session_id}/end", response_model=SessionRead, summary="结束场次并保留全部问答")
async def end_session(
    session_id: UUID,
    payload: SessionControl,
    session: SessionDependency,
    settings: SettingsDependency,
) -> SessionRead:
    return await InterviewService(session, settings).end(str(session_id), payload)
