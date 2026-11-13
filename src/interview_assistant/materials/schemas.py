from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_TEXT_BYTES = 1024 * 1024


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResumeFields(InputModel):
    name: str = Field(min_length=1, max_length=200)
    source_text: str = Field(min_length=1, max_length=MAX_TEXT_BYTES)
    source_file_name: str = Field(default="", max_length=255)
    document_id: UUID | None = None
    is_active: bool = True

    @field_validator("name", "source_text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("名称与简历文字不能为空")
        if "\x00" in value:
            raise ValueError("内容不能包含空字符")
        try:
            if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
                raise ValueError("简历文字最多 1 MiB")
        except UnicodeEncodeError:
            raise ValueError("内容必须是有效 UTF-8 文字") from None
        return value

    @field_validator("source_file_name")
    @classmethod
    def filename(cls, value: str) -> str:
        if any(character in value for character in '/\\') or any(ord(c) < 32 for c in value):
            raise ValueError("仅填写文件名，不接受本机路径或控制字符")
        return value


class ResumeCreate(ResumeFields):
    pass


class ResumeUpdate(ResumeFields):
    expected_revision: int = Field(ge=1)


class ResumeSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    source_file_name: str
    document_id: UUID | None
    is_active: bool
    content_version: int
    revision: int
    created_at: datetime
    updated_at: datetime


class ResumeRead(ResumeSummary):
    source_text: str


class ResumeList(BaseModel):
    items: list[ResumeSummary]
    total: int
    limit: int
    offset: int


class VersionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    resume_id: UUID
    version: int
    name: str
    source_file_name: str
    document_id: UUID | None
    content_sha256: str
    created_at: datetime


class VersionRead(VersionSummary):
    source_text: str


class VersionList(BaseModel):
    items: list[VersionSummary]
    total: int
    limit: int
    offset: int


class DocumentRead(BaseModel):
    id: UUID
    file_name: str
    content_sha256: str
    extracted_text: str
    page_count: int
    warnings: list[str]
    ocr_mode: str
    ocr_pages: list[int]
    page_methods: list[str]
    extraction_version: str
    cache_hit: bool = False


class TextFormatInput(InputModel):
    source_text: str = Field(min_length=1, max_length=MAX_TEXT_BYTES)

    @field_validator("source_text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return ResumeFields.nonblank(value)


class TextFormatRead(BaseModel):
    source_text: str


class FactInput(InputModel):
    fact_type: str = Field(default="职责", min_length=1, max_length=50)
    content: str = Field(min_length=1, max_length=10000)
    evidence_quote: str = Field(default="", max_length=20000)

    @field_validator("content", "fact_type")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("事实类型与内容不能为空")
        return value

    @field_validator("evidence_quote")
    @classmethod
    def require_meaningful_quote(cls, value: str) -> str:
        if value and not value.strip():
            raise ValueError("原文引用不能只包含空白；无引用请使用空字符串")
        return value


class FactRead(FactInput):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_kind: str


class ProjectFields(InputModel):
    name: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=50000)
    role: str = Field(default="", max_length=500)
    duration: str = Field(default="", max_length=100)
    tech_stack: list[str] = Field(default_factory=list, max_length=50)
    user_verified: bool = False
    facts: list[FactInput] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_fields(self) -> Self:
        if not self.name.strip():
            raise ValueError("项目名称不能为空")
        if any(not item.strip() or len(item) > 100 for item in self.tech_stack):
            raise ValueError("技术标签不能为空，且每个最多 100 字")
        return self


class ProjectCreate(ProjectFields):
    source_version: int = Field(ge=1, description="项目所依据的简历版本，创建后不自动切换")


class ProjectUpdate(ProjectFields):
    expected_revision: int = Field(ge=1)


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    resume_id: UUID
    source_version: int
    name: str
    summary: str
    role: str
    duration: str
    tech_stack: list[str]
    user_verified: bool
    facts: list[FactRead]
    revision: int
    created_at: datetime
    updated_at: datetime


class ProjectList(BaseModel):
    items: list[ProjectRead]
    total: int
    limit: int
    offset: int
