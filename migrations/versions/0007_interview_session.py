"""文字面试场次与轮次：冻结计划快照、逐轮问答与决策状态。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0007_interview_session"
down_revision = "0006_interview_plan"
branch_labels = None
depends_on = None

timestamp = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")
content_text = sa.Text().with_variant(mysql.LONGTEXT(), "mysql")


def upgrade() -> None:
    op.create_table(
        "interview_session",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plan_id", sa.String(36), nullable=False),
        sa.Column("plan_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("model", sa.String(150), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("direction", sa.String(500), nullable=False),
        sa.Column("runtime_json", sa.JSON(), nullable=False),
        sa.Column("progress_json", sa.JSON(), nullable=False),
        sa.Column("current_turn_id", sa.String(36)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.String(500)),
        sa.Column("started_at", timestamp, nullable=False),
        sa.Column("ended_at", timestamp),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["interview_plan.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'completed')",
            name="ck_interview_session_status",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_interview_session_revision"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_interview_session_listing", "interview_session", ["created_at", "id"]
    )

    op.create_table(
        "interview_turn",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(12), nullable=False),
        sa.Column("objective_id", sa.String(16)),
        sa.Column("source", sa.String(16)),
        sa.Column("question_id", sa.String(36)),
        sa.Column("is_followup", sa.Boolean(), nullable=False),
        sa.Column("text", content_text, nullable=False),
        sa.Column("answers_turn_id", sa.String(36)),
        sa.Column("request_key", sa.String(36)),
        sa.Column("decision_json", sa.JSON()),
        sa.Column("elapsed_ms", sa.Integer()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("created_at", timestamp, nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["interview_session.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["answers_turn_id"], ["interview_turn.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("session_id", "sequence", name="uq_interview_turn_sequence"),
        sa.UniqueConstraint("answers_turn_id", name="uq_interview_turn_answer"),
        sa.UniqueConstraint("request_key", name="uq_interview_turn_request"),
        sa.CheckConstraint(
            "role IN ('interviewer', 'candidate')", name="ck_interview_turn_role"
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_interview_turn_sequence"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_interview_turn_session", "interview_turn", ["session_id", "sequence"]
    )


def downgrade() -> None:
    op.drop_index("ix_interview_turn_session", table_name="interview_turn")
    op.drop_table("interview_turn")
    op.drop_index("ix_interview_session_listing", table_name="interview_session")
    op.drop_table("interview_session")
