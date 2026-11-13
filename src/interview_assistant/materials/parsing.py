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
from interview_assistant.database import Database, utc_now
from interview_assistant.materials.errors import MaterialError
from interview_assistant.materials.extraction import (
    RULE_PROMPT_VERSION, local_candidates, resolve_references, with_quality_warnings,
)
from interview_assistant.materials.models import Project, ResumeParseDraft
from interview_assistant.materials.parse_prompt import MODEL_SCHEMA_VERSION, PROMPT_VERSION, SCHEMA_VERSION, messages
from interview_assistant.materials.parse_schemas import (
    ParseConfirm, ParseCreate, ParseDraftList, ParseDraftRead, ReferencedResumeOutput, ResumeParseOutput,
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
    return with_quality_warnings(result)


async def run_parse_draft(
    database: Database, settings: Settings, resume_id: str, draft_id: str,
) -> None:
    """后台执行一次模型提取：独立会话，异常也写回 failed，避免草稿永远停在 running。"""
    try:
        async with database.sessions() as session:
            await ResumeParser(session, settings).run(resume_id, draft_id)
    except Exception:
        # 不记录异常正文：供应商或校验异常中可能包含上传资料。
        logger.error("Resume parse task failed; draft_id=%s", draft_id)
        try:
            async with database.sessions() as session:
                parser = ResumeParser(session, settings)
                async with session.begin():
                    row = await parser.require_draft(resume_id, draft_id, lock=True)
                    if row.status == "running":
                        row.status = "failed"
                        row.error_code = "parse_task_failed"
                        row.error_message = "提取任务异常结束，未自动重试模型调用；可重新发起一次提取"
                        await session.flush()
        except Exception:
            logger.error("Resume parse failure not recorded; draft_id=%s", draft_id)


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
            status=row.status, revision=row.revision, model=row.model, provider=row.provider,
            prompt_version=row.prompt_version, schema_version=row.schema_version,
            result=with_quality_warnings(ResumeParseOutput.model_validate(row.result_json)) if row.result_json else None,
            confirmed_projects={key: value["project_id"] for key, value in row.confirmed_projects_json.items()},
            error_code=row.error_code, error_message=row.error_message, elapsed_ms=row.elapsed_ms,
            input_tokens=row.input_tokens, output_tokens=row.output_tokens, created_at=row.created_at,
            expires_at=row.expires_at,
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

    async def list_drafts(
        self, resume_id: str, limit: int, offset: int, source_version: int | None = None,
    ) -> ParseDraftList:
        try:
            async with self.session.begin():
                await self.materials.require_resume(resume_id)
                conditions = [ResumeParseDraft.resume_id == resume_id]
                if source_version is not None:
                    conditions.append(ResumeParseDraft.source_version == source_version)
                total = await self.session.scalar(
                    select(func.count()).select_from(ResumeParseDraft).where(*conditions)
                )
                rows = list(await self.session.scalars(select(ResumeParseDraft).where(*conditions)
                    .order_by(ResumeParseDraft.created_at.desc(), ResumeParseDraft.id).limit(limit).offset(offset)))
                for row in rows:
                    self.expire(row)
                await self.session.flush()
                return ParseDraftList(items=[self.read(row) for row in rows], total=total or 0,
                                      limit=limit, offset=offset)
        except StaleDataError:
            raise conflict() from None

    async def create(self, resume_id: str, payload: ParseCreate) -> tuple[ParseDraftRead, bool]:
        """模型模式创建后台草稿；规则模式直接保存本地候选，不调用模型。

        返回 created 供路由决定是否提交后台任务：重放同一 request_id 不会
        重复发起收费的模型调用。
        """
        draft_id = str(payload.request_id)
        local = payload.mode == "rule"
        provider = "local_rules" if local else "qwen"
        try:
            async with self.session.begin():
                await self.materials.require_resume(resume_id, lock=True)
                existing = await self.session.get(ResumeParseDraft, draft_id)
                if existing is not None:
                    if (existing.resume_id != resume_id or existing.source_version != payload.source_version
                            or existing.provider != provider):
                        raise MaterialError("parse_request_conflict", "请求标识已用于其他简历、版本或提取方式", 409)
                    self.expire(existing)
                    await self.session.flush()
                    return self.read(existing), False
                # 刷新页面/多个标签页也读取同一版本的在途任务，不另开收费调用。
                running = await self.session.scalar(select(ResumeParseDraft).where(
                    ResumeParseDraft.resume_id == resume_id,
                    ResumeParseDraft.source_version == payload.source_version,
                    ResumeParseDraft.provider == provider,
                    ResumeParseDraft.status == "running",
                ).order_by(ResumeParseDraft.created_at.desc()).limit(1))
                if running is not None:
                    self.expire(running)
                    await self.session.flush()
                    if running.status == "running":
                        return self.read(running), False
                if not local and not configuration_status(self.settings)["configured"]:
                    raise MaterialError("model_not_configured", "请先在本地 .env 配置 Qwen API Key 和模型名称", 503)
                version = await self.materials.require_version(resume_id, payload.source_version)
                if not local and len(version.source_text) > 60000:
                    raise MaterialError("resume_model_input_limit", "AI 提取最多处理 60000 字符，请先精简简历", 413)
                now = utc_now()
                fallback = ground_result(local_candidates(version.source_text), version.source_text) if local else None
                row = ResumeParseDraft(
                    id=draft_id, resume_id=resume_id, source_version=payload.source_version,
                    status="ready" if local else "running", model="local-rules" if local else self.settings.qwen_model,
                    provider=provider, prompt_version=RULE_PROMPT_VERSION if local else PROMPT_VERSION,
                    schema_version=SCHEMA_VERSION,
                    result_json=fallback.model_dump(mode="json") if fallback is not None else {},
                    confirmed_projects_json={}, created_at=now,
                    expires_at=now + timedelta(seconds=self.settings.qwen_resume_timeout_seconds + 30),
                )
                self.session.add(row)
                await self.session.flush()
                return self.read(row), True
        except IntegrityError:
            existing_draft = await self.get(resume_id, draft_id)
            if existing_draft.source_version != payload.source_version or existing_draft.provider != provider:
                raise MaterialError("parse_request_conflict", "请求标识已用于其他版本或提取方式", 409) from None
            return existing_draft, False
        except StaleDataError:
            raise conflict() from None

    async def run(self, resume_id: str, draft_id: str) -> None:
        """后台执行：读取固定版本原文 → 模型调用（事务外）→ 写回草稿状态。"""
        async with self.session.begin():
            row = await self.require_draft(resume_id, draft_id)
            if row.status != "running":
                return
            version = await self.materials.require_version(resume_id, row.source_version)
            source = version.source_text
            source_version = row.source_version
            remaining = (row.expires_at - utc_now()).total_seconds()
            if remaining <= 0:
                self.expire(row)
                await self.session.flush()
                return

        # 模型调用不持有数据库事务；一次 request_id 仅发起一次调用，不自动重试收费请求。
        model = QwenModel(self.settings)
        output = None
        error: ModelError | None = None
        try:
            # 流式接收可区分「未收到正文」与「生成中超时」，不把中间 JSON 当作已保存候选。
            async for _ in model.generate_stream(
                messages=messages(source, source_version), output_type=ReferencedResumeOutput,
                prompt_version=PROMPT_VERSION, schema_version=MODEL_SCHEMA_VERSION, temperature=0,
                timeout_seconds=min(remaining, self.settings.qwen_resume_timeout_seconds), enable_thinking=False,
            ):
                pass
            if not isinstance(model.result, ReferencedResumeOutput):
                raise ModelError("model_invalid_output", "模型未返回完整的项目引用结构")
            output = ground_result(resolve_references(model.result, source), source)
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
