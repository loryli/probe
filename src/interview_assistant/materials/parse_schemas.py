from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator

from interview_assistant.materials.schemas import InputModel, ProjectFields


class ExtractedFact(InputModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    fact_type: str = Field(min_length=1, max_length=50)
    content: str = Field(min_length=1, max_length=10000)
    evidence_quote: str = Field(min_length=1, max_length=20000)
    confidence: float = Field(ge=0, le=1)
    needs_confirmation: list[str] = Field(default_factory=list, max_length=20)


class ExtractedProject(InputModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    name: str = Field(min_length=1, max_length=200)
    evidence_quote: str = Field(min_length=1, max_length=20000)
    summary: str | None = Field(default=None, max_length=10000)
    role: str | None = Field(default=None, max_length=500)
    duration: str | None = Field(default=None, max_length=100)
    tech_stack: list[str] = Field(default_factory=list, max_length=50)
    facts: list[ExtractedFact] = Field(default_factory=list, max_length=40)
    confidence: float = Field(ge=0, le=1)
    needs_confirmation: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("tech_stack")
    @classmethod
    def validate_tags(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 100 for value in values):
            raise ValueError("技术栈标签无效")
        return values


class ResumeParseOutput(InputModel):
    schema_version: Literal["resume_parse.v1"]
    projects: list[ExtractedProject] = Field(max_length=15)
    needs_confirmation: list[str] = Field(default_factory=list, max_length=50)


class ParseCreate(InputModel):
    request_id: UUID = Field(description="本次请求的 UUID；超时重试复用，不重复调用模型")
    source_version: int = Field(ge=1)


class ParseConfirm(ProjectFields):
    candidate_index: int = Field(ge=0)
    expected_revision: int = Field(ge=1)


class ParseDraftRead(InputModel):
    id: UUID
    resume_id: UUID
    source_version: int
    status: str
    revision: int
    model: str
    prompt_version: str
    schema_version: str
    result: ResumeParseOutput | None
    confirmed_projects: dict[str, UUID]
    error_code: str | None
    error_message: str | None
    elapsed_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    created_at: datetime


class ParseDraftList(InputModel):
    items: list[ParseDraftRead]
    total: int
    limit: int
    offset: int
