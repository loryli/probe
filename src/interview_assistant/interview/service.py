"""文字面试运行时：冻结计划、先落库回答再决策、状态守卫与断点续接。"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from interview_assistant.ai.qwen import ModelError, QwenModel, configuration_status
from interview_assistant.config import Settings
from interview_assistant.database import utc_now
from interview_assistant.interview import rules, variety
from interview_assistant.interview.models import (
    InterviewReport,
    InterviewSession,
    InterviewTurn,
)
from interview_assistant.interview.prompt import (
    DECISION_SCHEMA_VERSION,
    OUTLINE_PROMPT_VERSION,
    OUTLINE_SCHEMA_VERSION,
    PROMPT_VERSION,
    QUESTION_SCHEMA_VERSION,
    REPORT_PROMPT_VERSION,
    REPORT_SCHEMA_VERSION,
    decision_messages,
    outline_messages,
    question_messages,
    report_messages,
)
from interview_assistant.interview.schemas import (
    PROJECT_BASE_QUESTIONS,
    AnswerSubmit,
    DecisionDraft,
    ObjectiveProgress,
    ObjectiveState,
    OutlineDraft,
    OutlineObjective,
    OutlineQuestion,
    PlanOption,
    QuestionDraft,
    ReportContent,
    ReportCreate,
    ReportDraft,
    ReportEvaluation,
    ReportRead,
    SessionBatchDelete,
    SessionControl,
    SessionCreate,
    SessionList,
    SessionOptions,
    SessionOutline,
    SessionProgress,
    SessionRead,
    SessionSummary,
    TurnDecision,
    TurnRead,
)
from interview_assistant.planner.models import InterviewPlan
from interview_assistant.planner.schemas import (
    PlanInputs,
    PlanRuntime,
    PlanRuntimeObjective,
    SnapshotQuestion,
)
from interview_assistant.planner.service import PlannerError, PlannerService
from interview_assistant.planner.text import plain_text
from interview_assistant.interview.errors import InterviewError, conflict
from interview_assistant.interview.questioning import NextTurn, QuestioningService
from interview_assistant.interview.opening import OpeningService
from interview_assistant.interview.reporting import ReportService
from interview_assistant.interview.timing import active_seconds, close_pause, objective_seconds, settle_objectives

logger = logging.getLogger(__name__)

MAX_TURNS_IN_PROMPT = 3
OPTION_PLAN_LIMIT = 50
# 回答短于这个长度视为「没有可追问的内容」（如“不知道”），不追问细节，改问更基础的方面。
MIN_SUBSTANTIVE_ANSWER = 8
REPORT_MAX_TOKENS = 2000
# 决策只输出一小段 JSON，单独限长可以尽早截断跑偏的输出。
DECISION_MAX_TOKENS = 500


@dataclass(slots=True)
class DecisionContext:
    """一次决策所需的全部输入；读取与模型调用分离，模型调用不持有事务。"""

    runtime: PlanRuntime
    objective: PlanRuntimeObjective
    state: ObjectiveProgress
    progress: SessionProgress
    question_text: str
    answer_text: str
    recent: list[tuple[str, str]]
    elapsed: int
    remaining: int
    followups_remaining: int
    follow_up_allowed: bool
    base_remaining: int
    mode: str
    messages: list[object]


class InterviewService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        # 组合对象共享同一个 AsyncSession；事务边界仍由本类各方法持有，
        # 子对象不开启跨服务事务，也不缓存跨请求的数据库状态。
        self.questions = QuestioningService(self)
        self.opening = OpeningService(self)
        self.reports = ReportService(self)

    # ---------- 读取 ----------

    async def options(self) -> SessionOptions:
        rows = list(await self.session.scalars(
            select(InterviewPlan).where(
                InterviewPlan.deleted_at.is_(None),
                InterviewPlan.status == "confirmed",
            ).order_by(InterviewPlan.confirmed_at.desc(), InterviewPlan.id)
            .limit(OPTION_PLAN_LIMIT)
        ))
        plans: list[PlanOption] = []
        for row in rows:
            inputs = PlanInputs.model_validate(row.inputs_json)
            objectives = (row.result_json or {}).get("objectives")
            usable, reason = True, ""
            # 旧版计划无法冻结运行依据；先在这里判定，避免选完才报 409。
            try:
                PlannerService.current_contract(row)
            except PlannerError as error:
                usable, reason = False, error.message
            except ValueError:
                usable, reason = False, "这份计划的资料快照不完整，请重新生成并确认"
            plans.append(PlanOption(
                practice_mode="resume" if inputs.resume_id else "question_bank",
                id=UUID(row.id),
                direction=inputs.direction,
                duration_minutes=inputs.duration_minutes,
                objective_count=len(objectives) if isinstance(objectives, list) else 0,
                usable=usable,
                reason=reason,
                confirmed_at=row.confirmed_at,
                created_at=row.created_at,
            ))
        # 稳定排序：可用计划在前，页面默认选中第一项即可开始。
        plans.sort(key=lambda item: not item.usable)
        return SessionOptions(plans=plans, model=configuration_status(self.settings))

    async def require_session(self, session_id: str, *, lock: bool = False) -> InterviewSession:
        query = (
            select(InterviewSession)
            .where(InterviewSession.id == session_id)
            .execution_options(populate_existing=True)
        )
        row = await self.session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise InterviewError("session_not_found", "面试场次不存在", 404)
        return row

    async def turns(self, session_id: str) -> list[InterviewTurn]:
        return list(await self.session.scalars(
            select(InterviewTurn).where(InterviewTurn.session_id == session_id)
            .order_by(InterviewTurn.sequence, InterviewTurn.id)
        ))

    async def _next_sequence(self, session_id: str) -> int:
        value = await self.session.scalar(
            select(func.max(InterviewTurn.sequence))
            .where(InterviewTurn.session_id == session_id)
        )
        return (value or 0) + 1

    @staticmethod
    def elapsed(row: InterviewSession, progress: SessionProgress) -> int:
        return active_seconds(row.started_at, row.ended_at, row.status, row.updated_at, progress)

    @staticmethod
    def _followup_allowance(
        runtime: PlanRuntime, objective: PlanRuntimeObjective,
        state: ObjectiveProgress, progress: SessionProgress,
        *, question_is_followup: bool = False,
    ) -> int:
        """本目标此刻还能追问几次；决策提示与落库共用这一处判断。

        混合面试的题库原题至多追加一次追问，不能把之前未用的额度连续用在同一道题上。
        是否已经追问从所回答的真实轮次判断，不依赖模型或累计游标猜测。
        """
        if runtime.resume_id is None:
            return 0
        if objective.source == "question_bank" and (question_is_followup or state.cursor == 0):
            return 0
        allowance = max(0, min(
            objective.max_followups - state.followups,
            runtime.constraints.max_total_followups - progress.followups_used,
            runtime.constraints.max_followups_per_objective - state.followups,
        ))
        if objective.source == "question_bank":
            allowance = min(allowance, max(0, state.cursor - state.followups))
        return allowance

    def read(self, row: InterviewSession, turns: list[InterviewTurn]) -> SessionRead:
        runtime = PlanRuntime.model_validate(row.runtime_json)
        progress = SessionProgress.model_validate(row.progress_json)
        elapsed = self.elapsed(row, progress)
        states = [
            ObjectiveState(
                id=objective.id,
                topic=objective.topic,
                source=objective.source,
                priority=objective.priority,
                status=progress.state(objective.id).status,
                asked=progress.state(objective.id).asked,
                followups=progress.state(objective.id).followups,
                max_followups=objective.max_followups,
                time_budget_sec=objective.time_budget_sec,
                elapsed_sec=objective_seconds(progress.state(objective.id), elapsed),
            )
            for objective in runtime.objectives
        ]
        answered = {turn.answers_turn_id for turn in turns if turn.answers_turn_id}
        reads = [
            TurnRead(
                stage=next((stage for stage, question_id in progress.opening_questions.items()
                            if turn.id == question_id or turn.answers_turn_id == question_id), "technical"),
                id=UUID(turn.id),
                sequence=turn.sequence,
                role=turn.role,
                objective_id=turn.objective_id,
                source=turn.source,
                question_id=UUID(turn.question_id) if turn.question_id else None,
                is_followup=turn.is_followup,
                text=turn.text,
                answered=turn.role == "candidate" or turn.id in answered,
                skipped=(
                    turn.role == "interviewer"
                    and turn.id not in answered
                    and turn.id != row.current_turn_id
                ),
                decision=(
                    TurnDecision.model_validate(turn.decision_json)
                    if turn.decision_json else None
                ),
                created_at=turn.created_at,
            )
            for turn in turns
        ]
        current = next(
            (item for item in reads if row.current_turn_id and str(item.id) == row.current_turn_id),
            None,
        )
        pending = next(
            (turn for turn in turns if turn.role == "candidate" and turn.decision_json is None),
            None,
        )
        return SessionRead(
            phase=progress.phase,
            practice_mode="resume" if runtime.resume_id else "question_bank",
            id=UUID(row.id),
            plan_id=UUID(row.plan_id),
            plan_revision=row.plan_revision,
            status=row.status,
            revision=row.revision,
            mode=row.mode,
            provider=row.provider,
            model=row.model,
            prompt_version=row.prompt_version,
            schema_version=row.schema_version,
            direction=row.direction,
            duration_target_sec=runtime.duration_target_sec,
            elapsed_sec=elapsed,
            remaining_sec=runtime.duration_target_sec - elapsed,
            objectives=states,
            turns=reads,
            current_turn=current,
            pending_turn_id=UUID(pending.id) if pending is not None else None,
            answer_count=sum(1 for turn in turns if turn.role == "candidate"),
            question_count=sum(1 for turn in turns if turn.role == "interviewer"),
            followups_used=progress.followups_used,
            completion_reason=progress.completion_reason,
            error_code=row.error_code,
            error_message=row.error_message,
            started_at=row.started_at,
            ended_at=row.ended_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def get(self, session_id: str) -> SessionRead:
        async with self.session.begin():
            row = await self.require_session(session_id)
            return self.read(row, await self.turns(session_id))

    async def list_sessions(self, limit: int, offset: int) -> SessionList:
        try:
            async with self.session.begin():
                total = await self.session.scalar(
                    select(func.count()).select_from(InterviewSession)
                ) or 0
                rows = list(await self.session.execute(
                    select(
                        InterviewSession.id,
                        InterviewSession.plan_id,
                        InterviewSession.status,
                        InterviewSession.mode,
                        InterviewSession.direction,
                        InterviewSession.progress_json,
                        InterviewSession.started_at,
                        InterviewSession.ended_at,
                        InterviewSession.created_at,
                        InterviewSession.updated_at,
                    ).order_by(InterviewSession.created_at.desc(), InterviewSession.id)
                    .limit(limit).offset(offset)
                ))
                counts: dict[str, dict[str, int]] = {}
                ids = [row.id for row in rows]
                if ids:
                    grouped = await self.session.execute(
                        select(InterviewTurn.session_id, InterviewTurn.role, func.count())
                        .where(InterviewTurn.session_id.in_(ids))
                        .group_by(InterviewTurn.session_id, InterviewTurn.role)
                    )
                    for session_id, role, value in grouped.all():
                        counts.setdefault(session_id, {})[role] = int(value)
                items: list[SessionSummary] = []
                for row in rows:
                    progress = SessionProgress.model_validate(row.progress_json)
                    items.append(SessionSummary(
                        id=UUID(row.id),
                        plan_id=UUID(row.plan_id),
                        status=row.status,
                        mode=row.mode,
                        direction=row.direction,
                        answer_count=counts.get(row.id, {}).get("candidate", 0),
                        question_count=counts.get(row.id, {}).get("interviewer", 0),
                        objective_total=len(progress.objectives),
                        objective_done=sum(
                            1 for item in progress.objectives
                            if item.status in {"covered", "skipped"}
                        ),
                        elapsed_sec=active_seconds(row.started_at, row.ended_at, row.status, row.updated_at, progress),
                        created_at=row.created_at,
                        started_at=row.started_at,
                        ended_at=row.ended_at,
                    ))
                return SessionList(items=items, total=total, limit=limit, offset=offset)
        except StaleDataError:
            raise conflict() from None

    async def delete_sessions(self, payload: SessionBatchDelete) -> int:
        """物理删除面试记录：场次、全部问答轮次与整场复盘一起删除。

        进行中的场次不允许删除（先结束或暂停），避免与后台生成任务互相覆盖；
        计划不依赖场次存在，已确认计划不受影响。
        """
        ids = sorted({str(item) for item in payload.session_ids})
        async with self.session.begin():
            rows = {
                row.id: row
                for row in await self.session.scalars(
                    select(InterviewSession).where(InterviewSession.id.in_(ids))
                )
            }
            for session_id in ids:
                row = rows.get(session_id)
                if row is None:
                    raise InterviewError(
                        "session_not_found", "场次不存在或已被删除，请刷新记录列表", 404
                    )
                if row.status == "active":
                    raise InterviewError(
                        "session_active",
                        "进行中的场次不能删除，请先结束或暂停这场面试",
                        409,
                    )
            # 删除顺序：复盘 → 回答轮（持有 answers_turn_id 引用）→ 问题轮 → 场次。
            await self.session.execute(
                delete(InterviewReport).where(InterviewReport.session_id.in_(ids))
            )
            await self.session.execute(
                delete(InterviewTurn).where(
                    InterviewTurn.session_id.in_(ids),
                    InterviewTurn.answers_turn_id.is_not(None),
                )
            )
            await self.session.execute(
                delete(InterviewTurn).where(InterviewTurn.session_id.in_(ids))
            )
            for session_id in ids:
                await self.session.delete(rows[session_id])
            await self.session.flush()
        return len(ids)

    async def plan_runtime(self, plan_id: str) -> PlanRuntime:
        try:
            return await PlannerService(self.session, self.settings).runtime(plan_id)
        except PlannerError as error:
            raise InterviewError(error.code, error.message, error.status) from None

    # ---------- 创建与出题 ----------

    async def create(self, payload: SessionCreate) -> tuple[SessionRead, bool]:
        session_id = str(payload.request_id)
        plan_id = str(payload.plan_id)
        # 先查重放：重放不需要读取计划，计划之后被删除也不影响已建场次。
        try:
            async with self.session.begin():
                existing = await self.session.get(InterviewSession, session_id)
                if existing is not None:
                    if existing.plan_id != plan_id or existing.mode != payload.mode:
                        raise InterviewError(
                            "session_request_conflict", "请求标识已用于其他场次配置", 409
                        )
                    return self.read(existing, await self.turns(session_id)), False
        except StaleDataError:
            raise conflict() from None

        runtime = await self.plan_runtime(plan_id)
        try:
            async with self.session.begin():
                now = utc_now()
                progress = SessionProgress(
                    objectives=[ObjectiveProgress(id=item.id) for item in runtime.objectives],
                    started_at=now,
                    phase="self_introduction" if runtime.resume_id is not None else "technical",
                )
                row = InterviewSession(
                    id=session_id,
                    plan_id=plan_id,
                    plan_revision=runtime.plan_revision,
                    status="active",
                    mode=payload.mode,
                    provider="qwen" if payload.mode == "model" else "rule",
                    model=(self.settings.qwen_model or "") if payload.mode == "model" else "",
                    prompt_version=(
                        PROMPT_VERSION if payload.mode == "model"
                        else rules.RULE_PROMPT_VERSION
                    ),
                    schema_version=DECISION_SCHEMA_VERSION,
                    direction=runtime.direction[:500],
                    runtime_json=runtime.model_dump(mode="json"),
                    progress_json=progress.model_dump(mode="json"),
                    started_at=now,
                    created_at=now,
                    updated_at=now,
                )
                self.session.add(row)
                await self.session.flush()
        except IntegrityError:
            async with self.session.begin():
                existing = await self.session.get(InterviewSession, session_id)
                if (existing is None or existing.plan_id != plan_id
                        or existing.mode != payload.mode):
                    raise InterviewError(
                        "session_request_conflict", "请求标识已用于其他场次配置", 409
                    ) from None
                return self.read(existing, await self.turns(session_id)), False
        except StaleDataError:
            raise conflict() from None

        # 简历面试先保存介绍；技术问题在介绍完成后按本场上下文生成。
        await self._ask_next(session_id)
        return await self.get(session_id), True


    async def _ask_next(self, session_id: str) -> None:
        """出题的兜底边界：未预期异常变成场次上的可见错误，而不是 500。

        场次已经建好、回答也已落库，出题失败不该让整页报错；记下错误码后，
        前端可以点「继续生成下一问」重试这一小步。
        """
        try:
            await self._ask_next_question(session_id)
        except InterviewError:
            raise
        except Exception:
            logger.exception("Interview next question failed; session_id=%s", session_id)
            await self.set_error(session_id, ModelError(
                "next_question_failed", "下一步生成未完成，可点「继续生成下一问」重试",
            ))

    async def _ask_next_question(self, session_id: str) -> None:
        """为下一个待考察目标出题；题库题直接用用户原题，不调用模型。"""
        if await self.opening.ensure_question(session_id):
            return
        for _ in range(16):
            async with self.session.begin():
                row = await self.require_session(session_id, lock=True)
                if row.status != "active" or row.current_turn_id is not None:
                    return
                runtime = PlanRuntime.model_validate(row.runtime_json)
                progress = SessionProgress.model_validate(row.progress_json)
                elapsed = self.elapsed(row, progress)
                mode = row.mode
                plan_id = row.plan_id
                outline = SessionOutline.model_validate(row.outline_json or {})
                # 事务内先取历史用量与近期问题，块外的取题与出题只用缓存。
                await self._question_usage(plan_id, session_id)
                await self._avoid_questions(plan_id, session_id)
                previous_statuses = [item.status for item in progress.objectives]
                picked = self._next_pending(
                    runtime, progress, session_id=session_id,
                    preferred=self.opening.preferred_objective(runtime, progress, await self.turns(session_id)),
                )
                if previous_statuses != [item.status for item in progress.objectives]:
                    row.progress_json = progress.model_dump(mode="json")
                    row.updated_at = utc_now()
                    await self.session.flush()
            if picked is None:
                await self._complete(session_id, "所有考察目标已完成，场次自动结束。")
                return
            objective, state = picked
            if objective.source == "question_bank":
                snapshot = self._pick_bank(
                    self._bank_order(plan_id, session_id, runtime, objective), state.cursor,
                )
                if snapshot is None:
                    await self._mark(session_id, objective.id, "skipped")
                    continue
                await self._open_turn(session_id, NextTurn(
                    objective_id=objective.id, source="question_bank",
                    text=snapshot.question_text,
                    question_id=str(snapshot.id), activate=True, advance_cursor=True,
                ))
                return
            # 骨架优先：命中就是零模型调用；缺失时只为这一个目标补一次。
            outline_text = self._outline_question(outline, objective, state)
            if not outline_text and mode == "model":
                await self._ensure_outline(session_id, objectives=[objective])
                async with self.session.begin():
                    row = await self.require_session(session_id)
                    outline = SessionOutline.model_validate(row.outline_json or {})
                outline_text = self._outline_question(outline, objective, state)
            if outline_text:
                await self._open_turn(session_id, NextTurn(
                    objective_id=objective.id, source="project",
                    text=outline_text, activate=True,
                ))
                return
            if mode == "rule":
                await self._open_turn(session_id, NextTurn(
                    objective_id=objective.id, source="project",
                    text=self._project_question(
                        runtime, objective, state, seed=variety.session_seed(session_id),
                    ), activate=True,
                ))
                return
            draft, error = await self._ask_model_question(
                session_id, runtime, objective, elapsed,
            )
            if draft is None:
                if error is not None:
                    await self.set_error(session_id, error)
                return
            await self._open_turn(session_id, NextTurn(
                objective_id=objective.id, source="project",
                text=draft.question, activate=True,
            ))
            return
        await self._complete(session_id, "没有可继续提问的目标，场次已结束。")

    async def _ask_model_question(
        self, session_id: str, runtime: PlanRuntime,
        objective: PlanRuntimeObjective, elapsed: int,
    ) -> tuple[QuestionDraft | None, ModelError | None]:
        if not configuration_status(self.settings)["configured"]:
            return None, ModelError(
                "model_not_configured",
                "请先配置 Qwen 密钥与模型；也可用规则模式重开一场",
            )
        model = QwenModel(self.settings)
        async with self.session.begin():
            row = await self.require_session(session_id)
            introduction = self.opening.context(
                SessionProgress.model_validate(row.progress_json), await self.turns(session_id),
            )
        try:
            draft = await model.generate(
                messages=question_messages(
                    runtime=runtime, objective=objective, elapsed_sec=elapsed,
                    avoid=self._avoid.get(session_id, []),
                    introduction=introduction,
                ),
                output_type=QuestionDraft,
                prompt_version=PROMPT_VERSION,
                schema_version=QUESTION_SCHEMA_VERSION,
                temperature=self.settings.qwen_interview_temperature,
            )
        except ModelError as failure:
            return None, failure
        assert isinstance(draft, QuestionDraft)
        return draft, None

    async def _open_turn(self, session_id: str, plan: NextTurn) -> bool:
        try:
            async with self.session.begin():
                row = await self.require_session(session_id, lock=True)
                if row.status != "active" or row.current_turn_id is not None:
                    return False
                progress = SessionProgress.model_validate(row.progress_json)
                try:
                    state = progress.state(plan.objective_id)
                except KeyError:
                    return False
                if plan.activate and state.status in {"covered", "skipped"}:
                    return False
                await self._create_turn(row, progress, plan)
                row.updated_at = utc_now()
                row.error_code = None
                row.error_message = None
                return True
        except StaleDataError:
            raise conflict() from None

    async def _create_turn(
        self, row: InterviewSession, progress: SessionProgress, plan: NextTurn,
    ) -> InterviewTurn:
        settle_objectives(progress, self.elapsed(row, progress), plan.objective_id)
        state = progress.state(plan.objective_id)
        if plan.activate:
            state.status = "active"
        if plan.advance_cursor:
            state.cursor += 1
        state.asked += 1
        if plan.is_followup:
            state.followups += 1
            progress.followups_used += 1
        turn = InterviewTurn(
            id=str(uuid4()),
            session_id=row.id,
            sequence=await self._next_sequence(row.id),
            role="interviewer",
            objective_id=plan.objective_id,
            source=plan.source,
            question_id=plan.question_id,
            is_followup=plan.is_followup,
            text=plan.text if plan.question_id else plan.text[:2000],
            created_at=utc_now(),
        )
        self.session.add(turn)
        await self.session.flush()
        row.current_turn_id = turn.id
        row.progress_json = progress.model_dump(mode="json")
        return turn

    # ---------- 回答与决策 ----------

    async def answer(
        self, session_id: str, payload: AnswerSubmit,
    ) -> tuple[SessionRead, str | None]:
        """只负责把回答落库并提交；返回待决策的回答轮次，由调用方决定是否后台执行。"""
        turn_id = str(payload.turn_id)
        request_key = str(payload.request_id)
        try:
            async with self.session.begin():
                row = await self.require_session(session_id, lock=True)
                if row.current_turn_id != turn_id:
                    replayed = await self.session.scalar(
                        select(InterviewTurn).where(
                            InterviewTurn.request_key == request_key,
                            InterviewTurn.session_id == session_id,
                        )
                    )
                    if replayed is not None:
                        return self.read(row, await self.turns(session_id)), None
                    if row.status != "active":
                        raise InterviewError(
                            "session_not_active", "场次已暂停或结束，回答未提交", 409
                        )
                    raise InterviewError(
                        "turn_not_current", "这道题已不是当前问题，请刷新后重试", 409
                    )
                if row.status != "active":
                    raise InterviewError(
                        "session_not_active", "场次已暂停或结束，回答未提交", 409
                    )
                question = await self.session.get(InterviewTurn, turn_id)
                if (question is None or question.session_id != session_id
                        or question.role != "interviewer"):
                    raise InterviewError("turn_not_found", "题目不存在", 404)
                answered = await self.session.scalar(
                    select(InterviewTurn.id).where(InterviewTurn.answers_turn_id == turn_id)
                )
                if answered is not None:
                    raise InterviewError(
                        "turn_already_answered", "这道题已提交过回答，请刷新后继续", 409
                    )
                answer_turn = InterviewTurn(
                    id=str(uuid4()),
                    session_id=session_id,
                    sequence=await self._next_sequence(session_id),
                    role="candidate",
                    objective_id=question.objective_id,
                    source=question.source,
                    question_id=question.question_id,
                    is_followup=False,
                    text=payload.answer_text,
                    answers_turn_id=question.id,
                    request_key=request_key,
                    created_at=utc_now(),
                )
                self.session.add(answer_turn)
                await self.session.flush()
                # 回答已落库；清空当前问题指针，下一步决策失败时可按回答续接。
                row.current_turn_id = None
                row.updated_at = utc_now()
                answer_turn_id = answer_turn.id
        except IntegrityError:
            # 唯一约束拦截重复提交：只有同一幂等键才按重放返回，其他冲突明确报错。
            async with self.session.begin():
                row = await self.require_session(session_id)
                replayed = await self.session.scalar(
                    select(InterviewTurn).where(
                        InterviewTurn.request_key == request_key,
                        InterviewTurn.session_id == session_id,
                    )
                )
                if replayed is None:
                    raise InterviewError(
                        "answer_conflict", "这条回答与另一个提交冲突，请刷新后重试", 409
                    ) from None
                return self.read(row, await self.turns(session_id)), None
        except StaleDataError:
            raise conflict() from None

        # 模型调用不持有数据库事务；回答已提交，决策失败也不丢。
        return await self.get(session_id), answer_turn_id

    async def retry(self, session_id: str) -> SessionRead:
        async with self.session.begin():
            row = await self.require_session(session_id)
            if row.status == "paused":
                raise InterviewError("session_not_active", "场次已暂停，请先继续", 409)
            if row.status == "completed":
                return self.read(row, await self.turns(session_id))
            turns = await self.turns(session_id)
            pending = next(
                (turn for turn in reversed(turns)
                 if turn.role == "candidate" and turn.decision_json is None),
                None,
            )
            pending_id = pending.id if pending is not None else None
            has_current = row.current_turn_id is not None
        if pending_id is not None:
            await self.decide(session_id, pending_id)
        elif not has_current:
            await self._ask_next(session_id)
        else:
            raise InterviewError("no_pending_step", "当前没有问题待生成", 409)
        return await self.get(session_id)

    async def decide(self, session_id: str, answer_turn_id: str) -> None:
        """不带流的决策（重试与后台兜底用）；与流式路径共用同一套判定逻辑。"""
        await self._run_decision(session_id, answer_turn_id, emit=None)

    async def decide_stream(
        self, session_id: str, answer_turn_id: str, emit: Callable[[str], None],
    ) -> None:
        """模型内部流式接收；仅将校验并保存后的实际问题交给 SSE 展示。"""
        await self._run_decision(session_id, answer_turn_id, emit=emit)

    async def _decision_context(
        self, session_id: str, answer_turn_id: str,
    ) -> DecisionContext | None:
        """在事务内读完决策所需的全部输入；返回 None 表示无需决策。"""
        async with self.session.begin():
            row = await self.require_session(session_id)
            if row.status != "active":
                return None
            runtime = PlanRuntime.model_validate(row.runtime_json)
            progress = SessionProgress.model_validate(row.progress_json)
            turns = await self.turns(session_id)
            answer_turn = next((item for item in turns if item.id == answer_turn_id), None)
            if (answer_turn is None or answer_turn.decision_json is not None
                    or answer_turn.objective_id is None):
                return None
            objective = self._objective(runtime, answer_turn.objective_id)
            if objective is None:
                return None
            state = progress.state(objective.id)
            question_turn = next(
                (item for item in turns if item.id == answer_turn.answers_turn_id), None
            )
            question_text = question_turn.text if question_turn is not None else ""
            recent = [
                (item.role, item.text) for item in turns
                if item.sequence < answer_turn.sequence
                and item.role in {"interviewer", "candidate"}
            ][-MAX_TURNS_IN_PROMPT:]
            elapsed = self.elapsed(row, progress)
            remaining = runtime.duration_target_sec - elapsed
            followups_remaining = self._followup_allowance(
                runtime, objective, state, progress,
                question_is_followup=question_turn is None or question_turn.is_followup,
            )
            follow_up_allowed = (
                followups_remaining > 0 and remaining > -60
                and objective_seconds(state, elapsed) < objective.time_budget_sec
            )
            base_remaining = self._base_remaining(objective, state)
            mode = row.mode
            answer_text = answer_turn.text
            asked = [
                plain_text(item.text)[:100] for item in turns
                if item.role == "interviewer" and item.sequence < answer_turn.sequence
            ][-4:]
            # 同计划历史场次问过的问题一并给模型，避免跨场重复。
            avoid = await self._avoid_questions(row.plan_id, session_id)
            await self._question_usage(row.plan_id, session_id)
            messages: list[object] = []
            if mode != "rule":
                messages = list(decision_messages(
                    runtime=runtime, objective=objective, state=state, progress=progress,
                    question_text=question_text, answer_text=answer_text, recent=recent,
                    elapsed_sec=elapsed, remaining_sec=remaining,
                    followups_remaining=followups_remaining if follow_up_allowed else 0,
                    base_remaining=base_remaining,
                    asked=asked, avoid=avoid,
                    introduction=self.opening.context(progress, turns),
                ))
        return DecisionContext(
            runtime=runtime, objective=objective, state=state, progress=progress,
            question_text=question_text, answer_text=answer_text, recent=recent,
            elapsed=elapsed, remaining=remaining,
            followups_remaining=followups_remaining, follow_up_allowed=follow_up_allowed,
            base_remaining=base_remaining, mode=mode, messages=messages,
        )

    async def _run_decision(
        self, session_id: str, answer_turn_id: str, *, emit: Callable[[str], None] | None,
    ) -> None:
        opening_step = await self.opening.advance_answer(session_id, answer_turn_id)
        if opening_step is not None:
            if opening_step:
                await self._ask_next(session_id)
                result = await self.get(session_id)
                if emit is not None and result.current_turn is not None:
                    emit(result.current_turn.text)
            return
        context = await self._decision_context(session_id, answer_turn_id)
        if context is None:
            return
        model: QwenModel | None = None
        if context.mode == "rule":
            decision = rules.decide(
                context.answer_text,
                followups_remaining=(
                    context.followups_remaining if context.follow_up_allowed else 0
                ),
                base_remaining=context.base_remaining,
            )
            elapsed_ms = 0
        else:
            if not configuration_status(self.settings)["configured"]:
                await self.set_error(session_id, ModelError(
                    "model_not_configured",
                    "请先配置 Qwen 密钥与模型；也可用规则模式重开一场",
                ))
                return
            model = QwenModel(self.settings)
            started = time.monotonic()
            try:
                async for _ in model.generate_stream(
                    messages=context.messages,  # type: ignore[arg-type]
                    output_type=DecisionDraft,
                    prompt_version=PROMPT_VERSION,
                    schema_version=DECISION_SCHEMA_VERSION,
                    temperature=self.settings.qwen_interview_temperature,
                    max_tokens=DECISION_MAX_TOKENS,
                ):
                    # 未校验的措辞不能先展示：额度、来源和并发状态以落库时为准。
                    pass
            except ModelError as failure:
                await self.set_error(session_id, failure)
                return
            elapsed_ms = round((time.monotonic() - started) * 1000)
            draft = model.result
            if not isinstance(draft, DecisionDraft):
                await self.set_error(session_id, ModelError(
                    "model_invalid_output", "模型结果未通过校验，本步未保存",
                ))
                return
            decision = draft
        spoken = await self._apply(session_id, answer_turn_id, decision, elapsed_ms, model)
        if emit is not None and spoken:
            emit(spoken)

    async def _apply(
        self,
        session_id: str,
        answer_turn_id: str,
        decision: DecisionDraft,
        elapsed_ms: int,
        model: QwenModel | None,
    ) -> str | None:
        """写入决策与下一问前重新加锁校验：暂停、结束或已推进时丢弃模型结果。"""
        saved_question: str | None = None
        try:
            async with self.session.begin():
                row = await self.require_session(session_id, lock=True)
                progress = SessionProgress.model_validate(row.progress_json)
                runtime = PlanRuntime.model_validate(row.runtime_json)
                outline = SessionOutline.model_validate(row.outline_json or {})
                turns = await self.turns(session_id)
                answer_turn = await self.session.get(InterviewTurn, answer_turn_id)
                if answer_turn is None or answer_turn.decision_json is not None:
                    return
                latest = max((item.sequence for item in turns), default=0)
                if row.status != "active" or answer_turn.sequence < latest:
                    logger.info(
                        "Interview decision discarded; session_id=%s status=%s"
                        " sequence=%d latest=%d",
                        session_id, row.status, answer_turn.sequence, latest,
                    )
                    return
                objective = self._objective(runtime, answer_turn.objective_id)
                if objective is None:
                    return
                state = progress.state(objective.id)
                elapsed = self.elapsed(row, progress)
                remaining = runtime.duration_target_sec - elapsed
                question_turn = next(
                    (item for item in turns if item.id == answer_turn.answers_turn_id), None,
                )
                followups_remaining = self._followup_allowance(
                    runtime, objective, state, progress,
                    question_is_followup=question_turn is None or question_turn.is_followup,
                )
                # 目标时长只作节奏参考：超时不再新增追问，但仍可切目标或同目标续问，
                # 不因到时强制结束；结束由用户主动操作或所有考察目标完成后触发。
                follow_up_allowed = (
                    followups_remaining > 0 and remaining > -60
                    and objective_seconds(state, elapsed) < objective.time_budget_sec
                )
                if decision.follow_up_needed and not follow_up_allowed:
                    # 模型要追问但服务端不放行：留下的必须是基础题，不能让同一句改成普通题入库。
                    logger.info(
                        "Interview follow-up rejected by server; session_id=%s objective=%s"
                        " source=%s allowance=%d used=%d cursor=%d elapsed=%d budget=%d",
                        session_id, objective.id, objective.source, followups_remaining,
                        state.followups, state.cursor, objective_seconds(state, elapsed),
                        objective.time_budget_sec,
                    )
                base_remaining = self._base_remaining(objective, state)
                # 题库取题顺序依赖历史用量；在本事务内取一次即可。
                await self._question_usage(row.plan_id, session_id)

                next_turn: NextTurn | None = None
                action = "switch"
                # 回答几乎没有内容（如“不知道”）时不再追问细节，换更基础的问法或直接切题。
                answer_text = answer_turn.text or ""
                substantive = len(answer_text.strip()) >= MIN_SUBSTANTIVE_ANSWER
                spoken = decision.spoken
                followup_requested = decision.follow_up_needed
                repeated = any(
                    "".join(turn.text.split()).casefold() == "".join(spoken.split()).casefold()
                    for turn in turns if turn.role == "interviewer" and spoken
                )
                preferred = self._objective(runtime, decision.next_objective_id)
                switch_requested = (
                    not followup_requested and preferred is not None
                    and preferred.id != objective.id
                    and progress.state(preferred.id).status in {"pending", "active"}
                    and self._base_remaining(preferred, progress.state(preferred.id)) > 0
                )
                # 时间预算的运行时节奏：本目标已用满自身预算且还有其他未完成目标时，
                # 基础题也不再留在本目标，交回下方 _next_turn 按题源份额选目标。
                # 本目标状态保持不变（剩余基础题不丢），其他目标问完后仍会回来问完；
                # 不因到时强制结束；纯题库模式不适用（承诺问完所选原题）。
                defer_for_budget = (
                    runtime.resume_id is not None
                    and base_remaining > 0
                    and any(
                        item.id != objective.id
                        and progress.state(item.id).status in {"pending", "active"}
                        for item in runtime.objectives
                    )
                    and objective_seconds(state, elapsed) >= objective.time_budget_sec
                )
                if runtime.resume_id is None:
                    # 纯题库模式不接受模型自拟追问，也不因某次回答达标跳过同组剩余原题。
                    snapshot = self._pick_bank(
                        self._bank_order(row.plan_id, session_id, runtime, objective), state.cursor,
                    ) if base_remaining > 0 else None
                    if snapshot is not None:
                        next_turn = NextTurn(objective.id, "question_bank", snapshot.question_text,
                            question_id=str(snapshot.id), advance_cursor=True)
                        action = "continue"
                    else:
                        state.status = "covered"
                elif decision.objective_status == "covered":
                    state.status = "covered"
                elif (follow_up_allowed and substantive and spoken and not repeated
                      and followup_requested):
                    next_turn = NextTurn(
                        objective.id, objective.source,
                        spoken[:300], is_followup=True,
                    )
                    action = "follow_up"
                elif defer_for_budget:
                    # 超预算让位：不改状态、不出本目标基础题，走 _next_turn 按份额切目标。
                    pass
                elif switch_requested:
                    state.status = "covered"
                elif base_remaining > 0 and objective.source == "question_bank":
                    snapshot = self._pick_bank(
                        self._bank_order(row.plan_id, session_id, runtime, objective),
                        state.cursor,
                    )
                    if snapshot is not None:
                        next_turn = NextTurn(
                            objective.id, "question_bank",
                            snapshot.question_text,
                            question_id=str(snapshot.id), advance_cursor=True,
                        )
                        action = "continue"
                    else:
                        state.status = "covered"
                elif base_remaining > 0:
                    next_turn = NextTurn(
                        objective.id, "project",
                        self._outline_question(outline, objective, state)
                        or self._project_question(
                            runtime, objective, state,
                            seed=variety.session_seed(session_id),
                        ),
                    )
                    action = "continue"
                else:
                    state.status = "covered"

                if next_turn is not None:
                    created_turn = await self._create_turn(row, progress, next_turn)
                    saved_question = created_turn.text
                else:
                    picked = await self._next_turn(
                        runtime, progress, outline=outline,
                        plan_id=row.plan_id, session_id=session_id,
                        preferred=preferred.id if switch_requested and preferred is not None else "",
                        # 超预算让位时排除本目标，避免份额最低时又选回自己；
                        # 只剩本目标时 _next_pending 会照常返回，保证题被问完。
                        exclude=objective.id if defer_for_budget else "",
                        # 仅接受明确指向另一个有效项目目标的新问题；被拒绝的追问不能换标签入库。
                        fallback_text=(spoken if switch_requested and preferred is not None
                                       and preferred.source == "project"
                                       and progress.state(preferred.id).asked == 0
                                       and not repeated else ""),
                    )
                    if picked is None:
                        self._finish(row, progress, "所有考察目标已完成，场次自动结束。")
                        action = "complete"
                    else:
                        plan, _ = picked
                        created_turn = await self._create_turn(row, progress, plan)
                        saved_question = created_turn.text
                        action = "switch" if plan.objective_id != objective.id else "continue"

                answer_turn.decision_json = TurnDecision(
                    objective_status=decision.objective_status,
                    action=action,
                    observation=decision.observation[:160],
                    reason=decision.reason[:80],
                    followups_used=progress.followups_used,
                ).model_dump(mode="json")
                answer_turn.elapsed_ms = elapsed_ms
                if model is not None:
                    answer_turn.input_tokens = model.usage.input_tokens
                    answer_turn.output_tokens = model.usage.output_tokens
                row.progress_json = progress.model_dump(mode="json")
                row.updated_at = utc_now()
                row.error_code = None
                row.error_message = None
                await self.session.flush()
        except StaleDataError:
            raise conflict() from None
        return saved_question

    # ---------- 控制 ----------

    async def pause(self, session_id: str, payload: SessionControl) -> SessionRead:
        async with self.session.begin():
            row = await self.require_session(session_id, lock=True)
            if row.status == "completed":
                raise InterviewError("session_not_active", "场次已结束", 409)
            if row.revision != payload.expected_revision:
                raise conflict()
            if row.status == "active":
                progress = SessionProgress.model_validate(row.progress_json)
                progress.paused_at = utc_now()
                row.progress_json = progress.model_dump(mode="json")
                row.status = "paused"
                row.updated_at = progress.paused_at
                await self.session.flush()
            return self.read(row, await self.turns(session_id))

    async def resume(self, session_id: str, payload: SessionControl) -> SessionRead:
        pending_opening: str | None = None
        async with self.session.begin():
            row = await self.require_session(session_id, lock=True)
            if row.status == "completed":
                raise InterviewError("session_not_active", "场次已结束", 409)
            if row.revision != payload.expected_revision:
                raise conflict()
            if row.status == "paused":
                progress = SessionProgress.model_validate(row.progress_json)
                now = utc_now()
                close_pause(progress, now, row.updated_at)
                row.progress_json = progress.model_dump(mode="json")
                row.status = "active"
                row.error_code = None
                row.error_message = None
                row.updated_at = now
                await self.session.flush()
            turns = await self.turns(session_id)
            progress = SessionProgress.model_validate(row.progress_json)
            pending_opening = next((turn.id for turn in turns
                                    if turn.role == "candidate" and turn.decision_json is None
                                    and turn.answers_turn_id in progress.opening_questions.values()), None)
            result = self.read(row, turns)
        # 恢复按钮是明确的继续操作；接续被暂停拦下的原介绍，不重存回答或重复推进。
        if pending_opening is not None:
            await self._run_decision(session_id, pending_opening, emit=None)
            return await self.get(session_id)
        if result.phase != "technical" and result.current_turn is None:
            await self._ask_next(session_id)
            return await self.get(session_id)
        return result

    async def skip(self, session_id: str, payload: SessionControl) -> SessionRead:
        async with self.session.begin():
            row = await self.require_session(session_id, lock=True)
            if row.revision != payload.expected_revision:
                raise conflict()
            if row.status != "active":
                raise InterviewError("session_not_active", "只有进行中的场次可以跳过", 409)
            if row.current_turn_id is None:
                raise InterviewError(
                    "no_current_turn", "当前没有待回答的问题，可先点「继续」生成下一问", 409
                )
            turn = await self.session.get(InterviewTurn, row.current_turn_id)
            progress = SessionProgress.model_validate(row.progress_json)
            if turn is not None and turn.id in progress.opening_questions.values():
                progress.phase = "project_introduction" if progress.phase == "self_introduction" else "technical"
            if turn is not None and turn.objective_id:
                try:
                    progress.state(turn.objective_id).status = "skipped"
                except KeyError:
                    pass
            settle_objectives(progress, self.elapsed(row, progress))
            row.current_turn_id = None
            row.progress_json = progress.model_dump(mode="json")
            row.updated_at = utc_now()
            await self.session.flush()
        await self._ask_next(session_id)
        return await self.get(session_id)

    async def end(self, session_id: str, payload: SessionControl) -> SessionRead:
        async with self.session.begin():
            row = await self.require_session(session_id, lock=True)
            if row.status == "completed":
                return self.read(row, await self.turns(session_id))
            if row.revision != payload.expected_revision:
                raise conflict()
            progress = SessionProgress.model_validate(row.progress_json)
            self._finish(row, progress, "用户结束面试。")
            row.progress_json = progress.model_dump(mode="json")
            await self.session.flush()
            return self.read(row, await self.turns(session_id))



    @staticmethod
    def _finish(row: InterviewSession, progress: SessionProgress, reason: str) -> None:
        now = utc_now()
        elapsed = active_seconds(row.started_at, row.ended_at, row.status, row.updated_at, progress, now=now)
        settle_objectives(progress, elapsed)
        if row.status == "paused":
            close_pause(progress, now, row.updated_at)
        progress.completion_reason = reason
        row.status = "completed"
        row.ended_at = now
        row.current_turn_id = None
        row.updated_at = row.ended_at

    async def _mark(self, session_id: str, objective_id: str, status: str) -> None:
        try:
            async with self.session.begin():
                row = await self.require_session(session_id, lock=True)
                progress = SessionProgress.model_validate(row.progress_json)
                try:
                    progress.state(objective_id).status = status
                except KeyError:
                    return
                row.progress_json = progress.model_dump(mode="json")
                row.updated_at = utc_now()
                await self.session.flush()
        except StaleDataError:
            raise conflict() from None

    async def _complete(self, session_id: str, reason: str) -> None:
        try:
            async with self.session.begin():
                row = await self.require_session(session_id, lock=True)
                if row.status == "completed":
                    return
                progress = SessionProgress.model_validate(row.progress_json)
                self._finish(row, progress, reason)
                row.progress_json = progress.model_dump(mode="json")
                await self.session.flush()
        except StaleDataError:
            raise conflict() from None

    async def set_error(self, session_id: str, error: ModelError) -> None:
        try:
            async with self.session.begin():
                row = await self.require_session(session_id, lock=True)
                row.error_code = error.code
                row.error_message = error.message
                row.updated_at = utc_now()
                await self.session.flush()
        except StaleDataError:
            raise conflict() from None

    # ---------- 组合对象薄委托：实现见 questioning.py / reporting.py ----------

    async def get_report(self, session_id: str) -> ReportRead | None:
        return await self.reports.get_report(session_id)

    async def report(
        self, session_id: str, payload: ReportCreate,
    ) -> tuple[ReportRead, bool]:
        return await self.reports.report(session_id, payload)

    async def _ensure_outline(
        self, session_id: str, *,
        objectives: list[PlanRuntimeObjective] | None = None,
        limit: int | None = None,
    ) -> None:
        return await self.questions._ensure_outline(
            session_id, objectives=objectives, limit=limit,
        )

    async def _build_outline(
        self, session_id: str, runtime: PlanRuntime, mode: str,
        targets: list[PlanRuntimeObjective],
    ) -> SessionOutline:
        return await self.questions._build_outline(session_id, runtime, mode, targets)

    def _rule_outline(
        self, session_id: str, runtime: PlanRuntime,
        targets: list[PlanRuntimeObjective],
    ) -> SessionOutline:
        return self.questions._rule_outline(session_id, runtime, targets)

    def _outline_question(
        self, outline: SessionOutline, objective: PlanRuntimeObjective,
        state: ObjectiveProgress,
    ) -> str:
        return self.questions._outline_question(outline, objective, state)

    @staticmethod
    def _objective(
        runtime: PlanRuntime, objective_id: str | None,
    ) -> PlanRuntimeObjective | None:
        return QuestioningService._objective(runtime, objective_id)

    @staticmethod
    def _base_remaining(
        objective: PlanRuntimeObjective, state: ObjectiveProgress,
    ) -> int:
        return QuestioningService._base_remaining(objective, state)

    def _bank_order(
        self, plan_id: str, session_id: str,
        runtime: PlanRuntime, objective: PlanRuntimeObjective,
    ) -> list[SnapshotQuestion]:
        return self.questions._bank_order(plan_id, session_id, runtime, objective)

    async def _question_usage(self, plan_id: str, session_id: str) -> dict[str, int]:
        return await self.questions._question_usage(plan_id, session_id)

    async def _avoid_questions(self, plan_id: str, session_id: str) -> list[str]:
        return await self.questions._avoid_questions(plan_id, session_id)

    @staticmethod
    def _pick_bank(
        order: list[SnapshotQuestion], cursor: int,
    ) -> SnapshotQuestion | None:
        return QuestioningService._pick_bank(order, cursor)

    @staticmethod
    def _objective_facts(
        runtime: PlanRuntime, objective: PlanRuntimeObjective, *, seed: int = 0,
    ) -> list[tuple[str, str, str]]:
        return QuestioningService._objective_facts(runtime, objective, seed=seed)

    def _project_question(
        self, runtime: PlanRuntime, objective: PlanRuntimeObjective,
        state: ObjectiveProgress, *, seed: int = 0,
    ) -> str:
        return self.questions._project_question(runtime, objective, state, seed=seed)

    def _next_pending(
        self, runtime: PlanRuntime, progress: SessionProgress,
        *, session_id: str, preferred: str = "",
    ) -> tuple[PlanRuntimeObjective, ObjectiveProgress] | None:
        return self.questions._next_pending(
            runtime, progress, session_id=session_id, preferred=preferred,
        )

    def _objective_order(
        self, runtime: PlanRuntime, *, session_id: str,
    ) -> list[PlanRuntimeObjective]:
        return self.questions._objective_order(runtime, session_id=session_id)

    async def _next_turn(
        self, runtime: PlanRuntime, progress: SessionProgress, *,
        outline: SessionOutline, plan_id: str, session_id: str,
        preferred: str = "", fallback_text: str = "", exclude: str = "",
    ) -> tuple[NextTurn, ObjectiveProgress] | None:
        return await self.questions._next_turn(
            runtime, progress, outline=outline, plan_id=plan_id,
            session_id=session_id, preferred=preferred, fallback_text=fallback_text,
            exclude=exclude,
        )
