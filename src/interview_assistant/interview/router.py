from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import JSONResponse

from interview_assistant.api.dependencies import SessionDependency, SettingsDependency
from interview_assistant.interview.schemas import (
    AnswerSubmit,
    SessionControl,
    SessionCreate,
    SessionList,
    SessionOptions,
    SessionRead,
)
from interview_assistant.interview.service import InterviewError, InterviewService

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
             summary="提交回答：先落库再调用模型决定追问或切题")
async def submit_answer(
    session_id: UUID,
    payload: AnswerSubmit,
    session: SessionDependency,
    settings: SettingsDependency,
) -> SessionRead:
    return await InterviewService(session, settings).answer(str(session_id), payload)


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
