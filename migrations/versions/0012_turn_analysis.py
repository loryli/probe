"""逐轮异步分析任务与带证据的本场观察。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0012_turn_analysis"
down_revision = "0011_optional_plan_resume"
branch_labels = None
depends_on = None

timestamp = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.create_table(
        "interview_analysis_job",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("turn_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("input_version", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(150), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("result_json", sa.JSON()),
        sa.Column("elapsed_ms", sa.Integer()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.String(500)),
        sa.Column("attempt_expires_at", timestamp),
        sa.Column("completed_at", timestamp),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["interview_session.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["interview_turn.id"], ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "kind", "turn_id", "input_version",
            name="uq_interview_analysis_job_input",
        ),
        sa.CheckConstraint(
            "kind IN ('background')", name="ck_interview_analysis_job_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'ready', 'failed')",
            name="ck_interview_analysis_job_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_interview_analysis_job_attempts",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_interview_analysis_job_session",
        "interview_analysis_job",
        ["session_id", "created_at"],
    )

    op.create_table(
        "session_observation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("turn_id", sa.String(36), nullable=False),
        sa.Column("objective_id", sa.String(16)),
        sa.Column("topic", sa.String(80), nullable=False),
        sa.Column("observation_type", sa.String(32), nullable=False),
        sa.Column("statement", sa.String(500), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"], ["interview_analysis_job.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["interview_session.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["interview_turn.id"], ondelete="RESTRICT",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_session_observation_session",
        "session_observation",
        ["session_id", "created_at"],
    )
    op.create_index(
        "ix_session_observation_turn",
        "session_observation",
        ["turn_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_session_observation_turn", table_name="session_observation")
    op.drop_index("ix_session_observation_session", table_name="session_observation")
    op.drop_table("session_observation")
    op.drop_index("ix_interview_analysis_job_session", table_name="interview_analysis_job")
    op.drop_table("interview_analysis_job")
