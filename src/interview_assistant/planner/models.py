from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from interview_assistant.database import Base, UTCDateTime, utc_now

table_options = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


class InterviewPlan(Base):
    """一次规划请求对应一条记录；确认后固定，重新生成创建新记录。"""

    __tablename__ = "interview_plan"
    __table_args__ = (
        ForeignKeyConstraint(
            ["resume_id", "source_version"],
            ["resume_version.resume_id", "resume_version.version"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('running', 'ready', 'confirmed', 'failed')",
            name="ck_interview_plan_status",
        ),
        CheckConstraint("revision >= 1", name="ck_interview_plan_revision"),
        Index("ix_interview_plan_listing", "deleted_at", "created_at", "id"),
        table_options,
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    resume_id: Mapped[str] = mapped_column(String(36))
    source_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    provider: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(150), default="")
    prompt_version: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(64))
    inputs_json: Mapped[dict[str, JsonValue]] = mapped_column(JSON, default=dict)
    snapshot_json: Mapped[dict[str, JsonValue]] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict[str, JsonValue]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(500))
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __mapper_args__ = {"version_id_col": revision}
