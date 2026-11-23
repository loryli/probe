"""为复盘和实时生成增加持久化执行租约。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0015_interview_execution"
down_revision = "0014_interview_transcription"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "interview_execution",
        sa.Column("session_id", sa.String(36), primary_key=True),
        sa.Column("operation", sa.String(24), primary_key=True),
        sa.Column("owner_token", sa.String(36), nullable=False),
        sa.Column("expires_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["interview_session.id"], ondelete="CASCADE"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )


def downgrade() -> None:
    op.drop_table("interview_execution")
