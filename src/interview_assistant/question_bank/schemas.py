from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class QuestionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_text: str = Field(min_length=1)
    standard_answer_text: str = ""
    user_notes: str = ""
    is_active: bool = True

    @field_validator("question_text")
    @classmethod
    def require_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("题干不能只包含空白字符")
        return value


class QuestionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(
        ge=1, description="最近读取题目时返回的 revision，用于检查并发修改"
    )
    question_text: str | None = Field(default=None, min_length=1)
    standard_answer_text: str | None = None
    user_notes: str | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def validate_changes(self) -> Self:
        changes = self.model_fields_set - {"expected_revision"}
        if not changes:
            raise ValueError("至少提供一个待修改字段")
        if any(getattr(self, name) is None for name in changes):
            raise ValueError("字段不能为 null；清空答案或备注请使用空字符串")
        if self.question_text is not None and not self.question_text.strip():
            raise ValueError("题干不能只包含空白字符")
        return self


class QuestionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    question_text: str
    standard_answer_text: str
    user_notes: str
    source: str
    is_active: bool
    content_version: int
    revision: int
    created_at: datetime
    updated_at: datetime


class QuestionVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    question_id: UUID
    version: int
    question_text: str
    standard_answer_text: str
    user_notes: str
    source: str
    created_at: datetime


class QuestionList(BaseModel):
    items: list[QuestionRead]
    total: int
    limit: int
    offset: int


class QuestionHistory(BaseModel):
    items: list[QuestionVersionRead]
    total: int
    limit: int
    offset: int
