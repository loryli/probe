"""文字面试运行时：冻结计划、先落库回答再决策、状态守卫与断点续接。"""

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from interview_assistant.ai.qwen import ModelError, QwenModel, configuration_status
from interview_assistant.config import Settings
from interview_assistant.database import Database, utc_now
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

logger = logging.getLogger(__name__)

BUDGET_GRACE_SEC = 300
MAX_TURNS_IN_PROMPT = 3
OPTION_PLAN_LIMIT = 50
# 回答短于这个长度视为「没有可追问的内容」（如“不知道”），不追问细节，改问更基础的方面。
MIN_SUBSTANTIVE_ANSWER = 8
REPORT_MAX_TOKENS = 2000
# 决策只输出一小段 JSON，单独限长可以尽早截断跑偏的输出。
DECISION_MAX_TOKENS = 500


class InterviewError(Exception):
    def __init__(self, code: str, message: str, status: int = 422) -> None:
        self.code, self.message, self.status = code, message, status


def conflict() -> InterviewError:
    return InterviewError("revision_conflict", "场次已被修改，请重新读取后再操作", 409)


async def run_decision(
    database: Database, settings: Settings, session_id: str, answer_turn_id: str,
) -> None:
    """后台跑一次决策：用独立会话，异常也写回错误码，避免前端一直等不到结果。"""
    try:
        async with database.sessions() as session:
            await InterviewService(session, settings).decide(session_id, answer_turn_id)
    except Exception:
        logger.exception("Interview decision task failed; session_id=%s", session_id)
        try:
            async with database.sessions() as session:
                await InterviewService(session, settings).set_error(
                    session_id,
                    ModelError("decision_task_failed", "生成下一步失败，请点「重试下一步」"),
                )
        except Exception:
            logger.exception(
                "Interview decision failure not recorded; session_id=%s", session_id,
            )


# ---------- 流式进度 ----------

_LIVE_TTL_SEC = 120
_LIVE: dict[str, "LiveStream"] = {}


@dataclass(slots=True)
class LiveStream:
    """一次决策的实时状态：已产出的文本、是否结束、最终快照或错误。

    客户端可以在任意时刻接入：先读已有的 text，再继续等增量。
    这样客户端断开重连不会丢进度，也不会重复调用模型。
    """

    text: str = ""
    running: bool = False
    done: bool = False
    snapshot: "SessionRead | None" = None
    error: ModelError | None = None

    def write(self, piece: str) -> None:
        self.text += piece


def live_stream(session_id: str, turn_id: str) -> LiveStream:
    key = f"{session_id}:{turn_id}"
    live = _LIVE.get(key)
    if live is None:
        live = LiveStream()
        _LIVE[key] = live
        try:
            loop = asyncio.get_running_loop()
            loop.call_later(_LIVE_TTL_SEC, _LIVE.pop, key, None)
        except RuntimeError:
            pass
    return live


async def run_decision_stream(
    database: Database, settings: Settings, session_id: str, answer_turn_id: str,
) -> None:
    """后台流式决策：文本写进 LiveStream 供 SSE 转发，结束时补齐最终快照。"""
    live = live_stream(session_id, answer_turn_id)
    if live.running or live.done:
        return
    live.running = True
    try:
        async with database.sessions() as session:
            await InterviewService(session, settings).decide_stream(
                session_id, answer_turn_id, emit=live.write,
            )
    except Exception:
        logger.exception("Interview stream decision failed; session_id=%s", session_id)
        try:
            async with database.sessions() as session:
                await InterviewService(session, settings).set_error(
                    session_id,
                    ModelError("decision_task_failed", "生成下一步失败，请点「重试下一步」"),
                )
        except Exception:
            logger.exception(
                "Interview decision failure not recorded; session_id=%s", session_id,
            )
    finally:
        live.running = False
        try:
            async with database.sessions() as session:
                live.snapshot = await InterviewService(session, settings).get(session_id)
        except Exception:
            logger.exception("Interview stream snapshot failed; session_id=%s", session_id)
        # 快照就绪后才置为结束，避免订阅方在 snapshot 为空时提前收尾。
        live.done = True


@dataclass(slots=True)
class NextTurn:
    """一次待写入的问题轮次，附带该轮对进度的变更。"""

    objective_id: str
    source: str
    text: str
    question_id: str | None = None
    is_followup: bool = False
    activate: bool = False
    advance_cursor: bool = False


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


_SAY_PATTERN = re.compile(r'"say"\s*:\s*"')


class SayExtractor:
    """从流式 JSON 里增量抽出 "say" 的字符串值。

    整包 JSON 只在本轮结束时才完整，但面试官要说的话已经在流里了；
    这里按字符解码（含转义），让前端可以逐字显示而不是等整包。
    """

    __slots__ = ("_buffer", "_started", "_closed", "_escape")

    def __init__(self) -> None:
        self._buffer = ""
        self._started = False
        self._closed = False
        self._escape = False

    def feed(self, chunk: str) -> str:
        if self._closed or not chunk:
            return ""
        if not self._started:
            self._buffer += chunk
            match = _SAY_PATTERN.search(self._buffer)
            if match is None:
                # 只留尾部可能的半个键名，避免未命中时缓冲无限增长。
                self._buffer = self._buffer[-16:]
                return ""
            self._started = True
            self._buffer = self._buffer[match.end():]
        else:
            self._buffer += chunk
        out: list[str] = []
        index = 0
        size = len(self._buffer)
        while index < size:
            char = self._buffer[index]
            if self._escape:
                if char == "u":
                    if size - index < 5:
                        break
                    try:
                        out.append(chr(int(self._buffer[index + 1:index + 5], 16)))
                    except ValueError:
                        pass
                    index += 5
                    self._escape = False
                    continue
                out.append({"n": " ", "t": " ", "r": " "}.get(char, char))
                self._escape = False
                index += 1
                continue
            if char == "\\":
                self._escape = True
                index += 1
                continue
            if char == '"':
                self._closed = True
                index += 1
                break
            out.append(char)
            index += 1
        self._buffer = self._buffer[index:]
        return "".join(out)


class InterviewService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        # 一次请求内复用：顺序与历史用量都可重算，缓存只为少查几次。
        self._bank_orders: dict[tuple[str, str], list[SnapshotQuestion]] = {}
        self._objective_orders: dict[str, list[PlanRuntimeObjective]] = {}
        self._usage: dict[tuple[str, str], dict[str, int]] = {}
        self._avoid: dict[str, list[str]] = {}

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

    def read(self, row: InterviewSession, turns: list[InterviewTurn]) -> SessionRead:
        runtime = PlanRuntime.model_validate(row.runtime_json)
        progress = SessionProgress.model_validate(row.progress_json)
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
            )
            for objective in runtime.objectives
        ]
        answered = {turn.answers_turn_id for turn in turns if turn.answers_turn_id}
        reads = [
            TurnRead(
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
        end = row.ended_at or utc_now()
        elapsed = max(0, int((end - row.started_at).total_seconds()))
        return SessionRead(
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
                    end = row.ended_at or utc_now()
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
                        elapsed_sec=max(0, int((end - row.started_at).total_seconds())),
                        created_at=row.created_at,
                        started_at=row.started_at,
                        ended_at=row.ended_at,
                    ))
                return SessionList(items=items, total=total, limit=limit, offset=offset)
        except StaleDataError:
            raise conflict() from None

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

        # 只给第一个待考察目标预生成题目：开场等待与单轮出题同量级。
        # 骨架只是加速手段，任何失败都不该让「开始面试」整体失败。
        try:
            await self._ensure_outline(session_id, limit=1)
        except Exception:
            logger.exception("Interview outline failed; session_id=%s", session_id)
        await self._ask_next(session_id)
        return await self.get(session_id), True

    async def _ensure_outline(
        self, session_id: str, *,
        objectives: list[PlanRuntimeObjective] | None = None,
        limit: int | None = None,
    ) -> None:
        """按目标增量生成基础问题骨架；失败只降级，不阻断创建或出题。

        创建场次时只为前 `limit` 个目标生成，其余目标在真正轮到它时再补，
        这样「开始面试」的等待与单轮出题同量级，而不是一次批量生成整场题单。
        """
        async with self.session.begin():
            row = await self.require_session(session_id)
            runtime = PlanRuntime.model_validate(row.runtime_json)
            mode = row.mode
            current = SessionOutline.model_validate(row.outline_json or {})
        # 顺序必须与 _ask_next 取目标的顺序一致，否则预生成的不是第一题，
        # 开场会白跑一次模型调用再补一次。
        targets = [
            item for item in self._objective_order(runtime, session_id=session_id)
            if item.source == "project"
        ]
        if objectives is not None:
            wanted = {item.id for item in objectives}
            targets = [item for item in targets if item.id in wanted]
        covered = {item.objective_id for item in current.objectives}
        targets = [item for item in targets if item.id not in covered]
        if limit is not None:
            targets = targets[:limit]
        if not targets:
            return
        extra = await self._build_outline(session_id, runtime, mode, targets)
        if not extra.objectives:
            return
        async with self.session.begin():
            row = await self.require_session(session_id, lock=True)
            existing = SessionOutline.model_validate(row.outline_json or {})
            covered = {item.objective_id for item in existing.objectives}
            added = [item for item in extra.objectives if item.objective_id not in covered]
            if not added:
                return
            source = "model" if "model" in {existing.source, extra.source} else extra.source
            row.outline_json = SessionOutline(
                source=source, objectives=existing.objectives + added,
            ).model_dump(mode="json")
            await self.session.flush()

    async def _build_outline(
        self, session_id: str, runtime: PlanRuntime, mode: str,
        targets: list[PlanRuntimeObjective],
    ) -> SessionOutline:
        if mode == "rule" or not targets:
            return self._rule_outline(session_id, runtime, targets)
        if not configuration_status(self.settings)["configured"]:
            logger.info("Interview outline skipped; model not configured session_id=%s", session_id)
            return SessionOutline(source="fallback")
        model = QwenModel(self.settings)
        try:
            draft = await model.generate(
                messages=outline_messages(runtime=runtime, objectives=targets),
                output_type=OutlineDraft,
                prompt_version=OUTLINE_PROMPT_VERSION,
                schema_version=OUTLINE_SCHEMA_VERSION,
                temperature=self.settings.qwen_interview_temperature,
            )
        except ModelError as failure:
            # 骨架只是加速手段：生成失败时仍然可以用即时出题走完整场次。
            logger.warning(
                "Interview outline failed; session_id=%s code=%s", session_id, failure.code,
            )
            return SessionOutline(source="fallback")
        assert isinstance(draft, OutlineDraft)
        known = {item.id for item in targets}
        objectives = [item for item in draft.objectives if item.objective_id in known]
        # 模型漏掉的目标用规则骨架补上：骨架必须「必命中」，
        # 否则开场白跑一次模型调用再为同一个目标补一次即时出题。
        covered = {item.objective_id for item in objectives}
        missing = [item for item in targets if item.id not in covered]
        if missing:
            objectives.extend(self._rule_outline(session_id, runtime, missing).objectives)
        if not objectives:
            return SessionOutline(source="fallback")
        return SessionOutline(source="model", objectives=objectives)

    def _rule_outline(
        self, session_id: str, runtime: PlanRuntime, targets: list[PlanRuntimeObjective],
    ) -> SessionOutline:
        """规则模式用同一套事实模板生成骨架，保证两种模式的取题路径一致。"""
        seed = variety.session_seed(session_id)
        objectives: list[OutlineObjective] = []
        for objective in targets:
            facts = self._objective_facts(runtime, objective, seed=seed)
            questions = [
                OutlineQuestion(
                    text=rules.project_question(name, fact_type, content)[:300],
                )
                for name, fact_type, content in facts[:PROJECT_BASE_QUESTIONS]
            ]
            if questions:
                objectives.append(OutlineObjective(
                    objective_id=objective.id, questions=questions,
                ))
        return SessionOutline(source="rule", objectives=objectives)

    def _outline_question(
        self, outline: SessionOutline, objective: PlanRuntimeObjective, state: ObjectiveProgress,
    ) -> str:
        """按「已问过的基础问题数」取骨架题；没有就返回空串，由调用方回退。"""
        items = outline.questions(objective.id)
        index = state.asked - state.followups
        if 0 <= index < len(items):
            return items[index].text[:400]
        return ""

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
        for _ in range(16):
            async with self.session.begin():
                row = await self.require_session(session_id)
                if row.status != "active" or row.current_turn_id is not None:
                    return
                runtime = PlanRuntime.model_validate(row.runtime_json)
                progress = SessionProgress.model_validate(row.progress_json)
                elapsed = max(0, int((utc_now() - row.started_at).total_seconds()))
                mode = row.mode
                plan_id = row.plan_id
                outline = SessionOutline.model_validate(row.outline_json or {})
                # 事务内先取历史用量与近期问题，块外的取题与出题只用缓存。
                await self._question_usage(plan_id, session_id)
                await self._avoid_questions(plan_id, session_id)
                picked = self._next_pending(runtime, progress, session_id=session_id)
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
                    text=plain_text(snapshot.question_text)[:400],
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
        try:
            draft = await model.generate(
                messages=question_messages(
                    runtime=runtime, objective=objective, elapsed_sec=elapsed,
                    avoid=self._avoid.get(session_id, []),
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
            text=plan.text[:2000],
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
        """流式决策：判定字段一到就先拿 say，逐段交给 emit 展示。"""
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
            elapsed = max(0, int((utc_now() - row.started_at).total_seconds()))
            remaining = runtime.duration_target_sec - elapsed
            followups_remaining = max(0, min(
                objective.max_followups - state.followups,
                runtime.constraints.max_total_followups - progress.followups_used,
            ))
            follow_up_allowed = followups_remaining > 0 and remaining > -60
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
            if emit is not None and decision.spoken:
                emit(decision.spoken)
        else:
            if not configuration_status(self.settings)["configured"]:
                await self.set_error(session_id, ModelError(
                    "model_not_configured",
                    "请先配置 Qwen 密钥与模型；也可用规则模式重开一场",
                ))
                return
            model = QwenModel(self.settings)
            extractor = SayExtractor()
            started = time.monotonic()
            try:
                async for chunk in model.generate_stream(
                    messages=context.messages,  # type: ignore[arg-type]
                    output_type=DecisionDraft,
                    prompt_version=PROMPT_VERSION,
                    schema_version=DECISION_SCHEMA_VERSION,
                    temperature=self.settings.qwen_interview_temperature,
                    max_tokens=DECISION_MAX_TOKENS,
                ):
                    if emit is None:
                        continue
                    piece = extractor.feed(chunk)
                    if piece:
                        emit(piece)
            except ModelError as failure:
                # 已经流出去的文字无法撤回；这里只记录错误，前端据 error_code 提示重试。
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
        await self._apply(session_id, answer_turn_id, decision, elapsed_ms, model)

    async def _apply(
        self,
        session_id: str,
        answer_turn_id: str,
        decision: DecisionDraft,
        elapsed_ms: int,
        model: QwenModel | None,
    ) -> None:
        """写入决策与下一问前重新加锁校验：暂停、结束或已推进时丢弃模型结果。"""
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
                elapsed = max(0, int((utc_now() - row.started_at).total_seconds()))
                remaining = runtime.duration_target_sec - elapsed
                followups_remaining = max(0, min(
                    objective.max_followups - state.followups,
                    runtime.constraints.max_total_followups - progress.followups_used,
                ))
                follow_up_allowed = followups_remaining > 0 and remaining > -60
                base_remaining = self._base_remaining(objective, state)
                # 题库取题顺序依赖历史用量；在本事务内取一次即可。
                await self._question_usage(row.plan_id, session_id)

                next_turn: NextTurn | None = None
                action = "switch"
                if remaining < -BUDGET_GRACE_SEC:
                    # 软预算只到这一步：本轮回答已处理完，超过宽限就自然收尾。
                    self._finish(row, progress, "已超过计划时长，场次自然收尾。")
                    action = "complete"
                else:
                    # 回答几乎没有内容（如“不知道”）时不再追问细节，换更基础的问法或直接切题。
                    answer_text = answer_turn.text or ""
                    substantive = len(answer_text.strip()) >= MIN_SUBSTANTIVE_ANSWER
                    # say 已经流式展示给用户，落库必须用同一句，否则页面会跳变。
                    spoken = decision.spoken
                    if decision.objective_status == "covered":
                        state.status = "covered"
                    elif (follow_up_allowed and substantive and spoken
                          and (decision.follow_up_needed
                               or decision.objective_status == "insufficient")):
                        next_turn = NextTurn(
                            objective.id, objective.source,
                            spoken[:300], is_followup=True,
                        )
                        action = "follow_up"
                    elif base_remaining > 0 and decision.next_objective_id == objective.id:
                        # 同目标续问：模型刚说的话优先，骨架只作兜底。
                        text = spoken or self._outline_question(outline, objective, state)
                        if text:
                            next_turn = NextTurn(objective.id, objective.source, text[:400])
                            action = "continue"
                    elif base_remaining > 0 and objective.source == "question_bank":
                        snapshot = self._pick_bank(
                            self._bank_order(row.plan_id, session_id, runtime, objective),
                            state.cursor,
                        )
                        if snapshot is not None:
                            next_turn = NextTurn(
                                objective.id, "question_bank",
                                plain_text(snapshot.question_text)[:400],
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
                        # 追问与同目标续问在本轮决策里已给出措辞，必须落库，
                        # 否则场次会停在「当前没有待回答的问题」。
                        await self._create_turn(row, progress, next_turn)
                    else:
                        picked = await self._next_turn(
                            runtime, progress, outline=outline,
                            plan_id=row.plan_id, session_id=session_id,
                            preferred=decision.next_objective_id,
                            fallback_text=spoken,
                        )
                        if picked is None:
                            self._finish(row, progress, "所有考察目标已完成，场次自动结束。")
                            action = "complete"
                        else:
                            plan, _ = picked
                            await self._create_turn(row, progress, plan)
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

    # ---------- 控制 ----------

    async def pause(self, session_id: str, payload: SessionControl) -> SessionRead:
        async with self.session.begin():
            row = await self.require_session(session_id, lock=True)
            if row.status == "completed":
                raise InterviewError("session_not_active", "场次已结束", 409)
            if row.revision != payload.expected_revision:
                raise conflict()
            if row.status == "active":
                row.status = "paused"
                row.updated_at = utc_now()
                await self.session.flush()
            return self.read(row, await self.turns(session_id))

    async def resume(self, session_id: str, payload: SessionControl) -> SessionRead:
        async with self.session.begin():
            row = await self.require_session(session_id, lock=True)
            if row.status == "completed":
                raise InterviewError("session_not_active", "场次已结束", 409)
            if row.revision != payload.expected_revision:
                raise conflict()
            if row.status == "paused":
                row.status = "active"
                row.error_code = None
                row.error_message = None
                row.updated_at = utc_now()
                await self.session.flush()
            return self.read(row, await self.turns(session_id))

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
            if turn is not None and turn.objective_id:
                try:
                    progress.state(turn.objective_id).status = "skipped"
                except KeyError:
                    pass
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
            progress.completion_reason = "用户结束面试。"
            row.progress_json = progress.model_dump(mode="json")
            row.status = "completed"
            row.ended_at = utc_now()
            row.current_turn_id = None
            row.updated_at = row.ended_at
            await self.session.flush()
            return self.read(row, await self.turns(session_id))

    # ---------- 整场复盘 ----------

    async def get_report(self, session_id: str) -> ReportRead | None:
        async with self.session.begin():
            row = await self.session.scalar(
                select(InterviewReport).where(InterviewReport.session_id == session_id)
            )
            return self._report_read(row) if row is not None else None

    async def report(
        self, session_id: str, payload: ReportCreate,
    ) -> tuple[ReportRead, bool]:
        """为已结束的场次生成整场复盘；每场一份，失败行可被重试覆盖。"""
        request_key = str(payload.request_id)
        async with self.session.begin():
            row = await self.require_session(session_id)
            if row.status != "completed":
                raise InterviewError(
                    "session_not_completed",
                    "只有已结束的场次才能生成复盘；请先结束面试",
                    409,
                )
            existing = await self.session.scalar(
                select(InterviewReport).where(InterviewReport.session_id == session_id)
            )
            # 重放同一请求标识或已有成品：直接返回，不重复调用模型。
            if existing is not None and (
                existing.request_key == request_key or existing.status == "ready"
            ):
                return self._report_read(existing), False
            runtime = PlanRuntime.model_validate(row.runtime_json)
            progress = SessionProgress.model_validate(row.progress_json)
            turns = await self.turns(session_id)
            ended = row.ended_at or utc_now()
            elapsed_sec = max(0, int((ended - row.started_at).total_seconds()))
            rounds = self._report_rounds(turns)
            stats = self._report_stats(progress, turns, elapsed_sec)
            mode = row.mode

        if mode != "rule" and configuration_status(self.settings)["configured"]:
            content, error = await self._model_report(runtime, rounds, elapsed_sec)
        else:
            content, error = self._rule_report(runtime, progress, turns), None

        async with self.session.begin():
            row = await self.session.scalar(
                select(InterviewReport)
                .where(InterviewReport.session_id == session_id)
                .with_for_update()
            )
            if row is None:
                row = InterviewReport(
                    session_id=session_id, status="ready", source="model",
                    stats_json=stats, request_key=request_key,
                )
                self.session.add(row)
            row.request_key = request_key
            row.stats_json = stats
            row.updated_at = utc_now()
            if error is None:
                row.status = "ready"
                row.source = "model" if mode != "rule" else "rule"
                row.report_json = content.model_dump(mode="json")
                row.error_code = None
                row.error_message = None
            else:
                # Offline 失败保留状态供页面提示与重试；不影响已保存的场次数据。
                row.status = "failed"
                row.report_json = None
                row.error_code = error.code
                row.error_message = error.message[:500]
            await self.session.flush()
            return self._report_read(row), True

    async def _model_report(
        self, runtime: PlanRuntime, rounds: list[dict[str, object]], elapsed_sec: int,
    ) -> tuple[ReportContent | None, ModelError | None]:
        """调用模型生成复盘；失败返回错误而不是抛出，让调用方落库 failed 行。"""
        model = QwenModel(self.settings)
        try:
            draft = await model.generate(
                messages=report_messages(
                    runtime=runtime, rounds=rounds, elapsed_sec=elapsed_sec,
                ),
                output_type=ReportDraft,
                prompt_version=REPORT_PROMPT_VERSION,
                schema_version=REPORT_SCHEMA_VERSION,
                temperature=self.settings.qwen_interview_temperature,
                max_tokens=REPORT_MAX_TOKENS,
            )
        except ModelError as failure:
            return None, failure
        if not isinstance(draft, ReportDraft):
            return None, ModelError(
                "model_invalid_output", "模型结果未通过校验，复盘未保存",
            )
        return self._sanitize_report(draft, runtime), None

    def _rule_report(
        self, runtime: PlanRuntime, progress: SessionProgress, turns: list[InterviewTurn],
    ) -> ReportContent:
        """规则复盘只做统计与证据索引，不做语义评价。"""
        answers = [item for item in turns if item.role == "candidate"]
        by_objective: dict[str, list[int]] = {}
        for item in answers:
            if item.objective_id:
                by_objective.setdefault(item.objective_id, []).append(item.sequence)
        status_map = {"covered": "covered", "skipped": "missed", "active": "partial"}
        evaluations: list[ReportEvaluation] = []
        for objective in runtime.objectives:
            state = progress.state(objective.id)
            status = status_map.get(state.status, "missed")
            sequences = by_objective.get(objective.id, [])
            evaluations.append(ReportEvaluation(
                objective_id=objective.id,
                status=status,
                comment=(
                    f"规则模式统计：问 {state.asked} 轮，追问 {state.followups} 次，"
                    f"最终状态 {state.status}；语义评价需用模型模式生成。"
                ),
                turn_sequences=sequences[:4],
            ))
        covered = sum(1 for item in evaluations if item.status == "covered")
        overall = "solid" if covered >= max(1, len(evaluations)) else (
            "mixed" if covered else "weak"
        )
        return ReportContent(
            overall=overall,
            summary=(
                f"规则模式复盘：共 {len(answers)} 轮回答，"
                f"覆盖 {covered}/{len(evaluations)} 个目标。"
                "要获得逐目标评价、弱项与练习建议，请用模型模式生成复盘。"
            ),
            evaluations=evaluations,
            weak_topics=[
                item.topic for item in runtime.objectives
                if progress.state(item.id).status not in {"covered", "skipped"}
            ][:5],
            suggestions=["用 Qwen 面试官模式重新生成复盘，获取逐目标评价与练习建议。"],
        )

    def _sanitize_report(
        self, draft: ReportDraft, runtime: PlanRuntime,
    ) -> ReportContent:
        """只保留已知目标；模型漏掉或编造的目标 ID 不进入报告。"""
        known = {item.id for item in runtime.objectives}
        order = {item.id: index for index, item in enumerate(runtime.objectives)}
        evaluations = [
            item for item in draft.evaluations if item.objective_id in known
        ]
        evaluations.sort(key=lambda item: order.get(item.objective_id, 0))
        return ReportContent(
            overall=draft.overall,
            summary=draft.summary,
            evaluations=evaluations,
            weak_topics=draft.weak_topics,
            suggestions=draft.suggestions,
            risks=draft.risks,
        )

    @staticmethod
    def _report_rounds(turns: list[InterviewTurn]) -> list[dict[str, object]]:
        """问答成对压缩：每轮给模型问题、回答、目标与当时的判定。"""
        question_by_id = {
            item.id: item for item in turns if item.role == "interviewer"
        }
        rounds: list[dict[str, object]] = []
        for item in turns:
            if item.role != "candidate" or item.answers_turn_id is None:
                continue
            question = question_by_id.get(item.answers_turn_id)
            decision = item.decision_json or {}
            rounds.append({
                "sequence": item.sequence,
                "objective_id": item.objective_id,
                "is_followup": bool(question is not None and question.is_followup),
                "question": plain_text(question.text)[:200] if question else "",
                "answer": plain_text(item.text)[:600],
                "decision": {
                    "objective_status": decision.get("objective_status", ""),
                    "observation": (decision.get("observation") or "")[:120],
                },
            })
        return rounds

    @staticmethod
    def _report_stats(
        progress: SessionProgress, turns: list[InterviewTurn], elapsed_sec: int,
    ) -> dict[str, int]:
        answers = [item for item in turns if item.role == "candidate"]
        questions = [item for item in turns if item.role == "interviewer"]
        return {
            "answer_count": len(answers),
            "question_count": len(questions),
            "followups_used": sum(
                1 for item in questions if item.is_followup
            ),
            "objective_total": len(progress.objectives),
            "objective_covered": sum(
                1 for item in progress.objectives if item.status == "covered"
            ),
            "elapsed_sec": elapsed_sec,
            "input_tokens": sum(item.input_tokens or 0 for item in turns),
            "output_tokens": sum(item.output_tokens or 0 for item in turns),
        }

    @staticmethod
    def _report_read(row: InterviewReport) -> ReportRead:
        report = (
            ReportContent.model_validate(row.report_json)
            if row.report_json is not None else None
        )
        return ReportRead(
            id=UUID(row.id),
            session_id=UUID(row.session_id),
            status=row.status,  # type: ignore[arg-type]
            source=row.source,  # type: ignore[arg-type]
            report=report,
            stats={key: int(value) for key, value in (row.stats_json or {}).items()},
            error_code=row.error_code,
            error_message=row.error_message,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    # ---------- 组装辅助 ----------

    @staticmethod
    def _objective(
        runtime: PlanRuntime, objective_id: str | None,
    ) -> PlanRuntimeObjective | None:
        if objective_id is None:
            return None
        for item in runtime.objectives:
            if item.id == objective_id:
                return item
        return None

    @staticmethod
    def _base_remaining(objective: PlanRuntimeObjective, state: ObjectiveProgress) -> int:
        if objective.source == "question_bank":
            return max(0, len(objective.question_ids) - state.cursor)
        return max(0, PROJECT_BASE_QUESTIONS - (state.asked - state.followups))

    def _bank_order(
        self, plan_id: str, session_id: str,
        runtime: PlanRuntime, objective: PlanRuntimeObjective,
    ) -> list[SnapshotQuestion]:
        """题库取题顺序：同场次固定（cursor 依赖它）、场次间不同、历史问过的靠后。"""
        cached = self._bank_orders.get((session_id, objective.id))
        if cached is not None:
            return cached
        index = {str(item.id): item for item in runtime.materials.questions}
        items = [
            index[str(item)] for item in objective.question_ids if str(item) in index
        ]
        ordered = variety.shuffled(
            items, seed=variety.session_seed(session_id), key=lambda item: str(item.id),
        )
        ordered = variety.prefer_unseen(
            ordered,
            usage=self._usage.get((plan_id, session_id), {}),
            key=lambda item: str(item.id),
        )
        self._bank_orders[(session_id, objective.id)] = ordered
        return ordered

    async def _question_usage(self, plan_id: str, session_id: str) -> dict[str, int]:
        """同计划其他场次每题被问过几次；用于把没问过的题排到前面。"""
        cached = self._usage.get((plan_id, session_id))
        if cached is not None:
            return cached
        rows = await self.session.execute(
            select(InterviewTurn.question_id, func.count())
            .join(InterviewSession, InterviewTurn.session_id == InterviewSession.id)
            .where(
                InterviewSession.plan_id == plan_id,
                InterviewSession.id != session_id,
                InterviewTurn.role == "interviewer",
                InterviewTurn.question_id.is_not(None),
            )
            .group_by(InterviewTurn.question_id)
        )
        usage = {str(qid): int(count) for qid, count in rows if qid is not None}
        self._usage[(plan_id, session_id)] = usage
        return usage

    async def _avoid_questions(self, plan_id: str, session_id: str) -> list[str]:
        """同计划历史场次最近问过的问题，作为模型「别重复」的参考。"""
        cached = self._avoid.get(session_id)
        if cached is not None:
            return cached
        rows = await self.session.execute(
            select(InterviewTurn.text)
            .join(InterviewSession, InterviewTurn.session_id == InterviewSession.id)
            .where(
                InterviewSession.plan_id == plan_id,
                InterviewSession.id != session_id,
                InterviewTurn.role == "interviewer",
            )
            .order_by(InterviewTurn.created_at.desc(), InterviewTurn.sequence.desc())
            .limit(6)
        )
        seen: list[str] = []
        for text in rows.scalars():
            snippet = plain_text(text or "")[:120]
            if snippet and snippet not in seen:
                seen.append(snippet)
        self._avoid[session_id] = seen
        return seen

    @staticmethod
    def _pick_bank(
        order: list[SnapshotQuestion], cursor: int,
    ) -> SnapshotQuestion | None:
        return order[cursor] if 0 <= cursor < len(order) else None

    @staticmethod
    def _objective_facts(
        runtime: PlanRuntime, objective: PlanRuntimeObjective, *, seed: int = 0,
    ) -> list[tuple[str, str, str]]:
        wanted = {str(item) for item in objective.project_fact_ids}
        result: list[tuple[str, str, str]] = []
        for project in runtime.materials.projects:
            for fact in project.facts:
                if str(fact.id) in wanted:
                    result.append((project.name, fact.fact_type, fact.content))
        if seed:
            # 事实顺序也按场次打乱，避免每次从同一条经历问起。
            result = variety.shuffled(
                result, seed=seed,
                key=lambda item: "\x1f".join(item),
            )
        return result

    def _project_question(
        self, runtime: PlanRuntime, objective: PlanRuntimeObjective,
        state: ObjectiveProgress, *, seed: int = 0,
    ) -> str:
        facts = self._objective_facts(runtime, objective, seed=seed)
        if not facts:
            return rules.objective_question(objective.topic)
        index = (state.asked - state.followups) % len(facts)
        project_name, fact_type, content = facts[index]
        return rules.project_question(project_name, fact_type, content)

    def _next_pending(
        self, runtime: PlanRuntime, progress: SessionProgress,
        *, session_id: str, preferred: str = "",
    ) -> tuple[PlanRuntimeObjective, ObjectiveProgress] | None:
        if preferred:
            objective = self._objective(runtime, preferred)
            if objective is not None:
                state = progress.state(objective.id)
                if state.status in {"pending", "active"}:
                    return objective, state
        for objective in self._objective_order(runtime, session_id=session_id):
            state = progress.state(objective.id)
            if state.status in {"pending", "active"}:
                return objective, state
        return None

    def _objective_order(
        self, runtime: PlanRuntime, *, session_id: str,
    ) -> list[PlanRuntimeObjective]:
        """优先级分层不变、同层按场次打乱：每场从不同的目标切入。"""
        cached = self._objective_orders.get(session_id)
        if cached is None:
            cached = variety.grouped_by_priority(
                runtime.objectives,
                seed=variety.session_seed(session_id),
                key=lambda item: item.id,
                priority=lambda item: item.priority,
            )
            self._objective_orders[session_id] = cached
        return cached

    async def _next_turn(
        self,
        runtime: PlanRuntime,
        progress: SessionProgress,
        *,
        outline: SessionOutline,
        plan_id: str,
        session_id: str,
        preferred: str = "",
        fallback_text: str = "",
    ) -> tuple[NextTurn, ObjectiveProgress] | None:
        """先试模型建议的目标，再按场次顺序推进；模型刚说的话优先于骨架。"""
        chosen = self._next_pending(
            runtime, progress, session_id=session_id, preferred=preferred,
        )
        first = True
        while chosen is not None:
            objective, state = chosen
            if objective.source == "question_bank":
                snapshot = self._pick_bank(
                    self._bank_order(plan_id, session_id, runtime, objective), state.cursor,
                )
                if snapshot is None:
                    state.status = "skipped"
                    chosen = self._next_pending(runtime, progress, session_id=session_id)
                    first = False
                    continue
                return NextTurn(
                    objective_id=objective.id, source="question_bank",
                    text=plain_text(snapshot.question_text)[:400],
                    question_id=str(snapshot.id), activate=True, advance_cursor=True,
                ), state
            # 模型本轮已经说出口的话必须原样落库，否则用户会看到文字被替换。
            text = fallback_text if first and (not preferred or preferred == objective.id) else ""
            if not text:
                text = self._outline_question(outline, objective, state)
            if not text:
                text = self._project_question(
                    runtime, objective, state, seed=variety.session_seed(session_id),
                )
            return NextTurn(
                objective_id=objective.id, source="project", text=text, activate=True,
            ), state
        return None

    @staticmethod
    def _finish(row: InterviewSession, progress: SessionProgress, reason: str) -> None:
        progress.completion_reason = reason
        row.status = "completed"
        row.ended_at = utc_now()
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
