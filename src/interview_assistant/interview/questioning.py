"""基础问题骨架、目标顺序与题库取题；与主服务共享同一个数据库会话。

这里的方法不自行开启跨服务事务：事务仍由调用方持有，需要写库的方法
沿用原来那一层 async with session.begin()。
"""

import asyncio
import logging
import re
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
from typing import TYPE_CHECKING

from interview_assistant.interview.errors import InterviewError
from interview_assistant.interview.timing import active_seconds, source_seconds

if TYPE_CHECKING:
    from interview_assistant.interview.service import InterviewService

logger = logging.getLogger(__name__)

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

class QuestioningService:
    def __init__(self, owner: "InterviewService") -> None:
        self.owner = owner
        # 一次请求内复用：顺序与历史用量都可重算，缓存只为少查几次。
        self._bank_orders: dict[tuple[str, str], list[SnapshotQuestion]] = {}
        self._objective_orders: dict[str, list[PlanRuntimeObjective]] = {}
        self._usage: dict[tuple[str, str], dict[str, int]] = {}
        self._avoid: dict[str, list[str]] = {}

    async def _ensure_outline(
        self, session_id: str, *,
        objectives: list[PlanRuntimeObjective] | None = None,
        limit: int | None = None,
    ) -> None:
        """按目标增量生成基础问题骨架；失败只降级，不阻断创建或出题。

        创建场次时只为前 `limit` 个目标生成，其余目标在真正轮到它时再补，
        这样「开始面试」的等待与单轮出题同量级，而不是一次批量生成整场题单。
        """
        async with self.owner.session.begin():
            row = await self.owner.require_session(session_id)
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
        async with self.owner.session.begin():
            row = await self.owner.require_session(session_id, lock=True)
            existing = SessionOutline.model_validate(row.outline_json or {})
            covered = {item.objective_id for item in existing.objectives}
            added = [item for item in extra.objectives if item.objective_id not in covered]
            if not added:
                return
            source = "model" if "model" in {existing.source, extra.source} else extra.source
            row.outline_json = SessionOutline(
                source=source, objectives=existing.objectives + added,
            ).model_dump(mode="json")
            await self.owner.session.flush()

    async def _build_outline(
        self, session_id: str, runtime: PlanRuntime, mode: str,
        targets: list[PlanRuntimeObjective],
    ) -> SessionOutline:
        if mode == "rule" or not targets:
            return self._rule_outline(session_id, runtime, targets)
        if not configuration_status(self.owner.settings)["configured"]:
            logger.info("Interview outline skipped; model not configured session_id=%s", session_id)
            return SessionOutline(source="fallback")
        model = QwenModel(self.owner.settings)
        async with self.owner.session.begin():
            row = await self.owner.require_session(session_id)
            turns = await self.owner.turns(session_id)
            introduction = self.owner.opening.context(
                SessionProgress.model_validate(row.progress_json), turns,
            )
        try:
            draft = await model.generate(
                messages=outline_messages(runtime=runtime, objectives=targets, introduction=introduction),
                output_type=OutlineDraft,
                prompt_version=OUTLINE_PROMPT_VERSION,
                schema_version=OUTLINE_SCHEMA_VERSION,
                temperature=self.owner.settings.qwen_interview_temperature,
            )
        except ModelError as failure:
            # 骨架只是加速手段：生成失败时仍然可以用即时出题走完整场次。
            logger.warning(
                "Interview outline failed; session_id=%s code=%s", session_id, failure.code,
            )
            return SessionOutline(source="fallback")
        assert isinstance(draft, OutlineDraft)
        known = {item.id for item in targets}
        seen = {"".join(plain_text(item.text).split()).casefold()
                for item in turns if item.role == "interviewer"}
        objectives = []
        for item in draft.objectives:
            if item.objective_id not in known:
                continue
            questions = []
            for question in item.questions:
                key = "".join(plain_text(question.text).split()).casefold()
                if key and key not in seen:
                    seen.add(key)
                    questions.append(question)
            if questions:
                objectives.append(item.model_copy(update={"questions": questions}))
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
                    text=rules.project_question(name, fact_type, content, topic=objective.topic, angle=index)[:300],
                )
                for index, (name, fact_type, content) in enumerate(facts[:PROJECT_BASE_QUESTIONS])
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
        rows = await self.owner.session.execute(
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
        rows = await self.owner.session.execute(
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
        base_index = state.asked - state.followups
        index = base_index % len(facts)
        project_name, fact_type, content = facts[index]
        return rules.project_question(project_name, fact_type, content, topic=objective.topic, angle=base_index)

    def _next_pending(
        self, runtime: PlanRuntime, progress: SessionProgress,
        *, session_id: str, preferred: str = "", exclude: str = "",
    ) -> tuple[PlanRuntimeObjective, ObjectiveProgress] | None:
        # 被预算调度暂存的目标可能已耗尽基础题；不能重新激活后无限回退出题。
        # 追问在当前回答的决策中处理，这里只负责选择下一道基础题。
        for item in runtime.objectives:
            state = progress.state(item.id)
            if state.status in {"pending", "active"} and self._base_remaining(item, state) == 0:
                state.status = "covered"
        candidates = [
            item for item in self._objective_order(runtime, session_id=session_id)
            if progress.state(item.id).status in {"pending", "active"}
        ]
        if exclude and len(candidates) > 1:
            # 预算让位等场景排除当前目标；仅剩它时照常返回，保证题被问完。
            candidates = [item for item in candidates if item.id != exclude]
        if not candidates:
            return None
        elapsed = active_seconds(progress.started_at, None, "active", progress.started_at, progress)
        spent = source_seconds(runtime, progress, elapsed)

        def consumed_share(item: PlanRuntimeObjective) -> float:
            return spent[item.source] / max(1, runtime.source_time_budget_sec.get(item.source, 0))

        # 在目标边界优先补足相对预算消耗较少的题源；同题源仍按计划优先级和场次顺序。
        # 只调顺序，不丢弃目标，不按到时强制结束，也不打断正在作答的问题。
        minimum = min(consumed_share(item) for item in candidates)
        selected = next((item for item in candidates if item.id == preferred and consumed_share(item) <= minimum), None)
        if selected is None:
            selected = min(candidates, key=consumed_share)
        return selected, progress.state(selected.id)

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
        exclude: str = "",
    ) -> tuple[NextTurn, ObjectiveProgress] | None:
        """选择合法目标；模型措辞只用于明确指定的新项目目标。"""
        chosen = self._next_pending(
            runtime, progress, session_id=session_id, preferred=preferred,
            exclude=exclude,
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
                    text=snapshot.question_text,
                    question_id=str(snapshot.id), activate=True, advance_cursor=True,
                ), state
            text = fallback_text if first and preferred == objective.id else ""
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
