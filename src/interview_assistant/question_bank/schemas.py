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
    category_id: UUID | None = None

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
    category_id: UUID | None = None

    @model_validator(mode="after")
    def validate_changes(self) -> Self:
        changes = self.model_fields_set - {"expected_revision"}
        if not changes:
            raise ValueError("至少提供一个待修改字段")
        if any(getattr(self, name) is None for name in changes - {"category_id"}):
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
    category_id: UUID | None = None
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


class CategoryName(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value or "\n" in value or "\r" in value:
            raise ValueError("分类名称不能为空或包含换行")
        return value


class CategoryCreate(CategoryName):
    request_id: UUID


class CategoryRename(CategoryName):
    expected_revision: int = Field(ge=1)


class CategoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    revision: int
    question_count: int = 0


class CategoryList(BaseModel):
    items: list[CategoryRead]
    total_questions: int
    uncategorized_count: int


class QuestionMove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    expected_revision: int = Field(ge=1)


class CategoryAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_id: UUID | None
    questions: list[QuestionMove] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_questions(self) -> Self:
        if len({item.id for item in self.questions}) != len(self.questions):
            raise ValueError("同一道题目不能重复提交")
        return self


class QuestionBatchDelete(BaseModel):
    """批量逻辑删除题目；每个条目携带 revision，防止删掉别人刚改过的内容。"""

    model_config = ConfigDict(extra="forbid")

    questions: list[QuestionMove] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_questions(self) -> Self:
        if len({item.id for item in self.questions}) != len(self.questions):
            raise ValueError("同一道题目不能重复提交")
        return self


class CategoryMove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    expected_revision: int = Field(ge=1)


class CategoryBatchDelete(BaseModel):
    """批量删除分类；只有没有任何未删除题目的分类可以被删除。"""

    model_config = ConfigDict(extra="forbid")

    categories: list[CategoryMove] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_categories(self) -> Self:
        if len({item.id for item in self.categories}) != len(self.categories):
            raise ValueError("同一个分类不能重复提交")
        return self


class DeleteResult(BaseModel):
    deleted: int
