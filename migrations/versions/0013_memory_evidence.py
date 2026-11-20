"""需要人工确认的结构化记忆证据候选。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0013_memory_evidence"
down_revision = "0012_turn_analysis"
branch_labels = None
depends_on = None

timestamp = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.create_table(
        "memory_evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_observation_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("turn_id", sa.String(36), nullable=False),
        sa.Column("topic_key", sa.String(80), nullable=False),
        sa.Column("observation_type", sa.String(32), nullable=False),
        sa.Column("statement", sa.String(500), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("observed_at", timestamp, nullable=False),
        sa.Column("decided_at", timestamp),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.ForeignKeyConstraint(
            ["source_observation_id"], ["session_observation.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["interview_session.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["interview_turn.id"], ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "source_observation_id", name="uq_memory_evidence_observation",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected')",
            name="ck_memory_evidence_status",
        ),
        sa.CheckConstraint(
            "direction IN ('supports', 'contradicts', 'unknown')",
            name="ck_memory_evidence_direction",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_memory_evidence_revision"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_memory_evidence_session", "memory_evidence", ["session_id", "created_at"],
    )
    op.create_index(
        "ix_memory_evidence_status", "memory_evidence", ["status", "topic_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_evidence_status", table_name="memory_evidence")
    op.drop_index("ix_memory_evidence_session", table_name="memory_evidence")
    op.drop_table("memory_evidence")
