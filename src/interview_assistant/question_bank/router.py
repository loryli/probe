from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse

from interview_assistant.api.dependencies import SessionDependency
from interview_assistant.question_bank.schemas import (
    CategoryAssignment,
    CategoryCreate,
    CategoryList,
    CategoryRead,
    CategoryRename,
    QuestionCreate,
    QuestionHistory,
    QuestionList,
    QuestionPatch,
    QuestionRead,
    QuestionVersionRead,
)
from interview_assistant.question_bank.service import (
    CategoryProblem,
    CategoryService,
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
    if isinstance(error, CategoryProblem):
        code, message, http_status = error.code, error.message, error.status
    elif isinstance(error, QuestionNotFound):
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
    category_id: UUID | None = None,
    uncategorized: bool = False,
    search: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> QuestionList:
    return await QuestionService(session).list_questions(
        is_active=is_active, search=search, limit=limit, offset=offset,
        category_id=str(category_id) if category_id is not None else None,
        uncategorized=uncategorized,
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


category_router = APIRouter(prefix="/question-bank/categories", tags=["question-bank"])


@category_router.get("", response_model=CategoryList, summary="分类与未删除题目的数量")
async def list_categories(session: SessionDependency) -> CategoryList:
    return await CategoryService(session).list_categories()


@category_router.post("", response_model=CategoryRead, status_code=201, summary="创建个人分类")
async def create_category(
    payload: CategoryCreate, response: Response, session: SessionDependency,
) -> CategoryRead:
    category, created = await CategoryService(session).create(payload)
    if not created:
        response.status_code = 200
    return category


@category_router.patch("/{category_id}", response_model=CategoryRead, summary="重命名个人分类")
async def rename_category(
    category_id: UUID, payload: CategoryRename, session: SessionDependency,
) -> CategoryRead:
    return await CategoryService(session).rename(str(category_id), payload)


@router.post("/classify", response_model=list[QuestionRead], summary="原子批量移动题目分类，不修改原文版本")
async def classify_questions(
    payload: CategoryAssignment, session: SessionDependency,
) -> list[QuestionRead]:
    return await CategoryService(session).assign(payload)
