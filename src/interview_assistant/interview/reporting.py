"""整场复盘（Offline Path）：生成、幂等重放、规则降级与报告清洗。

组合对象共享主服务的数据库会话与设置；模型调用仍在事务之外。
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

if TYPE_CHECKING:
    from interview_assistant.interview.service import InterviewService

logger = logging.getLogger(__name__)

class ReportService:
    def __init__(self, owner: "InterviewService") -> None:
        self.owner = owner

    # ---------- 整场复盘 ----------

    async def get_report(self, session_id: str) -> ReportRead | None:
        async with self.owner.session.begin():
            row = await self.owner.session.scalar(
                select(InterviewReport).where(InterviewReport.session_id == session_id)
            )
            return self._report_read(row) if row is not None else None

    async def report(
        self, session_id: str, payload: ReportCreate,
    ) -> tuple[ReportRead, bool]:
        """为已结束的场次生成整场复盘；每场一份，失败行可被重试覆盖。"""
        request_key = str(payload.request_id)
        async with self.owner.session.begin():
            row = await self.owner.require_session(session_id)
            if row.status != "completed":
                raise InterviewError(
                    "session_not_completed",
                    "只有已结束的场次才能生成复盘；请先结束面试",
                    409,
                )
            existing = await self.owner.session.scalar(
                select(InterviewReport).where(InterviewReport.session_id == session_id)
            )
            # 重放同一请求标识或已有成品：直接返回，不重复调用模型。
            if existing is not None and (
                existing.request_key == request_key or existing.status == "ready"
            ):
                return self._report_read(existing), False
            runtime = PlanRuntime.model_validate(row.runtime_json)
            progress = SessionProgress.model_validate(row.progress_json)
            turns = await self.owner.turns(session_id)
            ended = row.ended_at or utc_now()
            elapsed_sec = max(0, int((ended - row.started_at).total_seconds()))
            rounds = self._report_rounds(turns)
            stats = self._report_stats(progress, turns, elapsed_sec)
            mode = row.mode

        if mode != "rule" and configuration_status(self.owner.settings)["configured"]:
            content, error = await self._model_report(runtime, rounds, elapsed_sec)
        else:
            content, error = self._rule_report(runtime, progress, turns), None

        async with self.owner.session.begin():
            row = await self.owner.session.scalar(
                select(InterviewReport)
                .where(InterviewReport.session_id == session_id)
                .with_for_update()
            )
            if row is None:
                row = InterviewReport(
                    session_id=session_id, status="ready", source="model",
                    stats_json=stats, request_key=request_key,
                )
                self.owner.session.add(row)
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
            await self.owner.session.flush()
            return self._report_read(row), True

    async def _model_report(
        self, runtime: PlanRuntime, rounds: list[dict[str, object]], elapsed_sec: int,
    ) -> tuple[ReportContent | None, ModelError | None]:
        """调用模型生成复盘；失败返回错误而不是抛出，让调用方落库 failed 行。"""
        model = QwenModel(self.owner.settings)
        try:
            draft = await model.generate(
                messages=report_messages(
                    runtime=runtime, rounds=rounds, elapsed_sec=elapsed_sec,
                ),
                output_type=ReportDraft,
                prompt_version=REPORT_PROMPT_VERSION,
                schema_version=REPORT_SCHEMA_VERSION,
                temperature=self.owner.settings.qwen_interview_temperature,
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
