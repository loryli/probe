"""文字面试的运行期契约：场次入参、决策输出、进度与读取结构。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_ANSWER_CHARS = 8000
PROJECT_BASE_QUESTIONS = 2
_TRUE_VALUES = {"true", "yes", "y", "1", "是"}
_FALSE_VALUES = {"false", "no", "n", "0", "否", ""}


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SessionCreate(InputModel):
    request_id: UUID = Field(description="场次幂等键；重放同一标识只读取原场次")
    plan_id: UUID
    mode: Literal["model", "rule"] = "model"


class AnswerSubmit(InputModel):
    request_id: UUID = Field(description="回答幂等键；重复提交不重复保存、不重复调用模型")
    turn_id: UUID = Field(description="所回答的问题轮次 ID")
    answer_text: str = Field(min_length=1, max_length=MAX_ANSWER_CHARS)

    @field_validator("answer_text")
    @classmethod
    def strip_answer(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("回答不能为空")
        return text


class SessionControl(InputModel):
    expected_revision: int = Field(ge=1)


class DecisionDraft(InputModel):
    """模型只做本轮判断与措辞；是否追问、切目标与收尾由服务端按配额决定。"""

    schema_version: Literal["interview_decision.v1"] = "interview_decision.v1"
    objective_status: Literal["covered", "partial", "insufficient"]
    follow_up_needed: bool = False
    follow_up_question: str = Field(default="", max_length=300)
    next_objective_id: str = Field(default="", max_length=16)
    next_question: str = Field(default="", max_length=300)
    observation: str = Field(default="", max_length=160)
    reason: str = Field(default="", max_length=80)

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data: object) -> object:
        """只裁剪长度与未知字段；枚举保持严格，避免把非法决策包装成合法结果。"""
        if not isinstance(data, dict):
            return data
        cleaned = {
            key: value for key, value in data.items()
            if key in {
                "schema_version", "objective_status", "follow_up_needed",
                "follow_up_question", "next_objective_id", "next_question",
                "observation", "reason",
            }
        }
        for key, limit in (
            ("follow_up_question", 300), ("next_question", 300),
            ("next_objective_id", 16), ("observation", 160), ("reason", 80),
        ):
            value = cleaned.get(key)
            if isinstance(value, str):
                cleaned[key] = value.strip()[:limit]
        flag = cleaned.get("follow_up_needed")
        if isinstance(flag, str):
            lowered = flag.strip().lower()
            if lowered in _TRUE_VALUES:
                cleaned["follow_up_needed"] = True
            elif lowered in _FALSE_VALUES:
                cleaned["follow_up_needed"] = False
        status = cleaned.get("objective_status")
        if isinstance(status, str):
            cleaned["objective_status"] = status.strip().lower()
        return cleaned


class QuestionDraft(InputModel):
    """开场问题：服务端已经选定目标，模型只负责措辞。"""

    schema_version: Literal["interview_question.v1"] = "interview_question.v1"
    question: str = Field(min_length=1, max_length=300)
    focus: str = Field(default="", max_length=80)

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        cleaned = {
            key: value for key, value in data.items()
            if key in {"schema_version", "question", "focus"}
        }
        for key, limit in (("question", 300), ("focus", 80)):
            value = cleaned.get(key)
            if isinstance(value, str):
                cleaned[key] = value.strip()[:limit]
        return cleaned


class OutlineQuestion(InputModel):
    """骨架里的一道基础问题：只来自该目标冻结的项目事实。"""

    text: str = Field(min_length=1, max_length=300)
    focus: str = Field(default="", max_length=80)


class OutlineObjective(InputModel):
    objective_id: str = Field(min_length=1, max_length=40)
    questions: list[OutlineQuestion] = Field(min_length=1, max_length=PROJECT_BASE_QUESTIONS)


class OutlineDraft(InputModel):
    """模型一次批量产出的基础问题骨架；题库目标不参与，仍用用户原题。"""

    schema_version: Literal["interview_outline.v1"] = "interview_outline.v1"
    objectives: list[OutlineObjective] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data: object) -> object:
        """容忍 questions 为字符串、字段名写成 question/id、以及目标重复。"""
        if not isinstance(data, dict):
            return data
        merged: dict[str, list[dict[str, str]]] = {}
        order: list[str] = []
        for raw in data.get("objectives") or []:
            if not isinstance(raw, dict):
                continue
            objective_id = raw.get("objective_id") or raw.get("id")
            if not isinstance(objective_id, str) or not objective_id.strip():
                continue
            key = objective_id.strip()[:40]
            if key not in merged:
                merged[key] = []
                order.append(key)
            for item in raw.get("questions") or raw.get("items") or []:
                if isinstance(item, str):
                    text, focus = item, ""
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("question") or ""
                    focus = item.get("focus") or ""
                else:
                    continue
                if not isinstance(text, str) or not text.strip():
                    continue
                if len(merged[key]) >= PROJECT_BASE_QUESTIONS:
                    break
                merged[key].append({
                    "text": text.strip()[:300],
                    "focus": str(focus).strip()[:80],
                })
        return {
            "schema_version": "interview_outline.v1",
            "objectives": [
                {"objective_id": key, "questions": merged[key]}
                for key in order if merged[key]
            ],
        }


class SessionOutline(InputModel):
    """落库的骨架：source 说明它来自模型、规则模板，还是生成失败后的降级。"""

    schema_version: Literal["interview_outline.v1"] = "interview_outline.v1"
    source: Literal["model", "rule", "fallback"] = "model"
    objectives: list[OutlineObjective] = Field(default_factory=list)

    def questions(self, objective_id: str) -> list[OutlineQuestion]:
        for item in self.objectives:
            if item.objective_id == objective_id:
                return item.questions
        return []


class ObjectiveProgress(InputModel):
    """单个目标的推进状态；由服务端维护，不交给模型。"""

    id: str
    status: Literal["pending", "active", "covered", "skipped"] = "pending"
    asked: int = Field(default=0, ge=0)
    followups: int = Field(default=0, ge=0)
    cursor: int = Field(default=0, ge=0)


class SessionProgress(InputModel):
    objectives: list[ObjectiveProgress]
    followups_used: int = Field(default=0, ge=0)
    started_at: datetime
    completion_reason: str = ""

    def state(self, objective_id: str) -> ObjectiveProgress:
        for item in self.objectives:
            if item.id == objective_id:
                return item
        raise KeyError(objective_id)


class TurnDecision(InputModel):
    objective_status: Literal["covered", "partial", "insufficient"]
    action: Literal["follow_up", "continue", "switch", "complete"]
    observation: str = ""
    reason: str = ""
    followups_used: int = 0


class TurnRead(BaseModel):
    id: UUID
    sequence: int
    role: Literal["interviewer", "candidate"]
    objective_id: str | None
    source: Literal["project", "question_bank"] | None
    question_id: UUID | None
    is_followup: bool
    text: str
    answered: bool
    skipped: bool
    decision: TurnDecision | None
    created_at: datetime


class ObjectiveState(BaseModel):
    id: str
    topic: str
    source: Literal["project", "question_bank"]
    priority: Literal["high", "medium", "low"]
    status: Literal["pending", "active", "covered", "skipped"]
    asked: int
    followups: int
    max_followups: int
    time_budget_sec: int


class SessionRead(BaseModel):
    id: UUID
    plan_id: UUID
    plan_revision: int
    status: Literal["active", "paused", "completed"]
    revision: int
    mode: Literal["model", "rule"]
    provider: str
    model: str
    prompt_version: str
    schema_version: str
    direction: str
    duration_target_sec: int
    elapsed_sec: int
    remaining_sec: int
    objectives: list[ObjectiveState]
    turns: list[TurnRead]
    current_turn: TurnRead | None
    pending_turn_id: UUID | None
    answer_count: int
    question_count: int
    followups_used: int
    completion_reason: str
    error_code: str | None
    error_message: str | None
    started_at: datetime
    ended_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SessionSummary(BaseModel):
    id: UUID
    plan_id: UUID
    status: Literal["active", "paused", "completed"]
    mode: Literal["model", "rule"]
    direction: str
    answer_count: int
    question_count: int
    objective_total: int
    objective_done: int
    elapsed_sec: int
    created_at: datetime
    started_at: datetime
    ended_at: datetime | None


class SessionList(BaseModel):
    items: list[SessionSummary]
    total: int
    limit: int
    offset: int


class PlanOption(BaseModel):
    id: UUID
    direction: str
    duration_minutes: int
    objective_count: int
    usable: bool = Field(default=True, description="旧版计划缺少新版运行依据时不可用于开始面试")
    reason: str = Field(default="", description="不可用原因，供页面直接展示")
    confirmed_at: datetime | None
    created_at: datetime


class SessionOptions(BaseModel):
    plans: list[PlanOption]
    model: dict[str, object]
