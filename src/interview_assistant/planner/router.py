from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import JSONResponse

from interview_assistant.api.dependencies import SessionDependency, SettingsDependency
from interview_assistant.planner.schemas import (
    PlanConfirm,
    PlanCreate,
    PlanList,
    PlanOptions,
    PlanRead,
    PlanRuntime,
)
from interview_assistant.planner.service import PlannerError, PlannerService

router = APIRouter(
    prefix="/interview-plans",
    tags=["interview-planner"],
    responses={
        404: {"description": "计划、简历或版本不存在"},
        409: {"description": "版本冲突、请求标识冲突或资料引用已失效"},
        422: {"description": "配置或资料不足"},
        503: {"description": "数据库未配置、不可用或尚未迁移"},
    },
)
Limit = Annotated[int, Query(ge=1, le=50)]
Offset = Annotated[int, Query(ge=0)]
Revision = Annotated[int, Query(ge=1)]


async def planner_error_handler(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, PlannerError)
    return JSONResponse(
        status_code=error.status,
        content={"detail": {"code": error.code, "message": error.message}},
    )


@router.get("/options", response_model=PlanOptions, summary="读取可选简历版本、已核对项目与启用题目，不调用模型")
async def plan_options(
    session: SessionDependency,
    settings: SettingsDependency,
    resume_id: UUID | None = None,
    source_version: Annotated[int | None, Query(ge=1)] = None,
) -> PlanOptions:
    return await PlannerService(session, settings).options(
        str(resume_id) if resume_id else None, source_version
    )


@router.post(
    "", response_model=PlanRead, status_code=status.HTTP_201_CREATED,
    summary="主动生成一份计划；刷新或重试只读取原任务，不重复调用模型",
    responses={200: {"model": PlanRead, "description": "相同请求标识重放，返回原计划"}},
)
async def create_plan(
    payload: PlanCreate,
    response: Response,
    session: SessionDependency,
    settings: SettingsDependency,
) -> PlanRead:
    plan, created = await PlannerService(session, settings).create(payload)
    if not created:
        response.status_code = status.HTTP_200_OK
    response.headers["Location"] = f"/interview-plans/{plan.id}"
    return plan


@router.get("", response_model=PlanList, summary="分页读取历史计划，不调用模型")
async def list_plans(
    session: SessionDependency,
    settings: SettingsDependency,
    limit: Limit = 10,
    offset: Offset = 0,
) -> PlanList:
    return await PlannerService(session, settings).list(limit, offset)


@router.get("/{plan_id}", response_model=PlanRead, summary="读取计划、资料快照与确认状态")
async def get_plan(
    plan_id: UUID, session: SessionDependency, settings: SettingsDependency,
) -> PlanRead:
    return await PlannerService(session, settings).get(str(plan_id))


@router.get("/{plan_id}/runtime", response_model=PlanRuntime,
            summary="读取已确认新版计划的目标、候选范围与完整资料快照，不调用模型")
async def plan_runtime(
    plan_id: UUID, session: SessionDependency, settings: SettingsDependency,
) -> PlanRuntime:
    return await PlannerService(session, settings).runtime(str(plan_id))


@router.post("/{plan_id}/confirm", response_model=PlanRead,
             summary="确认保存计划；确认前复核固定资料引用是否仍有效")
async def confirm_plan(
    plan_id: UUID,
    payload: PlanConfirm,
    session: SessionDependency,
    settings: SettingsDependency,
) -> PlanRead:
    return await PlannerService(session, settings).confirm(str(plan_id), payload)


@router.delete("/{plan_id}", status_code=status.HTTP_204_NO_CONTENT, summary="逻辑删除计划")
async def delete_plan(
    plan_id: UUID,
    session: SessionDependency,
    settings: SettingsDependency,
    expected_revision: Revision,
) -> Response:
    await PlannerService(session, settings).delete(str(plan_id), expected_revision)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
