from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Query

from interview_assistant.api.dependencies import (
    DatabaseDependency,
    SessionDependency,
    SettingsDependency,
)
from interview_assistant.materials.parse_schemas import (
    ParseConfirm, ParseCreate, ParseDraftList, ParseDraftRead,
)
from interview_assistant.materials.parsing import ResumeParser, run_parse_draft
from interview_assistant.materials.schemas import ProjectRead

router = APIRouter(prefix="/materials/resumes/{resume_id}/parse-drafts", tags=["resume-ai"])


@router.post("", response_model=ParseDraftRead, summary="提取项目候选：模型后台执行，或本地规则直接生成草稿")
async def create_parse_draft(
    resume_id: UUID, payload: ParseCreate,
    background: BackgroundTasks,
    session: SessionDependency,
    settings: SettingsDependency,
    database: DatabaseDependency,
) -> ParseDraftRead:
    draft, created = await ResumeParser(session, settings).create(str(resume_id), payload)
    if created and draft.status == "running" and database is not None:
        # 模型调用不占用 HTTP 请求；重放同一 request_id 不再提交第二次收费调用。
        background.add_task(
            run_parse_draft, database, settings, str(resume_id), str(draft.id),
        )
    return draft


@router.get("", response_model=ParseDraftList, summary="分页读取提取记录，不调用模型")
async def list_parse_drafts(
    resume_id: UUID, session: SessionDependency, settings: SettingsDependency,
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
    offset: Annotated[int, Query(ge=0)] = 0,
    source_version: Annotated[int | None, Query(ge=1)] = None,
) -> ParseDraftList:
    return await ResumeParser(session, settings).list_drafts(str(resume_id), limit, offset, source_version)


@router.get("/{draft_id}", response_model=ParseDraftRead, summary="读取提取草稿和逐项目确认状态")
async def get_parse_draft(
    resume_id: UUID, draft_id: UUID, session: SessionDependency, settings: SettingsDependency,
) -> ParseDraftRead:
    return await ResumeParser(session, settings).get(str(resume_id), str(draft_id))


@router.post("/{draft_id}/confirm", response_model=ProjectRead,
             summary="人工核对后保存单个候选项目，事务内保存事实与确认状态")
async def confirm_parse_project(
    resume_id: UUID, draft_id: UUID, payload: ParseConfirm,
    session: SessionDependency, settings: SettingsDependency,
) -> ProjectRead:
    return await ResumeParser(session, settings).confirm(str(resume_id), str(draft_id), payload)
