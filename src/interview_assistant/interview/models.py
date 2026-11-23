from datetime import datetime
from uuid import uuid4

from pydantic import JsonValue
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from interview_assistant.database import Base, UTCDateTime, utc_now

content_text = Text().with_variant(LONGTEXT(), "mysql")
table_options = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


class InterviewExecution(Base):
    """模型执行租约；唯一键在 SQLite 和 MySQL 上共同限制并发调用。"""

    __tablename__ = "interview_execution"
    __table_args__ = (table_options,)

    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("interview_session.id", ondelete="CASCADE"), primary_key=True,
    )
    operation: Mapped[str] = mapped_column(String(24), primary_key=True)
    owner_token: Mapped[str] = mapped_column(String(36), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class InterviewSession(Base):
    """一场面试；创建时冻结计划运行时契约，之后不追随计划或资料的修改。"""

    __tablename__ = "interview_session"
    __table_args__ = (
        ForeignKeyConstraint(["plan_id"], ["interview_plan.id"], ondelete="RESTRICT"),
        CheckConstraint(
            "status IN ('active', 'paused', 'completed')",
            name="ck_interview_session_status",
        ),
        CheckConstraint("revision >= 1", name="ck_interview_session_revision"),
        Index("ix_interview_session_listing", "created_at", "id"),
        table_options,
    )

    # 场次 ID 直接复用创建请求的幂等键，重放只读取原场次。
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(36))
    plan_revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="active")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    mode: Mapped[str] = mapped_column(String(8), default="model")
    provider: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(150), default="")
    prompt_version: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(500), default="")
    runtime_json: Mapped[dict[str, JsonValue]] = mapped_column(JSON)
    progress_json: Mapped[dict[str, JsonValue]] = mapped_column(JSON)
    # 基础问题骨架：创建场次时一次批量生成，取题时优先用它，缺失再即时生成。
    outline_json: Mapped[dict[str, JsonValue] | None] = mapped_column(JSON)
    current_turn_id: Mapped[str | None] = mapped_column(String(36))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(500))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

    __mapper_args__ = {"version_id_col": revision}


class InterviewTurn(Base):
    """一问一答各占一条；回答通过 answers_turn_id 指回问题，天然保证一问一答。"""

    __tablename__ = "interview_turn"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_interview_turn_sequence"),
        UniqueConstraint("answers_turn_id", name="uq_interview_turn_answer"),
        UniqueConstraint("request_key", name="uq_interview_turn_request"),
        CheckConstraint("role IN ('interviewer', 'candidate')", name="ck_interview_turn_role"),
        CheckConstraint("sequence >= 1", name="ck_interview_turn_sequence"),
        Index("ix_interview_turn_session", "session_id", "sequence"),
        table_options,
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("interview_session.id", ondelete="RESTRICT")
    )
    sequence: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(12))
    objective_id: Mapped[str | None] = mapped_column(String(16))
    source: Mapped[str | None] = mapped_column(String(16))
    question_id: Mapped[str | None] = mapped_column(String(36))
    is_followup: Mapped[bool] = mapped_column(Boolean, default=False)
    text: Mapped[str] = mapped_column(content_text)
    answers_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("interview_turn.id", ondelete="RESTRICT")
    )
    request_key: Mapped[str | None] = mapped_column(String(36))
    decision_json: Mapped[dict[str, JsonValue] | None] = mapped_column(JSON)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)

class InterviewReport(Base):
    """一场面试的整场复盘；每场最多一份，失败可重试覆盖。"""

    __tablename__ = "interview_report"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_interview_report_session"),
        UniqueConstraint("request_key", name="uq_interview_report_request"),
        CheckConstraint(
            "status IN ('ready', 'failed')", name="ck_interview_report_status",
        ),
        CheckConstraint(
            "source IN ('model', 'rule')", name="ck_interview_report_source",
        ),
        Index("ix_interview_report_session", "session_id"),
        table_options,
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("interview_session.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(8))
    report_json: Mapped[dict[str, JsonValue] | None] = mapped_column(JSON)
    stats_json: Mapped[dict[str, JsonValue]] = mapped_column(JSON)
    request_key: Mapped[str] = mapped_column(String(36))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class InterviewTranscription(Base):
    """语音转写任务与幂等记录；只保存临时文字草稿，从不保存音频。"""

    __tablename__ = "interview_transcription"
    __table_args__ = (Index("ix_interview_transcription_expires", "expires_at"),)

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("interview_session.id", ondelete="CASCADE"), index=True,
    )
    turn_id: Mapped[str] = mapped_column(String(36))
    audio_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    transcript: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


class InterviewAnalysisJob(Base):
    """逐轮后台分析任务；持久状态让失败与进程重启后都能人工续接。"""

    __tablename__ = "interview_analysis_job"
    __table_args__ = (
        UniqueConstraint(
            "kind", "turn_id", "input_version",
            name="uq_interview_analysis_job_input",
        ),
        CheckConstraint("kind IN ('background')", name="ck_interview_analysis_job_kind"),
        CheckConstraint(
            "status IN ('pending', 'running', 'ready', 'failed')",
            name="ck_interview_analysis_job_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_interview_analysis_job_attempts"),
        Index("ix_interview_analysis_job_session", "session_id", "created_at"),
        table_options,
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("interview_session.id", ondelete="RESTRICT")
    )
    turn_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("interview_turn.id", ondelete="RESTRICT")
    )
    kind: Mapped[str] = mapped_column(String(16), default="background")
    status: Mapped[str] = mapped_column(String(8), default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    input_version: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32), default="qwen")
    model: Mapped[str] = mapped_column(String(150), default="")
    prompt_version: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(64))
    result_json: Mapped[dict[str, JsonValue] | None] = mapped_column(JSON)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(500))
    attempt_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class SessionObservation(Base):
    """带回答证据的本场观察；不等同于长期记忆或能力定论。"""

    __tablename__ = "session_observation"
    __table_args__ = (
        Index("ix_session_observation_session", "session_id", "created_at"),
        Index("ix_session_observation_turn", "turn_id"),
        table_options,
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("interview_analysis_job.id", ondelete="RESTRICT")
    )
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("interview_session.id", ondelete="RESTRICT")
    )
    turn_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("interview_turn.id", ondelete="RESTRICT")
    )
    objective_id: Mapped[str | None] = mapped_column(String(16))
    topic: Mapped[str] = mapped_column(String(80))
    observation_type: Mapped[str] = mapped_column(String(32))
    statement: Mapped[str] = mapped_column(String(500))
    confidence: Mapped[float] = mapped_column(Float)
    evidence_json: Mapped[list[dict[str, JsonValue]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class MemoryEvidence(Base):
    """由本场观察派生、必须经用户确认的长期记忆证据候选。"""

    __tablename__ = "memory_evidence"
    __table_args__ = (
        UniqueConstraint(
            "source_observation_id", name="uq_memory_evidence_observation",
        ),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected')",
            name="ck_memory_evidence_status",
        ),
        CheckConstraint(
            "direction IN ('supports', 'contradicts', 'unknown')",
            name="ck_memory_evidence_direction",
        ),
        CheckConstraint("revision >= 1", name="ck_memory_evidence_revision"),
        Index("ix_memory_evidence_session", "session_id", "created_at"),
        Index("ix_memory_evidence_status", "status", "topic_key"),
        table_options,
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    source_observation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("session_observation.id", ondelete="RESTRICT")
    )
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("interview_session.id", ondelete="RESTRICT")
    )
    turn_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("interview_turn.id", ondelete="RESTRICT")
    )
    topic_key: Mapped[str] = mapped_column(String(80))
    observation_type: Mapped[str] = mapped_column(String(32))
    statement: Mapped[str] = mapped_column(String(500))
    direction: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[float] = mapped_column(Float)
    evidence_json: Mapped[list[dict[str, JsonValue]]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
