"""Store voice transcription requests without storing the audio."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0014_interview_transcription"
down_revision = "0013_memory_evidence"
branch_labels = None
depends_on = None

timestamp = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.create_table(
        "interview_transcription",
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("turn_id", sa.String(36), nullable=False),
        sa.Column("audio_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("transcript", sa.Text()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("expires_at", timestamp, nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["interview_session.id"], ondelete="CASCADE"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_interview_transcription_session_id", "interview_transcription", ["session_id"],
    )
    op.create_index(
        "ix_interview_transcription_expires", "interview_transcription", ["expires_at"],
    )


def downgrade() -> None:
    op.drop_table("interview_transcription")
