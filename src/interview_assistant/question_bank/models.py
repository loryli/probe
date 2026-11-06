from datetime import datetime
from uuid import uuid4

from pydantic import JsonValue
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import LONGTEXT, VARCHAR
from sqlalchemy.orm import Mapped, mapped_column

from interview_assistant.database import Base, UTCDateTime, utc_now

content_text = Text().with_variant(LONGTEXT(), "mysql")
creation_key_type = String(128).with_variant(VARCHAR(128, collation="utf8mb4_bin"), "mysql")
table_options = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


class Question(Base):
    __tablename__ = "question"
    __table_args__ = (
        CheckConstraint("content_version >= 1", name="ck_question_content_version"),
        CheckConstraint("revision >= 1", name="ck_question_revision"),
        Index("ix_question_listing", "deleted_at", "is_active", "created_at", "id"),
        table_options,
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    question_text: Mapped[str] = mapped_column(content_text)
    standard_answer_text: Mapped[str] = mapped_column(content_text)
    user_notes: Mapped[str] = mapped_column(content_text)
    source: Mapped[str] = mapped_column(String(32), default="manual", server_default="manual")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    content_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    creation_key: Mapped[str | None] = mapped_column(creation_key_type, unique=True)
    creation_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __mapper_args__ = {"version_id_col": revision}


class QuestionVersion(Base):
    __tablename__ = "question_version"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_question_version_positive"),
        table_options,
    )

    question_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("question.id", ondelete="RESTRICT"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_text: Mapped[str] = mapped_column(content_text)
    standard_answer_text: Mapped[str] = mapped_column(content_text)
    user_notes: Mapped[str] = mapped_column(content_text)
    source: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class QuestionMetadata(Base):
    """No metadata write API yet; every future result references an exact content version."""

    __tablename__ = "question_metadata"
    __table_args__ = (
        ForeignKeyConstraint(
            ["question_id", "content_version"],
            ["question_version.question_id", "question_version.version"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("origin IN ('ai', 'user')", name="ck_question_metadata_origin"),
        CheckConstraint(
            "status IN ('draft', 'accepted', 'overridden')", name="ck_question_metadata_status"
        ),
        Index("ix_question_metadata_version", "question_id", "content_version"),
        table_options,
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    question_id: Mapped[str] = mapped_column(String(36))
    content_version: Mapped[int] = mapped_column(Integer)
    metadata_json: Mapped[dict[str, JsonValue]] = mapped_column(JSON)
    origin: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), default="draft", server_default="draft")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class MarkdownImport(Base):
    __tablename__ = "markdown_import"
    __table_args__ = (
        CheckConstraint("status IN ('draft', 'confirmed')", name="ck_markdown_import_status"),
        CheckConstraint("revision >= 1", name="ck_markdown_import_revision"),
        table_options,
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    file_name: Mapped[str] = mapped_column(String(255))
    content_sha256: Mapped[str] = mapped_column(String(64))
    original_text: Mapped[str] = mapped_column(content_text)
    requested_strategy: Mapped[str] = mapped_column(String(16))
    resolved_strategy: Mapped[str] = mapped_column(String(16))
    parser_version: Mapped[str] = mapped_column(String(32))
    candidates_json: Mapped[list[dict[str, JsonValue]]] = mapped_column(JSON)
    warnings_json: Mapped[list[str]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    selection_hash: Mapped[str | None] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    __mapper_args__ = {"version_id_col": revision}


class QuestionImportSource(Base):
    __tablename__ = "question_import_source"
    __table_args__ = (
        UniqueConstraint("import_id", "candidate_index", name="uq_import_source_candidate"),
        CheckConstraint("candidate_index >= 0", name="ck_import_source_candidate"),
        table_options,
    )

    question_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("question.id", ondelete="RESTRICT"), primary_key=True
    )
    import_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("markdown_import.id", ondelete="RESTRICT")
    )
    candidate_index: Mapped[int] = mapped_column(Integer)
