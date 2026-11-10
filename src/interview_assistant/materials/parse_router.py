from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from interview_assistant.api.dependencies import SessionDependency, SettingsDependency
from interview_assistant.materials.parse_schemas import (
    ParseConfirm, ParseCreate, ParseDraftList, ParseDraftRead,
)
from interview_assistant.materials.parsing import ResumeParser
from interview_assistant.materials.schemas import ProjectRead

router = APIRouter(prefix="/materials/resumes/{resume_id}/parse-drafts", tags=["resume-ai"])


@router.post("", response_model=ParseDraftRead, summary="调用 Qwen 提取指定简历版本，保存待确认草稿")
async def create_parse_draft(
    resume_id: UUID, payload: ParseCreate, session: SessionDependency, settings: SettingsDependency,
) -> ParseDraftRead:
    return await ResumeParser(session, settings).create(str(resume_id), payload)


@router.get("", response_model=ParseDraftList, summary="分页读取提取记录，不调用模型")
async def list_parse_drafts(
    resume_id: UUID, session: SessionDependency, settings: SettingsDependency,
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ParseDraftList:
    return await ResumeParser(session, settings).list_drafts(str(resume_id), limit, offset)


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
