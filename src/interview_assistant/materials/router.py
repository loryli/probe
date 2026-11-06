from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Header, Path, Query, Request, Response
from fastapi.responses import JSONResponse

from interview_assistant.api.dependencies import SessionDependency, SettingsDependency
from interview_assistant.materials.models import ResumeDocument
from interview_assistant.materials.pdf import OcrMode
from interview_assistant.materials.schemas import (
    DocumentRead, ProjectCreate, ProjectList, ProjectRead, ProjectUpdate,
    ResumeCreate, ResumeFields, ResumeList, ResumeRead, ResumeUpdate, TextFormatInput,
    TextFormatRead, VersionList, VersionRead,
)
from interview_assistant.materials.service import MaterialError, MaterialService
from interview_assistant.materials.text_layout import format_resume_text

router = APIRouter(prefix="/materials", tags=["resume-materials"], responses={
    404: {"description": "资料不存在或已删除"},
    409: {"description": "编辑版本或创建标识冲突"},
    503: {"description": "数据库或 PDF 提取依赖不可用"},
})
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]
CreationKey = Annotated[UUID | None, Header(alias="Idempotency-Key")]
Revision = Annotated[int, Query(ge=1)]


async def material_error_handler(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, MaterialError)
    return JSONResponse(status_code=error.status, content={
        "detail": {"code": error.code, "message": error.message},
    })


@router.post("/text/format", response_model=TextFormatRead, summary="整理简历断行，返回草稿，不保存或补写内容")
def format_text(payload: TextFormatInput) -> TextFormatRead:
    return TextFormatRead(source_text=format_resume_text(payload.source_text))


@router.post("/documents/pdf", response_model=DocumentRead, status_code=201,
             summary="上传 PDF 原件并提取文字，暂不创建简历",
             openapi_extra={"requestBody": {"required": True, "content": {
                 "application/pdf": {"schema": {"type": "string", "format": "binary"}}
             }}})
async def upload_pdf(
    request: Request, session: SessionDependency, settings: SettingsDependency,
    file_name: Annotated[str, Query(min_length=1, max_length=255)],
    ocr_mode: Annotated[OcrMode, Query(description="auto: 文字不足页 OCR；force: 整份 OCR；off: 仅提取文字")] = "auto",
) -> DocumentRead:
    if not file_name.lower().endswith(".pdf"):
        raise MaterialError("invalid_filename", "请选择 PDF 文件")
    try:
        ResumeFields.filename(file_name)
    except ValueError:
        raise MaterialError("invalid_filename", "文件名不能包含路径或控制字符") from None
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > 10 * 1024 * 1024:
            raise MaterialError("pdf_size_limit", "PDF 文件最多 10 MiB", 413)
    if not content.startswith(b"%PDF-"):
        raise MaterialError("invalid_pdf", "文件不是有效的 PDF")
    return await MaterialService(session).upload_pdf(
        file_name, bytes(content), ocr_mode,
        settings.ocr_model_dir or settings.data_dir / "ocr-models",
    )


@router.post("/documents/{document_id}/extract", response_model=DocumentRead,
             summary="使用已保存的 PDF 原件重新提取，返回预览，不修改简历")
async def reextract_pdf(
    document_id: UUID, session: SessionDependency, settings: SettingsDependency,
    ocr_mode: OcrMode = "auto",
) -> DocumentRead:
    return await MaterialService(session).reextract_pdf(
        str(document_id), ocr_mode, settings.ocr_model_dir or settings.data_dir / "ocr-models",
    )


@router.get("/documents/{document_id}/file", summary="下载保存的 PDF 原件")
async def download_pdf(document_id: UUID, session: SessionDependency) -> Response:
    document = await session.get(ResumeDocument, str(document_id))
    if document is None:
        raise MaterialError("document_not_found", "PDF 原件不存在", 404)
    return Response(document.original_bytes, media_type="application/pdf", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(document.file_name, safe='')}",
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    })


@router.post("/resumes", response_model=ResumeRead, status_code=201, summary="创建简历及首个版本")
async def create_resume(
    payload: ResumeCreate, session: SessionDependency, response: Response,
    idempotency_key: CreationKey = None,
) -> ResumeRead:
    result, created = await MaterialService(session).create_resume(
        payload, str(idempotency_key) if idempotency_key else None
    )
    response.status_code = 201 if created else 200
    return result


@router.get("/resumes", response_model=ResumeList, summary="分页查询简历")
async def list_resumes(
    session: SessionDependency,
    search: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    limit: Limit = 20, offset: Offset = 0,
) -> ResumeList:
    return await MaterialService(session).list_resumes(search, limit, offset)


@router.get("/resumes/{resume_id}", response_model=ResumeRead, summary="读取简历当前内容")
async def get_resume(resume_id: UUID, session: SessionDependency) -> ResumeRead:
    return ResumeRead.model_validate(await MaterialService(session).require_resume(str(resume_id)))


@router.put("/resumes/{resume_id}", response_model=ResumeRead, summary="保存简历完整内容，检查 revision")
async def update_resume(
    resume_id: UUID, payload: ResumeUpdate, session: SessionDependency,
) -> ResumeRead:
    return await MaterialService(session).update_resume(str(resume_id), payload)


@router.delete("/resumes/{resume_id}", status_code=204, summary="逻辑删除简历，保留版本和项目依据")
async def delete_resume(
    resume_id: UUID, expected_revision: Revision, session: SessionDependency,
) -> Response:
    await MaterialService(session).delete_resume(str(resume_id), expected_revision)
    return Response(status_code=204)


@router.get("/resumes/{resume_id}/versions", response_model=VersionList, summary="分页查看简历版本")
async def list_versions(
    resume_id: UUID, session: SessionDependency, limit: Limit = 20, offset: Offset = 0,
) -> VersionList:
    return await MaterialService(session).history(str(resume_id), limit, offset)


@router.get("/resumes/{resume_id}/versions/{version}", response_model=VersionRead,
            summary="读取某版简历文字和 PDF 引用")
async def get_version(
    resume_id: UUID, version: Annotated[int, Path(ge=1)], session: SessionDependency,
) -> VersionRead:
    service = MaterialService(session)
    await service.require_resume(str(resume_id))
    return VersionRead.model_validate(await service.require_version(str(resume_id), version))


@router.post("/resumes/{resume_id}/projects", response_model=ProjectRead, status_code=201,
             summary="创建项目及人工事实，绑定指定简历版本")
async def create_project(
    resume_id: UUID, payload: ProjectCreate, session: SessionDependency,
    response: Response, idempotency_key: CreationKey = None,
) -> ProjectRead:
    result, created = await MaterialService(session).create_project(
        str(resume_id), payload, str(idempotency_key) if idempotency_key else None
    )
    response.status_code = 201 if created else 200
    return result


@router.get("/resumes/{resume_id}/projects", response_model=ProjectList, summary="分页查看简历下的项目")
async def list_projects(
    resume_id: UUID, session: SessionDependency, limit: Limit = 20, offset: Offset = 0,
) -> ProjectList:
    return await MaterialService(session).list_projects(str(resume_id), limit, offset)


@router.get("/resumes/{resume_id}/projects/{project_id}", response_model=ProjectRead,
            summary="读取项目和带来源的事实")
async def get_project(resume_id: UUID, project_id: UUID, session: SessionDependency) -> ProjectRead:
    service = MaterialService(session)
    await service.require_resume(str(resume_id))
    return await service.project_read(await service.require_project(str(resume_id), str(project_id)))


@router.put("/resumes/{resume_id}/projects/{project_id}", response_model=ProjectRead,
            summary="保存项目与事实完整内容，检查 revision")
async def update_project(
    resume_id: UUID, project_id: UUID, payload: ProjectUpdate, session: SessionDependency,
) -> ProjectRead:
    return await MaterialService(session).update_project(str(resume_id), str(project_id), payload)


@router.delete("/resumes/{resume_id}/projects/{project_id}", status_code=204, summary="逻辑删除项目")
async def delete_project(
    resume_id: UUID, project_id: UUID, expected_revision: Revision, session: SessionDependency,
) -> Response:
    await MaterialService(session).delete_project(str(resume_id), str(project_id), expected_revision)
    return Response(status_code=204)
