"""简历面试的开场推进；介绍是本场原始回答，不写回简历、项目或长期记忆。"""

import re
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy.orm.exc import StaleDataError

from interview_assistant.database import utc_now
from interview_assistant.interview.errors import conflict
from interview_assistant.interview.models import InterviewTurn
from interview_assistant.interview.schemas import SessionProgress, TurnDecision
from interview_assistant.planner.schemas import PlanRuntime

if TYPE_CHECKING:
    from interview_assistant.interview.service import InterviewService


class OpeningService:
    def __init__(self, owner: "InterviewService") -> None:
        self.owner = owner

    async def ensure_question(self, session_id: str) -> bool:
        """开场不调用模型；同一阶段只创建一次问题，恢复时沿用原轮次。"""
        try:
            async with self.owner.session.begin():
                row = await self.owner.require_session(session_id, lock=True)
                progress = SessionProgress.model_validate(row.progress_json)
                if progress.phase == "technical":
                    return False
                if row.status != "active" or row.current_turn_id is not None:
                    return True
                if progress.phase in progress.opening_questions:
                    # 回答已保存、但推进尚未完成时由原回答的决策任务接续，不重复问介绍。
                    return True
                runtime = PlanRuntime.model_validate(row.runtime_json)
                if progress.phase == "self_introduction":
                    text = "你好，我们先从自我介绍开始。请简要介绍你的背景、主要经历，以及这次希望应聘的方向。"
                else:
                    text = "接下来，请选择一个你最熟悉的项目，介绍它解决的问题、整体方案，以及你本人负责的工作。我们会根据你的介绍展开讨论。"
                    names = "、".join(item.name for item in runtime.materials.projects[:5])
                    if names:
                        text += f"你可以从本次简历资料中的这些项目选择：{names}。"
                turn = InterviewTurn(
                    id=str(uuid4()), session_id=row.id,
                    sequence=await self.owner._next_sequence(row.id), role="interviewer",
                    objective_id=None, source=None, question_id=None, is_followup=False,
                    text=text, created_at=utc_now(),
                )
                self.owner.session.add(turn)
                await self.owner.session.flush()
                progress.opening_questions[progress.phase] = turn.id
                row.current_turn_id = turn.id
                row.progress_json = progress.model_dump(mode="json")
                row.updated_at = utc_now()
                row.error_code = row.error_message = None
                await self.owner.session.flush()
                return True
        except StaleDataError:
            raise conflict() from None

    async def advance_answer(self, session_id: str, answer_id: str) -> bool | None:
        """None 表示技术问答；False 表示开场已处理或暂不可推进；True 才创建下一步。"""
        try:
            async with self.owner.session.begin():
                row = await self.owner.require_session(session_id, lock=True)
                progress = SessionProgress.model_validate(row.progress_json)
                answer = await self.owner.session.get(InterviewTurn, answer_id)
                if answer is None or answer.session_id != session_id or answer.role != "candidate":
                    return None
                stage = next((key for key, value in progress.opening_questions.items()
                              if value == answer.answers_turn_id), None)
                if stage is None:
                    return None
                if answer.decision_json is not None or row.status != "active" or progress.phase != stage:
                    return False
                turns = await self.owner.turns(session_id)
                if any(item.sequence > answer.sequence for item in turns):
                    return False
                progress.opening_answers[stage] = answer.id
                progress.phase = "project_introduction" if stage == "self_introduction" else "technical"
                answer.decision_json = TurnDecision(
                    objective_status="partial", action="continue",
                    reason="开场介绍已保存，进入下一阶段；介绍不视为技术目标已覆盖。",
                    followups_used=progress.followups_used,
                ).model_dump(mode="json")
                row.progress_json = progress.model_dump(mode="json")
                row.updated_at = utc_now()
                row.error_code = row.error_message = None
                await self.owner.session.flush()
                return True
        except StaleDataError:
            raise conflict() from None

    @staticmethod
    def context(progress: SessionProgress, turns: list[InterviewTurn]) -> dict[str, object]:
        by_id = {item.id: item for item in turns if item.role == "candidate"}
        return {
            stage: {"turn_id": turn.id, "text": turn.text[:4000],
                    "truncated": len(turn.text) > 4000, "verified_fact": False}
            for stage, answer_id in progress.opening_answers.items()
            if (turn := by_id.get(answer_id)) is not None
        }

    @staticmethod
    def preferred_objective(runtime: PlanRuntime, progress: SessionProgress, turns: list[InterviewTurn]) -> str:
        if not progress.opening_answers or any(item.asked for item in progress.objectives):
            return ""
        intro_id = progress.opening_answers.get("project_introduction")
        intro = " ".join(item.text.casefold() for item in turns if item.id == intro_id)
        matches = []
        for project in runtime.materials.projects:
            aliases = [project.name.casefold()]
            short = re.split(r"[·•：:（(—–|]", project.name, maxsplit=1)[0].strip().casefold()
            if len(short) >= 3:
                aliases.append(short)
            positions = [intro.find(alias) for alias in aliases if alias and alias in intro]
            if positions:
                matches.append((min(positions), project))
        chosen_project = min(matches, key=lambda item: item[0])[1] if matches else None
        mentioned = {str(fact.id) for fact in chosen_project.facts} if chosen_project else set()
        projects = [item for item in runtime.objectives if item.source == "project"]
        projects.sort(key=lambda item: {"high": 0, "medium": 1, "low": 2}[item.priority])
        chosen = next((item for item in projects
                       if any(str(value) in mentioned for value in item.project_fact_ids)), None)
        return chosen.id if chosen else projects[0].id if projects else ""
