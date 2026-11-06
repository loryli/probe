from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, ForeignKey, ForeignKeyConstraint,
    Index, Integer, LargeBinary, String, Text, text,
)
from sqlalchemy.dialects.mysql import LONGBLOB, LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from interview_assistant.database import Base, UTCDateTime, utc_now

content_text = Text().with_variant(LONGTEXT(), "mysql")
table_options = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


class ResumeDocument(Base):
    __tablename__ = "resume_document"
    __table_args__ = (table_options,)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    file_name: Mapped[str] = mapped_column(String(255))
    content_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    original_bytes: Mapped[bytes] = mapped_column(LargeBinary().with_variant(LONGBLOB(), "mysql"))
    extracted_text: Mapped[str] = mapped_column(content_text)
    page_count: Mapped[int] = mapped_column(Integer)
    warnings_json: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class ResumeExtraction(Base):
    __tablename__ = "resume_extraction"
    __table_args__ = (table_options,)

    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("resume_document.id", ondelete="RESTRICT"), primary_key=True
    )
    extraction_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    extracted_text: Mapped[str] = mapped_column(content_text)
    warnings_json: Mapped[list[str]] = mapped_column(JSON)
    ocr_pages_json: Mapped[list[int]] = mapped_column(JSON)
    page_methods_json: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class Resume(Base):
    __tablename__ = "resume"
    __table_args__ = (
        CheckConstraint("content_version >= 1", name="ck_resume_content_version"),
        CheckConstraint("revision >= 1", name="ck_resume_revision"),
        Index("ix_resume_listing", "deleted_at", "created_at", "id"),
        table_options,
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(200))
    source_text: Mapped[str] = mapped_column(content_text)
    source_file_name: Mapped[str] = mapped_column(String(255), default="")
    document_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("resume_document.id", ondelete="RESTRICT")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    content_version: Mapped[int] = mapped_column(Integer, default=1)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    creation_key: Mapped[str | None] = mapped_column(String(36), unique=True)
    creation_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __mapper_args__ = {"version_id_col": revision}


class ResumeVersion(Base):
    __tablename__ = "resume_version"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_resume_version_positive"), table_options,
    )

    resume_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("resume.id", ondelete="RESTRICT"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    source_text: Mapped[str] = mapped_column(content_text)
    source_file_name: Mapped[str] = mapped_column(String(255))
    content_sha256: Mapped[str] = mapped_column(String(64))
    document_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("resume_document.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Project(Base):
    __tablename__ = "project"
    __table_args__ = (
        ForeignKeyConstraint(
            ["resume_id", "source_version"],
            ["resume_version.resume_id", "resume_version.version"], ondelete="RESTRICT",
        ),
        CheckConstraint("revision >= 1", name="ck_project_revision"),
        Index("ix_project_listing", "resume_id", "deleted_at", "created_at", "id"),
        table_options,
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    resume_id: Mapped[str] = mapped_column(String(36))
    source_version: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(content_text)
    role: Mapped[str] = mapped_column(String(500))
    duration: Mapped[str] = mapped_column(String(100))
    tech_stack: Mapped[list[str]] = mapped_column(JSON)
    user_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    creation_key: Mapped[str | None] = mapped_column(String(36), unique=True)
    creation_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __mapper_args__ = {"version_id_col": revision}


class ProjectFact(Base):
    __tablename__ = "project_fact"
    __table_args__ = (
        CheckConstraint("source_kind IN ('resume', 'user_edit')", name="ck_project_fact_source"),
        Index("ix_project_fact_listing", "project_id", "deleted_at", "position"),
        table_options,
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("project.id", ondelete="RESTRICT")
    )
    position: Mapped[int] = mapped_column(Integer)
    fact_type: Mapped[str] = mapped_column(String(50))
    content: Mapped[str] = mapped_column(content_text)
    source_kind: Mapped[str] = mapped_column(String(16))
    evidence_quote: Mapped[str] = mapped_column(content_text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
