from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse

from interview_assistant.api.dependencies import SessionDependency
from interview_assistant.question_bank.schemas import (
    QuestionCreate,
    QuestionHistory,
    QuestionList,
    QuestionPatch,
    QuestionRead,
    QuestionVersionRead,
)
from interview_assistant.question_bank.service import (
    CreationKeyConflict,
    QuestionNotFound,
    QuestionService,
)

router = APIRouter(
    prefix="/question-bank/questions",
    tags=["question-bank"],
    responses={
        404: {"description": "题目或内容版本不存在"},
        409: {"description": "版本冲突或创建请求标识冲突"},
        503: {"description": "数据库未配置、不可用或尚未迁移"},
    },
)
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


async def question_error_handler(request: Request, error: Exception) -> JSONResponse:
    if isinstance(error, QuestionNotFound):
        code, message, http_status = "question_not_found", "题目或内容版本不存在", 404
    elif isinstance(error, CreationKeyConflict):
        code, message, http_status = (
            "creation_key_conflict",
            "创建请求标识已被其他内容使用或题目已删除",
            409,
        )
    else:
        code, message, http_status = "revision_conflict", "题目已被修改，请重新读取后再操作", 409
    return JSONResponse(
        status_code=http_status, content={"detail": {"code": code, "message": message}}
    )


@router.post(
    "",
    response_model=QuestionRead,
    status_code=status.HTTP_201_CREATED,
    summary="创建题目",
    responses={200: {"model": QuestionRead, "description": "重复创建请求，返回当前题目"}},
)
async def create_question(
    payload: QuestionCreate,
    response: Response,
    session: SessionDependency,
    idempotency_key: Annotated[
        str | None,
        Header(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$"),
    ] = None,
) -> QuestionRead:
    question, created = await QuestionService(session).create(payload, idempotency_key)
    if not created:
        response.status_code = status.HTTP_200_OK
    response.headers["Location"] = f"/question-bank/questions/{question.id}"
    return question


@router.get("", response_model=QuestionList, summary="分页查询题目")
async def list_questions(
    session: SessionDependency,
    is_active: bool | None = None,
    search: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> QuestionList:
    return await QuestionService(session).list(
        is_active=is_active, search=search, limit=limit, offset=offset
    )


@router.get("/{question_id}", response_model=QuestionRead, summary="读取题目")
async def get_question(question_id: UUID, session: SessionDependency) -> QuestionRead:
    return await QuestionService(session).get(str(question_id))


@router.patch("/{question_id}", response_model=QuestionRead, summary="编辑原文或切换启用状态")
async def patch_question(
    question_id: UUID, payload: QuestionPatch, session: SessionDependency
) -> QuestionRead:
    return await QuestionService(session).patch(str(question_id), payload)


@router.delete("/{question_id}", status_code=status.HTTP_204_NO_CONTENT, summary="逻辑删除题目")
async def delete_question(
    question_id: UUID,
    session: SessionDependency,
    expected_revision: Annotated[int, Query(ge=1, description="最近读取题目时返回的 revision")],
) -> Response:
    await QuestionService(session).delete(str(question_id), expected_revision)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{question_id}/versions", response_model=QuestionHistory, summary="分页查看内容历史")
async def list_versions(
    question_id: UUID, session: SessionDependency, limit: Limit = 50, offset: Offset = 0
) -> QuestionHistory:
    return await QuestionService(session).history(str(question_id), limit=limit, offset=offset)


@router.get(
    "/{question_id}/versions/{version}",
    response_model=QuestionVersionRead,
    summary="读取指定内容版本",
)
async def get_version(
    question_id: UUID,
    version: Annotated[int, Path(ge=1)],
    session: SessionDependency,
) -> QuestionVersionRead:
    return await QuestionService(session).version(str(question_id), version)
