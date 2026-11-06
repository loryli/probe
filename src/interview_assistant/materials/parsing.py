import hashlib
import json
import logging
from datetime import timedelta
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from interview_assistant.ai.qwen import ModelError, QwenModel, configuration_status
from interview_assistant.config import Settings
from interview_assistant.database import utc_now
from interview_assistant.materials.errors import MaterialError
from interview_assistant.materials.models import Project, ResumeParseDraft
from interview_assistant.materials.parse_prompt import PROMPT_VERSION, SCHEMA_VERSION, messages
from interview_assistant.materials.parse_schemas import (
    ParseConfirm, ParseCreate, ParseDraftList, ParseDraftRead, ResumeParseOutput,
)
from interview_assistant.materials.schemas import ProjectRead
from interview_assistant.materials.service import MaterialService, conflict, original_quote

logger = logging.getLogger(__name__)


def ground_result(output: ResumeParseOutput, source: str) -> ResumeParseOutput:
    """无原文依据的候选不进入待确认列表；不猜测纠正引用内容。"""
    result = output.model_copy(deep=True)
    projects = []
    warnings = list(result.needs_confirmation)
    for index, project in enumerate(result.projects):
        quote = original_quote(source, project.evidence_quote)
        if not project.name.strip() or not project.evidence_quote.strip() or quote is None:
            warnings.append(f"第 {index + 1} 个项目缺少可匹配原文依据，已从候选中排除。")
            continue
        project.evidence_quote = quote
        facts = []
        invalid_facts = False
        for fact in project.facts:
            evidence = original_quote(source, fact.evidence_quote)
            if (not fact.content.strip() or not fact.fact_type.strip()
                    or not fact.evidence_quote.strip() or evidence is None):
                invalid_facts = True
                continue
            fact.evidence_quote = evidence
            facts.append(fact)
        project.facts = facts
        if invalid_facts:
            project.needs_confirmation = [
                "有事实缺少可匹配原文依据，已排除，请对照简历补充。",
                *project.needs_confirmation,
            ][:20]
        projects.append(project)
    result.projects = projects
    result.needs_confirmation = warnings[:50]
    return result


class ResumeParser:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.materials = MaterialService(session)

    async def require_draft(self, resume_id: str, draft_id: str, *, lock: bool = False) -> ResumeParseDraft:
        query = select(ResumeParseDraft).where(
            ResumeParseDraft.id == draft_id, ResumeParseDraft.resume_id == resume_id,
        ).execution_options(populate_existing=True)
        row = await self.session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise MaterialError("parse_draft_not_found", "提取草稿不存在", 404)
        return row

    @staticmethod
    def expire(row: ResumeParseDraft) -> None:
        if row.status == "running" and row.expires_at < utc_now():
            row.status = "failed"
            row.error_code = "model_call_interrupted"
            row.error_message = "本次提取已超时或因服务重启中断；不会自动再次调用模型，可手动重新提取"

    @staticmethod
    def read(row: ResumeParseDraft) -> ParseDraftRead:
        return ParseDraftRead(
            id=row.id, resume_id=row.resume_id, source_version=row.source_version,
            status=row.status, revision=row.revision, model=row.model,
            prompt_version=row.prompt_version, schema_version=row.schema_version,
            result=ResumeParseOutput.model_validate(row.result_json) if row.result_json else None,
            confirmed_projects={key: value["project_id"] for key, value in row.confirmed_projects_json.items()},
            error_code=row.error_code, error_message=row.error_message, elapsed_ms=row.elapsed_ms,
            input_tokens=row.input_tokens, output_tokens=row.output_tokens, created_at=row.created_at,
        )

    async def get(self, resume_id: str, draft_id: str) -> ParseDraftRead:
        try:
            async with self.session.begin():
                await self.materials.require_resume(resume_id)
                row = await self.require_draft(resume_id, draft_id, lock=True)
                self.expire(row)
                await self.session.flush()
                return self.read(row)
        except StaleDataError:
            raise conflict() from None

    async def list(self, resume_id: str, limit: int, offset: int) -> ParseDraftList:
        try:
            async with self.session.begin():
                await self.materials.require_resume(resume_id)
                total = await self.session.scalar(select(func.count()).select_from(ResumeParseDraft).where(
                    ResumeParseDraft.resume_id == resume_id
                ))
                rows = list(await self.session.scalars(select(ResumeParseDraft).where(
                    ResumeParseDraft.resume_id == resume_id
                ).order_by(ResumeParseDraft.created_at.desc(), ResumeParseDraft.id).limit(limit).offset(offset)))
                for row in rows:
                    self.expire(row)
                await self.session.flush()
                return ParseDraftList(items=[self.read(row) for row in rows], total=total or 0,
                                      limit=limit, offset=offset)
        except StaleDataError:
            raise conflict() from None

    async def create(self, resume_id: str, payload: ParseCreate) -> ParseDraftRead:
        draft_id = str(payload.request_id)
        try:
            async with self.session.begin():
                await self.materials.require_resume(resume_id, lock=True)
                existing = await self.session.get(ResumeParseDraft, draft_id)
                if existing is not None:
                    if existing.resume_id != resume_id or existing.source_version != payload.source_version:
                        raise MaterialError("parse_request_conflict", "请求标识已用于其他简历或版本", 409)
                    self.expire(existing)
                    await self.session.flush()
                    return self.read(existing)
                if not configuration_status(self.settings)["configured"]:
                    raise MaterialError("model_not_configured", "请先在本地 .env 配置 Qwen API Key 和模型名称", 503)
                version = await self.materials.require_version(resume_id, payload.source_version)
                source = version.source_text
                if len(source) > 60000:
                    raise MaterialError("resume_model_input_limit", "AI 提取最多处理 60000 字符，请先精简简历", 413)
                now = utc_now()
                row = ResumeParseDraft(
                    id=draft_id, resume_id=resume_id, source_version=payload.source_version,
                    status="running", model=self.settings.qwen_model, provider="qwen",
                    prompt_version=PROMPT_VERSION, schema_version=SCHEMA_VERSION,
                    result_json={}, confirmed_projects_json={}, created_at=now,
                    expires_at=now + timedelta(seconds=self.settings.qwen_timeout_seconds + 30),
                )
                self.session.add(row)
                await self.session.flush()
        except IntegrityError:
            return await self.get(resume_id, draft_id)
        except StaleDataError:
            raise conflict() from None

        # 模型调用不持有数据库事务；一次 request_id 仅发起一次调用，不自动重试收费请求。
        model = QwenModel(self.settings)
        output = None
        error: ModelError | None = None
        try:
            generated = await model.generate(
                messages=messages(source, payload.source_version), output_type=ResumeParseOutput,
                prompt_version=PROMPT_VERSION, schema_version=SCHEMA_VERSION,
            )
            output = ground_result(generated, source)
        except ModelError as failure:
            error = failure
        except Exception as failure:
            logger.warning("Resume parse failed; draft_id=%s error_type=%s", draft_id, type(failure).__name__)
            error = ModelError("model_internal_error", f"提取处理失败（{type(failure).__name__}），请查看后端日志")
        async with self.session.begin():
            row = await self.require_draft(resume_id, draft_id, lock=True)
            if row.status == "running":
                row.elapsed_ms = model.elapsed_ms
                row.input_tokens = model.usage.input_tokens
                row.output_tokens = model.usage.output_tokens
                if error is not None:
                    row.status, row.error_code, row.error_message = "failed", error.code, error.message
                else:
                    assert output is not None
                    row.status, row.result_json = "ready", output.model_dump(mode="json")
                await self.session.flush()
            return self.read(row)

    async def confirm(self, resume_id: str, draft_id: str, payload: ParseConfirm) -> ProjectRead:
        if not payload.user_verified:
            raise MaterialError("confirmation_required", "请核对项目和事实，并勾选「我已核对」后保存")
        content = payload.model_dump(exclude={"candidate_index", "expected_revision"})
        digest = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        try:
            async with self.session.begin():
                await self.materials.require_resume(resume_id, lock=True)
                row = await self.require_draft(resume_id, draft_id, lock=True)
                key = str(payload.candidate_index)
                existing = row.confirmed_projects_json.get(key)
                if existing is not None:
                    if existing["payload_hash"] != digest:
                        raise MaterialError("parse_confirmation_conflict", "该候选已保存；请在已有项目中修改，不重复创建", 409)
                    project = await self.materials.require_project(resume_id, existing["project_id"])
                    return await self.materials.project_read(project)
                if row.revision != payload.expected_revision:
                    raise conflict()
                if row.status != "ready":
                    raise MaterialError("parse_not_ready", "此提取草稿还不能确认保存", 409)
                output = ResumeParseOutput.model_validate(row.result_json)
                if payload.candidate_index >= len(output.projects):
                    raise MaterialError("parse_candidate_not_found", "候选项目不存在", 404)
                project = Project(
                    resume_id=resume_id, source_version=row.source_version,
                    **payload.model_dump(exclude={"facts", "candidate_index", "expected_revision"}),
                    creation_key=str(uuid5(UUID(draft_id), key)), creation_hash=digest,
                )
                self.session.add(project)
                await self.session.flush()
                await self.materials.replace_facts(project, payload.facts)
                row.confirmed_projects_json = {
                    **row.confirmed_projects_json, key: {"project_id": project.id, "payload_hash": digest},
                }
                if len(row.confirmed_projects_json) == len(output.projects):
                    row.status = "confirmed"
                await self.session.flush()
                result = await self.materials.project_read(project)
            return result
        except (StaleDataError, IntegrityError):
            raise conflict() from None
