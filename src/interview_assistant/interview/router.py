import logging
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
from interview_assistant.interview.analysis import TurnAnalysisService, run_turn_analysis
from interview_assistant.interview.schemas import (
    AnswerSubmit,
    MemoryCandidateRead,
    MemoryCandidateUpdate,
    ReportCreate,
    ReportRead,
    SessionBatchDelete,
    SessionControl,
    SessionCreate,
    SessionList,
    SessionOptions,
    SessionRead,
    TurnAnalysisJobRead,
    TurnAnalysisList,
)
from interview_assistant.interview.service import InterviewError, InterviewService
from interview_assistant.interview.streaming import (
    SSE_HEADERS,
    answer_frames,
    broker,
    live_stream,
    replay_frames,
    run_answer_background,
    run_decision_stream,
    stream_events,
)
from interview_assistant.interview.transcription import (
    MAX_AUDIO_BYTES,
    TranscriptionRead,
    accept_audio,
    get_task,
    run_transcription,
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

logger = logging.getLogger(__name__)


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


@router.post(
    "/batch-delete",
    response_model=dict,
    summary="批量删除面试记录：连同问答轮次与整场复盘物理删除，不可恢复",
    responses={409: {"description": "包含进行中的场次"}},
)
async def batch_delete_sessions(
    payload: SessionBatchDelete,
    session: SessionDependency,
    settings: SettingsDependency,
) -> dict:
    deleted = await InterviewService(session, settings).delete_sessions(payload)
    return {"deleted": deleted}


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
        # 回答已保存后，实时决策与逐轮分析并行；分析失败不影响下一题。
        background.add_task(
            run_answer_background, database, settings, str(session_id), pending_id,
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
            replay_frames(interview), media_type="text/event-stream", headers=SSE_HEADERS,
        )
    live = live_stream(str(session_id), pending_id)
    if not live.running and not live.done:
        broker.spawn(run_decision_stream(database, settings, str(session_id), pending_id))
        broker.spawn(run_turn_analysis(database, settings, str(session_id), pending_id))
    return StreamingResponse(
        answer_frames(request, interview, str(session_id), pending_id),
        media_type="text/event-stream", headers=SSE_HEADERS,
    )




@router.post(
    "/{session_id}/transcriptions",
    response_model=TranscriptionRead,
    status_code=202,
    summary="对当前题的录音启动转写，不提交回答",
    openapi_extra={"requestBody": {"required": True, "content": {
        "audio/wav": {"schema": {"type": "string", "format": "binary"}},
    }}},
)
async def create_transcription(
    session_id: UUID,
    request_id: UUID,
    turn_id: UUID,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    session: SessionDependency,
    database: DatabaseDependency,
    settings: SettingsDependency,
) -> TranscriptionRead:
    if database is None:
        raise InterviewError("database_not_configured", "数据库尚未配置。", 503)
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "audio/wav":
        raise InterviewError("invalid_voice_format", "录音必须为 WAV 格式。", 415)
    audio = bytearray()
    async for chunk in request.stream():
        if len(audio) + len(chunk) > MAX_AUDIO_BYTES:
            raise InterviewError("voice_audio_too_large", "单段录音不能超过 3 分钟。", 413)
        audio.extend(chunk)
    wav = bytes(audio)
    job, created = await accept_audio(
        session, settings, str(session_id), str(turn_id), str(request_id), wav,
    )
    if created:
        background.add_task(run_transcription, database, settings, str(request_id), wav)
    else:
        response.status_code = 200
    response.headers["Cache-Control"] = "no-store"
    return job


@router.get(
    "/{session_id}/transcriptions/{request_id}",
    response_model=TranscriptionRead,
    summary="读取对应录音的转写状态与临时草稿",
)
async def read_transcription(
    session_id: UUID, request_id: UUID, response: Response, session: SessionDependency,
) -> TranscriptionRead:
    response.headers["Cache-Control"] = "no-store"
    return await get_task(session, str(session_id), str(request_id))


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
        stream_events(request, database, settings, str(session_id)),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )




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


@router.get(
    "/{session_id}/analyses",
    response_model=TurnAnalysisList,
    summary="读取逐轮后台分析任务、结果与失败状态，不触发模型调用",
)
async def list_turn_analyses(
    session_id: UUID,
    session: SessionDependency,
    settings: SettingsDependency,
) -> TurnAnalysisList:
    return await TurnAnalysisService(session, settings).list(str(session_id))


@router.post(
    "/{session_id}/analyses/{job_id}/retry",
    response_model=TurnAnalysisJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="人工重试一条失败或过期的逐轮分析，不影响面试状态机",
)
async def retry_turn_analysis(
    session_id: UUID,
    job_id: UUID,
    background: BackgroundTasks,
    session: SessionDependency,
    settings: SettingsDependency,
    database: DatabaseDependency,
) -> TurnAnalysisJobRead:
    if database is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "database_not_configured", "message": "数据库尚未配置"},
        )
    job = await TurnAnalysisService(session, settings).prepare_retry(
        str(session_id), str(job_id),
    )
    background.add_task(
        run_turn_analysis, database, settings, str(session_id), str(job.turn_id),
    )
    return job


@router.patch(
    "/{session_id}/memory-candidates/{memory_id}",
    response_model=MemoryCandidateRead,
    summary="确认或忽略一条结构化记忆证据候选",
)
async def update_memory_candidate(
    session_id: UUID,
    memory_id: UUID,
    payload: MemoryCandidateUpdate,
    session: SessionDependency,
    settings: SettingsDependency,
) -> MemoryCandidateRead:
    return await TurnAnalysisService(session, settings).update_memory(
        str(session_id), str(memory_id), payload,
    )


@router.get(
    "/{session_id}/report", response_model=ReportRead,
    summary="读取整场复盘；没有生成过时返回 404",
    responses={404: {"description": "场次不存在或尚未生成复盘"}},
)
async def get_report(
    session_id: UUID, session: SessionDependency, settings: SettingsDependency,
) -> ReportRead:
    report = await InterviewService(session, settings).get_report(str(session_id))
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "report_not_found", "message": "还没有生成过复盘"},
        )
    return report


@router.post(
    "/{session_id}/report", response_model=ReportRead, status_code=status.HTTP_201_CREATED,
    summary="为已结束的场次生成整场复盘；每场一份，失败可重试",
    responses={
        200: {"model": ReportRead, "description": "重放已生成的复盘，直接返回原报告"},
        409: {"description": "场次尚未结束"},
    },
)
async def create_report(
    session_id: UUID,
    payload: ReportCreate,
    response: Response,
    session: SessionDependency,
    settings: SettingsDependency,
) -> ReportRead:
    """同步生成（一次模型调用，约 10–30 秒）；已生成过则直接返回原报告。"""
    report, created = await InterviewService(session, settings).report(
        str(session_id), payload,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return report

