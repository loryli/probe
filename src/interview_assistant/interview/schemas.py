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


class SessionBatchDelete(InputModel):
    """批量删除面试记录：物理删除场次、全部轮次与复盘，不可恢复。"""

    session_ids: list[UUID] = Field(min_length=1, max_length=50)

    @field_validator("session_ids")
    @classmethod
    def unique_sessions(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("同一场次不能重复提交")
        return value


class DecisionDraft(InputModel):
    """模型只做本轮判断与措辞；是否追问、切目标与收尾由服务端按配额决定。

    字段顺序即输出顺序：判定字段在前，`say` 紧随其后，其余元数据在后。
    完整解析、配额校验与落库后，才向页面发送实际保存的问题。
    """

    schema_version: str = Field(default="interview_decision.v2", max_length=32)
    objective_status: Literal["covered", "partial", "insufficient"]
    follow_up_needed: bool = False
    say: str = Field(default="", max_length=300)
    next_objective_id: str = Field(default="", max_length=16)
    follow_up_question: str = Field(default="", max_length=300)
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
                "objective_status", "follow_up_needed", "say",
                "follow_up_question", "next_objective_id", "next_question",
                "observation", "reason",
            }
        }
        for key, limit in (
            ("say", 300), ("follow_up_question", 300), ("next_question", 300),
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

    @property
    def spoken(self) -> str:
        """面试官真正要说出口的那一句；旧字段作为兼容回退。"""
        return self.say or self.follow_up_question or self.next_question


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
    elapsed_sec: int = Field(default=0, ge=0)
    started_elapsed_sec: int | None = Field(default=None, ge=0)


class SessionProgress(InputModel):
    objectives: list[ObjectiveProgress]
    followups_used: int = Field(default=0, ge=0)
    started_at: datetime
    paused_seconds: float = Field(default=0, ge=0)
    paused_at: datetime | None = None
    completion_reason: str = ""
    # 缺省 technical 兼容已有场次；只有新建简历面试才显式从自我介绍开始。
    phase: Literal["self_introduction", "project_introduction", "technical"] = "technical"
    opening_questions: dict[Literal["self_introduction", "project_introduction"], str] = Field(default_factory=dict)
    opening_answers: dict[Literal["self_introduction", "project_introduction"], str] = Field(default_factory=dict)

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
    stage: Literal["self_introduction", "project_introduction", "technical"] = "technical"
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
    elapsed_sec: int = 0


class SessionRead(BaseModel):
    phase: Literal["self_introduction", "project_introduction", "technical"] = "technical"
    practice_mode: Literal["question_bank", "resume"] = "resume"
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
    practice_mode: Literal["question_bank", "resume"] = "resume"
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


class ReportCreate(InputModel):
    request_id: UUID = Field(description="复盘幂等键；重放同一标识返回已生成的报告")


class ReportEvaluation(BaseModel):
    """复盘里对一个考察目标的评价；turn_sequences 是支撑证据的回答轮次号。"""

    objective_id: str = Field(max_length=40)
    status: Literal["covered", "partial", "missed"]
    comment: str = Field(default="", max_length=400)
    highlights: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    turn_sequences: list[int] = Field(default_factory=list)


class ReportContent(InputModel):
    """落库的复盘内容；source 说明它来自模型还是规则统计。"""

    schema_version: Literal["interview_report.v1"] = "interview_report.v1"
    overall: Literal["solid", "mixed", "weak"] = "mixed"
    summary: str = Field(default="", max_length=600)
    evaluations: list[ReportEvaluation] = Field(default_factory=list)
    weak_topics: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class ReportDraft(ReportContent):
    """模型一次产出的整场复盘；normalize 容忍字段名与形态偏差。"""

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        cleaned: dict[str, object] = {
            key: value for key, value in data.items()
            if key in {
                "overall", "summary", "evaluations", "weak_topics",
                "suggestions", "risks",
            }
        }
        if isinstance(cleaned.get("summary"), str):
            cleaned["summary"] = cleaned["summary"].strip()[:600]
        overall = cleaned.get("overall")
        if isinstance(overall, str):
            lowered = overall.strip().lower()
            cleaned["overall"] = lowered if lowered in {"solid", "mixed", "weak"} else "mixed"
        evaluations: list[dict[str, object]] = []
        for raw in cleaned.get("evaluations") or []:
            if not isinstance(raw, dict):
                continue
            objective_id = raw.get("objective_id") or raw.get("id") or raw.get("objective")
            if not isinstance(objective_id, str) or not objective_id.strip():
                continue
            status = raw.get("status") or raw.get("coverage")
            status = status.strip().lower() if isinstance(status, str) else ""
            if status not in {"covered", "partial", "missed"}:
                status = "partial"
            comment = raw.get("comment") or raw.get("evaluation") or ""
            highlights = [
                item.strip()[:200] for item in _string_list(raw.get("highlights"))
                if item.strip()
            ][:4]
            gaps = [
                item.strip()[:200] for item in _string_list(raw.get("gaps"))
                if item.strip()
            ][:4]
            sequences = [
                int(item) for item in _string_list(raw.get("turn_sequences"))
                if str(item).strip().lstrip("-").isdigit()
            ][:8]
            evaluations.append({
                "objective_id": objective_id.strip()[:40],
                "status": status,
                "comment": str(comment).strip()[:400],
                "highlights": highlights,
                "gaps": gaps,
                "turn_sequences": sequences,
            })
        cleaned["evaluations"] = evaluations
        cleaned["weak_topics"] = [
            _topic_text(item) for item in _string_list(cleaned.get("weak_topics"))
            if _topic_text(item)
        ][:5]
        cleaned["suggestions"] = [
            item.strip()[:200] for item in _string_list(cleaned.get("suggestions"))
            if item.strip()
        ][:5]
        cleaned["risks"] = [
            item.strip()[:200] for item in _string_list(cleaned.get("risks"))
            if item.strip()
        ][:5]
        return cleaned


def _string_list(value: object) -> list[str]:
    """容忍模型把字符串列表写成对象列表、单个字符串或逗号串。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        for key in ("topic", "detail", "text", "note"):
            if isinstance(value.get(key), str):
                return [value[key]]
        return []
    if isinstance(value, list):
        items: list[str] = []
        for item in value:
            if isinstance(item, str):
                items.append(item)
            elif isinstance(item, dict):
                for key in ("topic", "detail", "text", "note"):
                    if isinstance(item.get(key), str):
                        items.append(item[key])
                        break
        return items
    return []


def _topic_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()[:120]
    if isinstance(value, dict):
        parts = [str(value.get(key) or "").strip() for key in ("topic", "detail")]
        return "：".join(part for part in parts if part)[:200]
    return ""


class ReportRead(BaseModel):
    id: UUID
    session_id: UUID
    status: Literal["ready", "failed"]
    source: Literal["model", "rule"]
    report: ReportContent | None
    stats: dict[str, int]
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
