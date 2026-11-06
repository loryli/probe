from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from interview_assistant.api.dependencies import SessionDependency
from interview_assistant.question_bank.imports import (
    ImportConfirm,
    ImportCreate,
    ImportPreview,
    ImportProblem,
    ImportResult,
    ImportService,
    SourceRead,
)

router = APIRouter(prefix="/question-bank", tags=["markdown-import"])


async def import_error_handler(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, ImportProblem)
    return JSONResponse(
        status_code=error.status, content={"detail": {"code": error.code, "message": error.message}}
    )


@router.post(
    "/imports",
    response_model=ImportPreview,
    status_code=201,
    summary="解析并保存 Markdown 预览草稿",
)
async def preview_import(
    payload: ImportCreate, response: Response, session: SessionDependency
) -> ImportPreview:
    preview, created = await ImportService(session).preview(payload)
    if not created:
        response.status_code = 200
    return preview


@router.get("/imports/{import_id}", response_model=ImportPreview, summary="读取导入预览与确认状态")
async def read_import(import_id: UUID, session: SessionDependency) -> ImportPreview:
    return await ImportService(session).read(str(import_id))


@router.post(
    "/imports/{import_id}/confirm", response_model=ImportResult, summary="原子导入选中的题目"
)
async def confirm_import(
    import_id: UUID, payload: ImportConfirm, session: SessionDependency
) -> ImportResult:
    return await ImportService(session).confirm(str(import_id), payload)


@router.get(
    "/imports/{import_id}/document", response_class=Response, summary="下载完整 Markdown 来源原文"
)
async def import_document(import_id: UUID, session: SessionDependency) -> Response:
    batch = await ImportService(session).require(str(import_id))
    disposition = f"attachment; filename*=UTF-8''{quote(batch.file_name, safe='')}"
    return Response(
        content=batch.original_text.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": disposition,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/questions/{question_id}/source", response_model=SourceRead, summary="读取导入来源与对应原文"
)
async def question_source(question_id: UUID, session: SessionDependency) -> SourceRead:
    return await ImportService(session).source(str(question_id))
