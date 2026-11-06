"""面试准备与 Planner 服务：固定资料版本、生成目标与预算、人工确认保存。"""

import logging
import math
import re
import time
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from interview_assistant.ai.qwen import ModelError, QwenModel, configuration_status
from interview_assistant.config import Settings
from interview_assistant.database import utc_now
from interview_assistant.materials.models import Project, ProjectFact, Resume, ResumeVersion
from interview_assistant.planner.models import InterviewPlan
from interview_assistant.planner.prompt import (
    PROMPT_VERSION,
    RULE_PROMPT_VERSION,
    SCHEMA_VERSION,
    messages,
)
from interview_assistant.planner.schemas import (
    MAX_FACTS_PER_PROJECT,
    MAX_OBJECTIVES,
    MAX_PROJECTS,
    MAX_QUESTIONS,
    MAX_QUESTION_TEXT_CHARS,
    ObjectiveDraft,
    PlanConfirm,
    PlanConstraints,
    PlanCoverage,
    PlanCreate,
    PlanInputs,
    PlanList,
    PlanNotes,
    PlanObjective,
    PlanOptions,
    PlanOutput,
    PlanRead,
    PlanRuntime,
    PlanRuntimeObjective,
    PlanSnapshot,
    PlanSummary,
    PlannerDraft,
    ProjectOption,
    QuestionOption,
    ResumeOption,
    SnapshotFact,
    SnapshotProject,
    SnapshotQuestion,
    VersionOption,
    objective_limit,
)
from interview_assistant.question_bank.models import Question

logger = logging.getLogger(__name__)

OPTION_QUESTION_LIMIT = 200
OPTION_RESUME_LIMIT = 50


class PlannerError(Exception):
    def __init__(self, code: str, message: str, status: int = 422) -> None:
        self.code, self.message, self.status = code, message, status


def conflict() -> PlannerError:
    return PlannerError("revision_conflict", "计划已被修改，请重新读取后再操作", 409)


def stale(message: str) -> PlannerError:
    return PlannerError("plan_reference_stale", message, 409)


VERSION_PATTERN = re.compile(r"(?<!\w)[vV](\d+)\b")


def foreign_versions(text: str, source_version: int) -> bool:
    """只拦截与所选版本不符的显式版本号，不误伤 IPv4、vLLM 等技术名词。"""
    return any(int(value) != source_version for value in VERSION_PATTERN.findall(text))


def filter_model_notes(items: list[str], source_version: int) -> tuple[list[str], int]:
    """模型自评不是事实；含未核验版本号的条目直接剔除。"""
    accepted: list[str] = []
    removed = 0
    for item in items[:6]:
        text = item.strip()[:240]
        if not text:
            continue
        if foreign_versions(text, source_version):
            removed += 1
            continue
        accepted.append(text)
    return accepted, removed


def compile_runtime_context(
    objectives: list[PlanObjective], snapshot: PlanSnapshot,
) -> dict[str, str]:
    """按目标预编译紧凑上下文，实时链路无需再做 ID 关联或二次模型调用。"""
    facts = {
        str(fact.id): (project.name, fact)
        for project in snapshot.projects
        for fact in project.facts
    }
    questions = {str(item.id): item for item in snapshot.questions}
    compiled: dict[str, str] = {}
    for objective in objectives:
        lines = [
            f"目标 {objective.id}｜来源 {objective.source_preference}｜优先级 {objective.priority}",
            f"考察主题：{objective.topic}",
            f"完成规则：{objective.completion_rule or '能说明关键依据并回应追问'}",
            f"时间预算 {objective.time_budget_sec}s｜追问上限 {objective.max_followups}",
        ]
        if objective.observable_evidence:
            lines.append("可观察证据：" + "；".join(objective.observable_evidence))
        for fact_id in objective.project_fact_ids:
            fact_item = facts.get(str(fact_id))
            if fact_item is not None:
                name, fact = fact_item
                lines.append(
                    f"项目事实[{fact_id}]（{name}）：{fact.fact_type}｜{fact.content[:500]}"
                )
        for question_id in objective.question_ids:
            question_item = questions.get(str(question_id))
            if question_item is not None:
                lines.append(
                    f"启用题目[{question_id}]：{question_item.question_text[:300]}"
                )
        compiled[objective.id] = "\n".join(lines)
    return compiled


class PlannerService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    @staticmethod
    def expire(row: InterviewPlan) -> None:
        if row.status == "running" and row.expires_at < utc_now():
            row.status = "failed"
            row.error_code = "plan_generation_interrupted"
            row.error_message = "本次规划已超时或因服务重启中断；不会自动重新调用模型，可手动重新生成"

    @staticmethod
    def read(row: InterviewPlan) -> PlanRead:
        return PlanRead(
            id=row.id,
            resume_id=row.resume_id,
            source_version=row.source_version,
            status=row.status,
            revision=row.revision,
            provider=row.provider,
            model=row.model,
            prompt_version=row.prompt_version,
            schema_version=row.schema_version,
            inputs=PlanInputs.model_validate(row.inputs_json),
            snapshot=PlanSnapshot.model_validate(row.snapshot_json) if row.snapshot_json else None,
            result=PlanOutput.model_validate(row.result_json) if row.result_json else None,
            error_code=row.error_code,
            error_message=row.error_message,
            elapsed_ms=row.elapsed_ms,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            created_at=row.created_at,
            updated_at=row.updated_at,
            confirmed_at=row.confirmed_at,
        )

    async def require_plan(self, plan_id: str, *, lock: bool = False) -> InterviewPlan:
        query = select(InterviewPlan).where(
            InterviewPlan.id == plan_id, InterviewPlan.deleted_at.is_(None)
        ).execution_options(populate_existing=True)
        row = await self.session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise PlannerError("plan_not_found", "计划不存在或已删除", 404)
        return row

    async def options(self, resume_id: str | None, source_version: int | None) -> PlanOptions:
        """只读组装可选资料，不调用模型，不修改任何数据。"""
        resumes = list(await self.session.scalars(
            select(Resume).where(Resume.deleted_at.is_(None))
            .order_by(Resume.created_at.desc(), Resume.id).limit(OPTION_RESUME_LIMIT)
        ))
        selected: Resume | None = None
        if resume_id is not None:
            selected = await self.session.scalar(
                select(Resume).where(Resume.id == resume_id, Resume.deleted_at.is_(None))
            )
            if selected is None:
                raise PlannerError("resume_not_found", "简历不存在或已删除", 404)
        elif resumes:
            selected = resumes[0]

        versions: list[VersionOption] = []
        projects: list[ProjectOption] = []
        questions: list[QuestionOption] = []
        question_total = 0
        chosen_version: int | None = None
        if selected is not None:
            version_rows = list(await self.session.scalars(
                select(ResumeVersion).where(ResumeVersion.resume_id == selected.id)
                .order_by(ResumeVersion.version.desc())
            ))
            versions = [
                VersionOption(
                    version=row.version, name=row.name,
                    content_sha256=row.content_sha256, created_at=row.created_at,
                )
                for row in version_rows
            ]
            available = {row.version for row in version_rows}
            chosen_version = (
                source_version if source_version in available else
                (version_rows[0].version if version_rows else None)
            )
            if chosen_version is not None:
                project_rows = list(await self.session.scalars(
                    select(Project).where(
                        Project.resume_id == selected.id,
                        Project.source_version == chosen_version,
                        Project.user_verified.is_(True),
                        Project.deleted_at.is_(None),
                    ).order_by(Project.created_at.desc(), Project.id).limit(MAX_PROJECTS)
                ))
                counts: dict[str, int] = {}
                if project_rows:
                    grouped = await self.session.execute(
                        select(ProjectFact.project_id, func.count()).where(
                            ProjectFact.project_id.in_([row.id for row in project_rows]),
                            ProjectFact.deleted_at.is_(None),
                        ).group_by(ProjectFact.project_id)
                    )
                    counts = {str(key): int(value) for key, value in grouped.all()}
                projects = [
                    ProjectOption(
                        id=row.id, name=row.name, source_version=row.source_version,
                        tech_stack=row.tech_stack, fact_count=counts.get(row.id, 0),
                    )
                    for row in project_rows
                ]
            question_total = await self.session.scalar(
                select(func.count()).select_from(Question).where(
                    Question.deleted_at.is_(None), Question.is_active.is_(True)
                )
            ) or 0
            question_rows = list(await self.session.scalars(
                select(Question).where(
                    Question.deleted_at.is_(None), Question.is_active.is_(True)
                ).order_by(Question.created_at.desc(), Question.id).limit(OPTION_QUESTION_LIMIT)
            ))
            questions = [
                QuestionOption(
                    id=row.id, question_text=row.question_text[:MAX_QUESTION_TEXT_CHARS],
                    content_version=row.content_version,
                )
                for row in question_rows
            ]

        return PlanOptions(
            resumes=[
                ResumeOption(
                    id=row.id, name=row.name,
                    content_version=row.content_version, is_active=row.is_active,
                )
                for row in resumes
            ],
            versions=versions,
            projects=projects,
            questions=questions,
            question_total=question_total,
            selected_resume_id=selected.id if selected is not None else None,
            selected_version=chosen_version,
            model=configuration_status(self.settings),
            limits={
                "max_projects": MAX_PROJECTS,
                "max_questions": MAX_QUESTIONS,
                "max_objectives": MAX_OBJECTIVES,
                "max_duration_minutes": 180,
                "option_question_limit": OPTION_QUESTION_LIMIT,
            },
        )

    async def build_snapshot(self, inputs: PlanInputs) -> tuple[PlanSnapshot, list[str]]:
        """固定所选简历版本、已核对项目事实与启用题目，剔除无效引用。"""
        limitations: list[str] = []
        resume = await self.session.scalar(
            select(Resume).where(Resume.id == str(inputs.resume_id), Resume.deleted_at.is_(None))
        )
        if resume is None:
            raise PlannerError("resume_not_found", "简历不存在或已删除", 404)
        version = await self.session.get(ResumeVersion, (resume.id, inputs.source_version))
        if version is None:
            raise PlannerError("version_not_found", "所选简历版本不存在", 404)

        project_ids = [str(item) for item in inputs.project_ids]
        project_rows: list[Project] = []
        if project_ids:
            project_rows = list(await self.session.scalars(
                select(Project).where(
                    Project.id.in_(project_ids),
                    Project.resume_id == resume.id,
                    Project.source_version == inputs.source_version,
                    Project.user_verified.is_(True),
                    Project.deleted_at.is_(None),
                ).order_by(Project.created_at, Project.id).limit(MAX_PROJECTS)
            ))
            if len(project_rows) < len(project_ids):
                limitations.append("部分项目未核对、不属于所选简历版本或已删除，已从依据中排除。")

        fact_rows: list[ProjectFact] = []
        if project_rows:
            fact_rows = list(await self.session.scalars(
                select(ProjectFact).where(
                    ProjectFact.project_id.in_([row.id for row in project_rows]),
                    ProjectFact.deleted_at.is_(None),
                ).order_by(ProjectFact.project_id, ProjectFact.position)
            ))
        grouped: dict[str, list[ProjectFact]] = {}
        for fact in fact_rows:
            grouped.setdefault(fact.project_id, []).append(fact)
        truncated_facts = False
        projects: list[SnapshotProject] = []
        for row in project_rows:
            facts = grouped.get(row.id, [])
            if len(facts) > MAX_FACTS_PER_PROJECT:
                truncated_facts = True
            projects.append(SnapshotProject(
                id=row.id, name=row.name, role=row.role, tech_stack=row.tech_stack,
                facts=[
                    SnapshotFact(id=fact.id, fact_type=fact.fact_type, content=fact.content)
                    for fact in facts[:MAX_FACTS_PER_PROJECT]
                ],
            ))
        if truncated_facts:
            limitations.append(f"每个项目最多使用 {MAX_FACTS_PER_PROJECT} 条事实，超出部分未纳入本次规划。")
        if any(not project.facts for project in projects):
            limitations.append("部分已核对项目没有事实条目，只能依据项目名称与字段规划。")

        question_ids = [str(item) for item in inputs.question_ids]
        question_rows: list[Question] = []
        if question_ids:
            question_rows = list(await self.session.scalars(
                select(Question).where(
                    Question.id.in_(question_ids),
                    Question.deleted_at.is_(None),
                    Question.is_active.is_(True),
                ).order_by(Question.created_at, Question.id).limit(MAX_QUESTIONS)
            ))
            if len(question_rows) < len(question_ids):
                limitations.append("部分题目已停用、删除或不存在，已从依据中排除。")
        snapshot_questions = [
            SnapshotQuestion(
                id=row.id, question_text=row.question_text, content_version=row.content_version,
            )
            for row in question_rows
        ]
        return PlanSnapshot(
            resume_id=resume.id, resume_name=resume.name,
            source_version=inputs.source_version, content_sha256=version.content_sha256,
            projects=projects, questions=snapshot_questions,
        ), limitations

    @staticmethod
    def rule_draft(snapshot: PlanSnapshot, inputs: PlanInputs) -> PlannerDraft:
        """无模型时按资料分块生成确定性目标，明确标注不调用模型。"""
        objectives: list[ObjectiveDraft] = []
        limitations = [
            "规则模式未调用模型，主题按项目名称与题库分块生成，请人工核对后确认。"
        ]
        allowed = objective_limit(inputs.duration_minutes)
        usable_projects = [item for item in snapshot.projects if item.facts][:allowed]
        if len(usable_projects) < len(snapshot.projects):
            limitations.append("有已核对项目没有事实条目，未为它们生成项目题目标。")
        for index, project in enumerate(usable_projects):
            topic = project.name[:80]
            if project.tech_stack:
                topic = f"{topic} · {'、'.join(project.tech_stack[:3])}"
            objectives.append(ObjectiveDraft(
                topic=topic[:120] or "项目经历",
                source_preference="project",
                priority="high" if index == 0 else "medium",
                observable_evidence=["能结合项目事实说明职责、实现取舍与结果"],
                project_fact_ids=[str(fact.id) for fact in project.facts][:20],
                question_ids=[],
                completion_rule="说明关键做法，并能回应一个基于事实的追问",
                max_followups=2,
            ))
        remaining = allowed - len(objectives)
        if snapshot.questions and remaining > 0:
            groups = min(remaining, max(1, math.ceil(len(snapshot.questions) / 5)))
            size = math.ceil(len(snapshot.questions) / groups)
            for index in range(groups):
                chunk = snapshot.questions[index * size:(index + 1) * size]
                if not chunk:
                    continue
                objectives.append(ObjectiveDraft(
                    topic=f"通用题组 {index + 1}（第 {index * size + 1}–"
                          f"{index * size + len(chunk)} 题）",
                    source_preference="question_bank",
                    priority="medium" if index == 0 else "low",
                    observable_evidence=["能覆盖所选启用题目的关键知识点"],
                    project_fact_ids=[],
                    question_ids=[str(item.id) for item in chunk][:20],
                    completion_rule="覆盖主要知识点并说明依据",
                    max_followups=1,
                ))
        return PlannerDraft(
            schema_version="planner.v1",
            session_summary=f"依据简历 v{inputs.source_version}、已核对项目与启用题库生成的规则计划",
            objectives=objectives,
            limitations=limitations,
        )

    @staticmethod
    def build_output(
        draft: PlannerDraft,
        inputs: PlanInputs,
        snapshot: PlanSnapshot,
        limitations: list[str],
        mode: str,
    ) -> PlanOutput:
        """摘要、预算、追问配额、覆盖率与运行时上下文均由程序完成，模型只给语义骨架。"""
        valid_facts = {str(fact.id) for project in snapshot.projects for fact in project.facts}
        valid_questions = {str(item.id) for item in snapshot.questions}
        system_notes: list[str] = list(limitations)
        model_notes, removed_notes = filter_model_notes(
            draft.limitations, snapshot.source_version
        )
        if removed_notes:
            system_notes.append(f"已剔除 {removed_notes} 条含未核验版本号的模型提示。")
        system_notes.append(
            "模型提示仅是模型对资料完整度的自评，未经事实核验；请对照引用明细判断。"
        )

        objectives: list[PlanObjective] = []
        invalid_reference = False
        version_warning = False
        for raw in draft.objectives[:objective_limit(inputs.duration_minutes)]:
            fact_ids = list(dict.fromkeys(raw.project_fact_ids))
            question_ids = list(dict.fromkeys(raw.question_ids))
            facts = [item for item in fact_ids if item in valid_facts]
            questions = [item for item in question_ids if item in valid_questions]
            if len(facts) != len(fact_ids) or len(questions) != len(question_ids):
                invalid_reference = True
            source: str = raw.source_preference
            if source == "project" and not facts:
                source = "question_bank" if questions else ""
            elif source == "question_bank" and not questions:
                source = "project" if facts else ""
            if not source:
                continue
            topic = raw.topic.strip()[:80] or "未命名目标"
            rule = raw.completion_rule.strip()[:120]
            evidence = [
                item.strip()[:80] for item in raw.observable_evidence if item.strip()
            ][:3]
            if foreign_versions(
                f"{topic} {' '.join(evidence)} {rule}", snapshot.source_version
            ):
                version_warning = True
            objectives.append(PlanObjective(
                id=f"obj_{len(objectives) + 1:02d}",
                topic=topic,
                source_preference=source,
                priority=raw.priority,
                observable_evidence=evidence,
                project_fact_ids=[UUID(value) for value in facts],
                question_ids=[UUID(value) for value in questions],
                time_budget_sec=0,
                max_followups=raw.max_followups,
                completion_rule=rule,
            ))
        if invalid_reference:
            system_notes.append(
                "有资料引用不存在或不属于所选版本，已剔除；目标只保留可核验的引用。"
            )
        if version_warning:
            system_notes.append("目标文本含与所选简历版本不符的版本号，请核对引用明细后使用。")
        if not objectives:
            raise PlannerError(
                "no_usable_objectives",
                "本次未得到有有效资料引用的目标，可切换规则模式或重新生成",
            )

        duration_sec = inputs.duration_minutes * 60
        buffer = min(60, max(30, round(duration_sec * 0.05 / 30) * 30))
        topic_sec = max(60, duration_sec - 2 * buffer)
        project_sec = round(topic_sec * inputs.project_ratio / 30) * 30
        bank_sec = topic_sec - project_sec
        if not valid_facts or not any(o.source_preference == "project" for o in objectives):
            project_sec, bank_sec = 0, topic_sec
            system_notes.append("没有可用的已核对项目事实，项目题时间预算已分配给题库题。")
        elif not valid_questions or not any(
            o.source_preference == "question_bank" for o in objectives
        ):
            project_sec, bank_sec = topic_sec, 0
            system_notes.append("没有可用的启用题目，题库题时间预算已分配给项目题。")

        weights = {"high": 3, "medium": 2, "low": 1}
        for source, budget in (("project", project_sec), ("question_bank", bank_sec)):
            group = [item for item in objectives if item.source_preference == source]
            if not group or budget <= 0:
                continue
            shares = [weights[item.priority] for item in group]
            total = sum(shares)
            assigned = 0
            for index, (objective, share) in enumerate(zip(group, shares)):
                if index == len(group) - 1:
                    seconds = budget - assigned
                else:
                    seconds = round(budget * share / total / 30) * 30
                    seconds = max(
                        30,
                        min(seconds, budget - assigned - 30 * (len(group) - index - 1)),
                    )
                objective.time_budget_sec = max(30, seconds)
                assigned += objective.time_budget_sec
        assigned_total = sum(item.time_budget_sec for item in objectives)
        if assigned_total > topic_sec:
            factor = topic_sec / assigned_total
            for objective in objectives:
                objective.time_budget_sec = max(
                    30, round(objective.time_budget_sec * factor / 30) * 30
                )

        # 追问配额先给每个有意愿的目标保底一次，再按优先级分配剩余额度。
        max_total = inputs.max_total_followups or min(
            12, max(3, inputs.duration_minutes // 5)
        )
        cap = min(3, max_total)
        desired = [max(0, min(item.max_followups, cap)) for item in objectives]
        if sum(desired) <= max_total:
            quota = desired[:]
        else:
            willing = sum(1 for item in desired if item >= 1)
            base = 1 if max_total >= willing else 0
            quota = [min(base, item) for item in desired]
            remaining = max_total - sum(quota)
            order = sorted(
                range(len(objectives)),
                key=lambda index: (-weights[objectives[index].priority], index),
            )
            while remaining > 0:
                progressed = False
                for index in order:
                    if remaining == 0:
                        break
                    if quota[index] < desired[index] and quota[index] < cap:
                        quota[index] += 1
                        remaining -= 1
                        progressed = True
                if not progressed:
                    break
        for index, objective in enumerate(objectives):
            objective.max_followups = quota[index]

        used_facts = {str(item) for o in objectives for item in o.project_fact_ids}
        used_questions = {str(item) for o in objectives for item in o.question_ids}
        used_projects = sum(
            1 for project in snapshot.projects
            if any(str(fact.id) in used_facts for fact in project.facts)
        )
        coverage = PlanCoverage(
            facts_used=len(used_facts),
            facts_total=len(valid_facts),
            questions_used=len(used_questions),
            questions_total=len(valid_questions),
            projects_used=used_projects,
            projects_total=len(snapshot.projects),
        )
        system_notes.append(
            f"本次目标引用 {coverage.facts_used}/{coverage.facts_total} 条项目事实、"
            f"{coverage.questions_used}/{coverage.questions_total} 道启用题目；"
            "未被目标引用的资料不会进入本场考察范围。"
        )

        policy_notes = [
            "目标时长仅作节奏参考，到时不强制结束；追问受总上限与各目标上限约束。",
            "计划只含考察目标与预算，不含固定问题清单；具体问题由后续面试环节动态生成。",
        ]
        if mode == "rule":
            policy_notes.append(
                "本计划由规则模式生成，未调用模型；配置好 Qwen 后可重新生成。"
            )

        project_count = sum(
            1 for item in objectives if item.source_preference == "project"
        )
        summary = (
            f"共 {len(objectives)} 个考察目标（项目题 {project_count} 个 / "
            f"题库题 {len(objectives) - project_count} 个）；主题时间 {topic_sec // 60} 分钟，"
            f"其中项目题 {project_sec // 60} 分钟、题库题 {bank_sec // 60} 分钟。"
            f"覆盖主题：{'、'.join(item.topic[:24] for item in objectives[:4])}"
            f"{'等' if len(objectives) > 4 else ''}。"
        )
        combined = (
            system_notes
            + [f"模型提示：{item}" for item in model_notes]
            + policy_notes
        )
        return PlanOutput(
            schema_version="planner.v1",
            session_summary=summary,
            objectives=objectives,
            source_time_budget_sec={"project": project_sec, "question_bank": bank_sec},
            opening_buffer_sec=buffer,
            closing_buffer_sec=buffer,
            duration_target_sec=duration_sec,
            constraints=PlanConstraints(
                max_total_followups=max_total,
                max_followups_per_objective=cap,
                disabled_topics=[],
                duration_is_soft=True,
            ),
            limitations=combined[:30],
            notes=PlanNotes(
                system=system_notes[:20], model=model_notes[:10], policy=policy_notes[:10]
            ),
            coverage=coverage,
            runtime_context=compile_runtime_context(objectives, snapshot),
        )

    async def create(self, payload: PlanCreate) -> tuple[PlanRead, bool]:
        plan_id = str(payload.request_id)
        inputs = PlanInputs.model_validate(payload.model_dump(exclude={"request_id"}))
        snapshot: PlanSnapshot | None = None
        limitations: list[str] = []
        try:
            async with self.session.begin():
                existing = await self.session.get(InterviewPlan, plan_id)
                if existing is not None:
                    if existing.deleted_at is not None:
                        raise PlannerError(
                            "plan_request_conflict", "请求标识已用于已删除的计划", 409
                        )
                    if PlanInputs.model_validate(existing.inputs_json) != inputs:
                        raise PlannerError(
                            "plan_request_conflict", "请求标识已用于其他配置", 409
                        )
                    self.expire(existing)
                    await self.session.flush()
                    return self.read(existing), False
                snapshot, limitations = await self.build_snapshot(inputs)
                if not snapshot.questions and not any(
                    project.facts for project in snapshot.projects
                ):
                    raise PlannerError(
                        "insufficient_materials",
                        "请至少选择一道启用题目，或选择带事实条目的已核对项目后再生成计划",
                    )
                now = utc_now()
                timeout = (
                    self.settings.qwen_timeout_seconds if inputs.mode == "model" else 60
                )
                row = InterviewPlan(
                    id=plan_id,
                    resume_id=str(inputs.resume_id),
                    source_version=inputs.source_version,
                    status="running",
                    provider="qwen" if inputs.mode == "model" else "rule",
                    model=(self.settings.qwen_model or "") if inputs.mode == "model" else "",
                    prompt_version=(
                        PROMPT_VERSION if inputs.mode == "model" else RULE_PROMPT_VERSION
                    ),
                    schema_version=SCHEMA_VERSION,
                    inputs_json=inputs.model_dump(mode="json"),
                    snapshot_json=snapshot.model_dump(mode="json"),
                    result_json={},
                    created_at=now,
                    updated_at=now,
                    expires_at=now + timedelta(seconds=timeout + 30),
                )
                self.session.add(row)
                await self.session.flush()
        except IntegrityError:
            return await self.get(plan_id), False
        except StaleDataError:
            raise conflict() from None

        # 模型调用不持有数据库事务；一次 request_id 仅调用一次，不自动重试收费请求。
        assert snapshot is not None
        model = QwenModel(self.settings) if inputs.mode == "model" else None
        started = time.monotonic()
        output: PlanOutput | None = None
        error: ModelError | None = None
        try:
            if inputs.mode == "rule":
                draft = self.rule_draft(snapshot, inputs)
            else:
                if not configuration_status(self.settings)["configured"]:
                    raise ModelError(
                        "model_not_configured",
                        "请先配置 Qwen 密钥与模型；也可选择规则模式生成，不调用模型",
                    )
                assert model is not None
                draft = await model.generate(
                    messages=messages(snapshot, inputs),
                    output_type=PlannerDraft,
                    prompt_version=PROMPT_VERSION,
                    schema_version=SCHEMA_VERSION,
                )
            output = self.build_output(draft, inputs, snapshot, limitations, inputs.mode)
        except ModelError as failure:
            error = failure
        except PlannerError as failure:
            error = ModelError(failure.code, failure.message)
        except Exception as failure:
            logger.warning(
                "Plan generation failed; plan_id=%s error_type=%s",
                plan_id, type(failure).__name__,
            )
            error = ModelError(
                "planner_internal_error",
                f"规划处理失败（{type(failure).__name__}），请查看后端日志",
            )
        elapsed_ms = (
            model.elapsed_ms if model is not None
            else round((time.monotonic() - started) * 1000)
        )

        try:
            async with self.session.begin():
                row = await self.require_plan(plan_id, lock=True)
                if row.status == "running":
                    row.elapsed_ms = elapsed_ms
                    row.input_tokens = model.usage.input_tokens if model else None
                    row.output_tokens = model.usage.output_tokens if model else None
                    if error is not None:
                        row.status = "failed"
                        row.error_code, row.error_message = error.code, error.message
                    else:
                        assert output is not None
                        row.status = "ready"
                        row.result_json = output.model_dump(mode="json")
                    row.updated_at = utc_now()
                    await self.session.flush()
                return self.read(row), True
        except StaleDataError:
            raise conflict() from None

    async def get(self, plan_id: str) -> PlanRead:
        try:
            async with self.session.begin():
                row = await self.require_plan(plan_id, lock=True)
                self.expire(row)
                await self.session.flush()
                return self.read(row)
        except StaleDataError:
            raise conflict() from None

    async def runtime(self, plan_id: str) -> PlanRuntime:
        """已确认计划的精简投影：实时链路一次读取即可使用，无需关联快照或再调模型。"""
        try:
            async with self.session.begin():
                row = await self.require_plan(plan_id, lock=True)
                self.expire(row)
                await self.session.flush()
                if row.status != "confirmed":
                    raise PlannerError(
                        "plan_not_confirmed", "只有已确认的计划可供面试运行时读取", 409
                    )
                if not row.result_json:
                    raise PlannerError("plan_not_ready", "计划没有可用结果", 409)
                result = PlanOutput.model_validate(row.result_json)
                objectives = [
                    PlanRuntimeObjective(
                        id=item.id,
                        source=item.source_preference,
                        priority=item.priority,
                        topic=item.topic,
                        time_budget_sec=item.time_budget_sec,
                        max_followups=item.max_followups,
                        context=result.runtime_context.get(item.id, item.topic),
                    )
                    for item in result.objectives
                ]
                return PlanRuntime(
                    plan_id=row.id,
                    status=row.status,
                    confirmed_at=row.confirmed_at,
                    resume_id=row.resume_id,
                    source_version=row.source_version,
                    schema_version=row.schema_version,
                    prompt_version=row.prompt_version,
                    duration_target_sec=result.duration_target_sec,
                    source_time_budget_sec=result.source_time_budget_sec,
                    constraints=result.constraints,
                    objectives=objectives,
                )
        except StaleDataError:
            raise conflict() from None

    async def list(self, limit: int, offset: int) -> PlanList:
        try:
            async with self.session.begin():
                conditions = [InterviewPlan.deleted_at.is_(None)]
                total = await self.session.scalar(
                    select(func.count()).select_from(InterviewPlan).where(*conditions)
                )
                rows = list(await self.session.scalars(
                    select(InterviewPlan).where(*conditions)
                    .order_by(InterviewPlan.created_at.desc(), InterviewPlan.id)
                    .limit(limit).offset(offset)
                ))
                for row in rows:
                    self.expire(row)
                await self.session.flush()
                items: list[PlanSummary] = []
                for row in rows:
                    inputs = PlanInputs.model_validate(row.inputs_json)
                    objectives = row.result_json.get("objectives")
                    items.append(PlanSummary(
                        id=row.id, status=row.status, provider=row.provider,
                        resume_id=row.resume_id, source_version=row.source_version,
                        direction=inputs.direction,
                        duration_minutes=inputs.duration_minutes,
                        objective_count=len(objectives) if isinstance(objectives, list) else 0,
                        created_at=row.created_at, confirmed_at=row.confirmed_at,
                    ))
                return PlanList(items=items, total=total or 0, limit=limit, offset=offset)
        except StaleDataError:
            raise conflict() from None

    async def verify_references(self, row: InterviewPlan) -> None:
        """确认前复核固定引用：资料被编辑或停用后不悄悄沿用旧计划。"""
        if not row.snapshot_json:
            raise stale("计划缺少资料快照，请重新生成")
        snapshot = PlanSnapshot.model_validate(row.snapshot_json)
        version = await self.session.get(ResumeVersion, (row.resume_id, row.source_version))
        if version is None or version.content_sha256 != snapshot.content_sha256:
            raise stale("所选简历版本已变化，请重新生成计划")
        project_ids = [str(item.id) for item in snapshot.projects]
        if project_ids:
            count = await self.session.scalar(
                select(func.count()).select_from(Project).where(
                    Project.id.in_(project_ids),
                    Project.deleted_at.is_(None),
                    Project.user_verified.is_(True),
                    Project.source_version == row.source_version,
                )
            )
            if count != len(project_ids):
                raise stale("部分项目已取消核对或删除，请重新生成计划")
        question_ids = [str(item.id) for item in snapshot.questions]
        if question_ids:
            current = {
                item.id: item.content_version
                for item in await self.session.scalars(
                    select(Question).where(
                        Question.id.in_(question_ids),
                        Question.deleted_at.is_(None),
                        Question.is_active.is_(True),
                    )
                )
            }
            if any(
                current.get(str(item.id)) != item.content_version
                for item in snapshot.questions
            ):
                raise stale("部分启用题目已修改或停用，请重新生成计划")

    async def confirm(self, plan_id: str, payload: PlanConfirm) -> PlanRead:
        try:
            async with self.session.begin():
                row = await self.require_plan(plan_id, lock=True)
                self.expire(row)
                if row.status == "confirmed":
                    if row.revision in {payload.expected_revision, payload.expected_revision + 1}:
                        return self.read(row)
                    raise conflict()
                if row.revision != payload.expected_revision:
                    raise conflict()
                if row.status != "ready":
                    raise PlannerError(
                        "plan_not_ready", "此计划还不能确认保存，请查看失败原因或重新生成", 409
                    )
                await self.verify_references(row)
                row.status = "confirmed"
                row.confirmed_at = utc_now()
                row.updated_at = row.confirmed_at
                await self.session.flush()
                return self.read(row)
        except StaleDataError:
            raise conflict() from None

    async def delete(self, plan_id: str, revision: int) -> None:
        try:
            async with self.session.begin():
                row = await self.require_plan(plan_id, lock=True)
                if row.revision != revision:
                    raise conflict()
                row.deleted_at = row.updated_at = utc_now()
                await self.session.flush()
        except StaleDataError:
            raise conflict() from None
