from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_PROJECTS = 8
MAX_QUESTIONS = 40
MAX_OBJECTIVES = 8
MAX_FACTS_PER_PROJECT = 12
MAX_QUESTION_TEXT_CHARS = 200
MAX_FACT_CHARS = 120
MODEL_QUESTION_CHARS = 80


def objective_limit(duration_minutes: int) -> int:
    """目标数按时长程序化确定，避免短场次被塞满目标、也避免模型凑数。"""
    return max(4, min(MAX_OBJECTIVES, duration_minutes // 6))


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PlanInputs(InputModel):
    mode: Literal["model", "rule"] = "model"
    resume_id: UUID | None = None
    source_version: int | None = Field(default=None, ge=1)
    project_ids: list[UUID] = Field(default_factory=list, max_length=MAX_PROJECTS)
    question_ids: list[UUID] = Field(default_factory=list, max_length=MAX_QUESTIONS)
    direction: str = Field(default="", max_length=500)
    project_ratio: float = Field(default=0.5, ge=0, le=1)
    duration_minutes: int = Field(default=45, ge=10, le=180)
    max_total_followups: int = Field(default=0, ge=0, le=30)

    @field_validator("project_ids", "question_ids")
    @classmethod
    def unique_ids(cls, values: list[UUID]) -> list[UUID]:
        seen: set[UUID] = set()
        result: list[UUID] = []
        for value in values:
            if value not in seen:
                seen.add(value)
                result.append(value)
        return result

    @field_validator("direction")
    @classmethod
    def clean_direction(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_material_mode(self) -> "PlanInputs":
        if self.resume_id is None:
            if self.source_version is not None or self.project_ids:
                raise ValueError("纯题库练习不绑定简历版本或项目")
            if not self.question_ids:
                raise ValueError("纯题库练习请至少选择一道题目")
            self.project_ratio = 0
            self.max_total_followups = 0
            if not self.direction:
                self.direction = "题库练习"
        elif self.source_version is None:
            raise ValueError("使用简历时必须选择具体版本")
        return self


class PlanCreate(PlanInputs):
    request_id: UUID = Field(description="本次规划的 UUID；超时重试复用，不重复调用模型")


class PlanConfirm(InputModel):
    expected_revision: int = Field(ge=1)


class SnapshotFact(InputModel):
    id: UUID
    fact_type: str
    content: str
    source_kind: Literal["resume", "user_edit"] | None = None
    evidence_quote: str | None = None


class SnapshotProject(InputModel):
    id: UUID
    revision: int | None = None
    name: str
    summary: str = ""
    duration: str = ""
    role: str
    tech_stack: list[str]
    facts: list[SnapshotFact]


class SnapshotQuestion(InputModel):
    id: UUID
    question_text: str
    content_version: int
    standard_answer_text: str | None = None
    user_notes: str | None = None


class PlanSnapshot(InputModel):
    """生成时固定的资料版本；确认计划时据此复核引用是否仍然有效。"""

    snapshot_version: Literal[1, 2] = 1
    resume_id: UUID | None = None
    resume_name: str
    source_version: int | None = None
    content_sha256: str
    projects: list[SnapshotProject]
    questions: list[SnapshotQuestion]


def planning_sources(snapshot: PlanSnapshot, inputs: PlanInputs) -> list[str]:
    """只有一种材料时允许降级；两种都有时尊重 0% / 100% 排除。"""
    project = any(item.facts for item in snapshot.projects)
    bank = bool(snapshot.questions)
    if project and bank:
        return (["project"] if inputs.project_ratio > 0 else []) + (
            ["question_bank"] if inputs.project_ratio < 1 else []
        )
    return (["project"] if project else []) + (["question_bank"] if bank else [])


class ObjectiveDraft(InputModel):
    """模型只输出语义骨架；未知字段丢弃，枚举与引用不做纠正。"""

    topic: str = Field(min_length=1, max_length=120)
    source_preference: Literal["project", "question_bank"]
    priority: Literal["high", "medium", "low"]
    observable_evidence: list[str] = Field(default_factory=list, max_length=3)
    project_fact_ids: list[str] = Field(default_factory=list, max_length=20)
    question_ids: list[str] = Field(default_factory=list, max_length=20)
    completion_rule: str = Field(default="", max_length=120)
    max_followups: int = Field(default=1, ge=0, le=10)

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data: object) -> object:
        """只做空白、长度、数量与未知字段的格式处理；枚举和引用仍严格校验。"""
        if not isinstance(data, dict):
            return data
        cleaned = {
            key: value for key, value in data.items()
            if key in {
                "topic", "source_preference", "priority", "observable_evidence",
                "project_fact_ids", "question_ids", "completion_rule", "max_followups",
            }
        }
        topic = cleaned.get("topic")
        if isinstance(topic, str):
            cleaned["topic"] = topic.strip()[:120]
        evidence = cleaned.get("observable_evidence")
        if isinstance(evidence, list):
            cleaned["observable_evidence"] = [
                item.strip()[:80] for item in evidence if isinstance(item, str)
            ][:3]
        rule = cleaned.get("completion_rule")
        if isinstance(rule, str):
            cleaned["completion_rule"] = rule.strip()[:120]
        for key in ("project_fact_ids", "question_ids"):
            values = cleaned.get(key)
            if isinstance(values, list):
                cleaned[key] = [str(item) for item in values][:20]
        followups = cleaned.get("max_followups")
        if isinstance(followups, int) and not isinstance(followups, bool):
            cleaned["max_followups"] = max(0, min(followups, 3))
        return cleaned

    @field_validator("priority", mode="before")
    @classmethod
    def normalize_priority(cls, value: object) -> object:
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"high", "medium", "low"}:
                return lowered
            return lowered
        return value

    @field_validator("source_preference", mode="before")
    @classmethod
    def normalize_source(cls, value: object) -> object:
        if isinstance(value, str):
            lowered = value.strip().lower()
            return lowered
        return value


class PlannerDraft(InputModel):
    """session_summary 仅为兼容旧模型输出保留，服务端一律程序化生成摘要。"""

    schema_version: Literal["planner.v2"] = "planner.v2"
    session_summary: str = Field(default="", max_length=500)
    objectives: list[ObjectiveDraft] = Field(default_factory=list, max_length=MAX_OBJECTIVES)
    limitations: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data: object) -> object:
        """schema_version 不纠正；其余只裁剪长度、数量与未知字段。"""
        if not isinstance(data, dict):
            return data
        cleaned = {
            key: value for key, value in data.items()
            if key in {"schema_version", "session_summary", "objectives", "limitations", "notes"}
        }
        if "limitations" not in cleaned and "notes" in cleaned:
            cleaned["limitations"] = cleaned.pop("notes")
        summary = cleaned.get("session_summary")
        if isinstance(summary, str):
            cleaned["session_summary"] = summary.strip()[:500]
        notes = cleaned.get("limitations")
        if isinstance(notes, list):
            cleaned["limitations"] = [
                item.strip()[:300] for item in notes if isinstance(item, str)
            ][:6]
        objectives = cleaned.get("objectives")
        if isinstance(objectives, list):
            cleaned["objectives"] = objectives[:MAX_OBJECTIVES]
        return cleaned


class PlanConstraints(InputModel):
    max_total_followups: int
    max_followups_per_objective: int
    disabled_topics: list[str] = Field(default_factory=list)
    duration_is_soft: bool = True


class PlanObjective(InputModel):
    id: str
    topic: str
    source_preference: Literal["project", "question_bank"]
    priority: Literal["high", "medium", "low"]
    observable_evidence: list[str]
    project_fact_ids: list[UUID]
    question_ids: list[UUID]
    time_budget_sec: int
    max_followups: int
    completion_rule: str


class PlanNotes(InputModel):
    """提示按来源分区，模型自评一律标注为未经核验。"""

    system: list[str] = Field(default_factory=list)
    model: list[str] = Field(default_factory=list)
    policy: list[str] = Field(default_factory=list)


class PlanCoverage(InputModel):
    facts_used: int
    facts_total: int
    questions_used: int
    questions_total: int
    projects_used: int
    projects_total: int


class CandidateScope(InputModel):
    """允许选择的资料池；引用只是目标依据，选择仍须与当前目标相关。"""

    project_fact_ids: list[UUID] = Field(default_factory=list)
    question_ids: list[UUID] = Field(default_factory=list)


class PlanOutput(InputModel):
    """目标与预算，不含固定问题清单；新增字段均可选，旧计划仍可读取。"""

    schema_version: Literal["planner.v1", "planner.v2"]
    session_summary: str
    objectives: list[PlanObjective]
    source_time_budget_sec: dict[str, int]
    opening_buffer_sec: int
    closing_buffer_sec: int
    duration_target_sec: int
    constraints: PlanConstraints
    limitations: list[str] = Field(default_factory=list)
    notes: PlanNotes = Field(default_factory=PlanNotes)
    coverage: PlanCoverage | None = None
    runtime_context: dict[str, str] = Field(default_factory=dict)
    candidate_scope: CandidateScope | None = None

    @model_validator(mode="after")
    def validate_v2_budgets(self) -> "PlanOutput":
        if self.schema_version == "planner.v1":
            return self
        if not self.objectives or self.candidate_scope is None:
            raise ValueError("Plan requires objectives and a candidate scope")
        if len({item.id for item in self.objectives}) != len(self.objectives):
            raise ValueError("Duplicate objective ID")
        for source in ("project", "question_bank"):
            allocated = sum(
                item.time_budget_sec
                for item in self.objectives
                if item.source_preference == source
            )
            budget = self.source_time_budget_sec.get(source, -1)
            if budget < 0 or allocated != budget:
                raise ValueError("Source and objective budgets disagree")
        if any(item.time_budget_sec <= 0 for item in self.objectives):
            raise ValueError("Zero-budget objectives cannot be scheduled")
        total_budget = (
            sum(self.source_time_budget_sec.values())
            + self.opening_buffer_sec
            + self.closing_buffer_sec
        )
        if total_budget != self.duration_target_sec:
            raise ValueError("Plan budget does not match duration")
        followups = sum(item.max_followups for item in self.objectives)
        if followups > self.constraints.max_total_followups:
            raise ValueError("Follow-up allocations exceed total")
        return self


class PlanRuntimeObjective(BaseModel):
    id: str
    source: Literal["project", "question_bank"]
    priority: Literal["high", "medium", "low"]
    topic: str
    time_budget_sec: int
    max_followups: int
    context: str
    project_fact_ids: list[UUID]
    question_ids: list[UUID]
    observable_evidence: list[str]
    completion_rule: str


class PlanRuntime(BaseModel):
    """服务端运行时契约。快照是原文依据；context 只是摘要，不可用于恢复原题。"""

    contract_version: Literal["interview_runtime.v1"] = "interview_runtime.v1"
    plan_id: UUID
    plan_revision: int
    status: str
    confirmed_at: datetime | None
    resume_id: UUID | None
    source_version: int | None
    schema_version: str
    prompt_version: str
    direction: str
    duration_target_sec: int
    opening_buffer_sec: int
    closing_buffer_sec: int
    source_time_budget_sec: dict[str, int]
    constraints: PlanConstraints
    objectives: list[PlanRuntimeObjective]
    candidate_scope: CandidateScope
    materials: PlanSnapshot


class PlanRead(BaseModel):
    id: UUID
    resume_id: UUID | None
    source_version: int | None
    status: str
    revision: int
    provider: str
    model: str
    prompt_version: str
    schema_version: str
    inputs: PlanInputs
    snapshot: PlanSnapshot | None
    result: PlanOutput | None
    error_code: str | None
    error_message: str | None
    elapsed_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None


class PlanSummary(BaseModel):
    id: UUID
    status: str
    provider: str
    resume_id: UUID | None
    source_version: int | None
    direction: str
    duration_minutes: int
    objective_count: int
    created_at: datetime
    confirmed_at: datetime | None


class PlanList(BaseModel):
    items: list[PlanSummary]
    total: int
    limit: int
    offset: int


class ResumeOption(BaseModel):
    id: UUID
    name: str
    content_version: int
    is_active: bool


class VersionOption(BaseModel):
    version: int
    name: str
    content_sha256: str
    created_at: datetime


class ProjectOption(BaseModel):
    id: UUID
    name: str
    source_version: int
    tech_stack: list[str]
    fact_count: int


class QuestionOption(BaseModel):
    id: UUID
    question_text: str
    content_version: int


class PlanOptions(BaseModel):
    resumes: list[ResumeOption]
    versions: list[VersionOption]
    projects: list[ProjectOption]
    questions: list[QuestionOption]
    question_total: int
    selected_resume_id: UUID | None
    selected_version: int | None
    model: dict[str, object]
    limits: dict[str, int]
