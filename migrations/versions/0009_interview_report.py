"""整场复盘：每场面试结束后最多一份报告，失败可重试覆盖。

复盘（Offline Path）在场次结束后触发，输入为全部问答与各轮判定，
输出逐目标评价、弱项主题与练习建议。表按 session_id 唯一，
request_key 唯一用于幂等重放：重放同一标识只读取已生成的报告。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0009_interview_report"
down_revision = "0008_interview_outline"
branch_labels = None
depends_on = None

timestamp = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.create_table(
        "interview_report",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("report_json", sa.JSON(), nullable=True),
        sa.Column("stats_json", sa.JSON(), nullable=False),
        sa.Column("request_key", sa.String(36), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.String(500)),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["interview_session.id"], ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("session_id", name="uq_interview_report_session"),
        sa.UniqueConstraint("request_key", name="uq_interview_report_request"),
        sa.CheckConstraint(
            "status IN ('ready', 'failed')", name="ck_interview_report_status",
        ),
        sa.CheckConstraint(
            "source IN ('model', 'rule')", name="ck_interview_report_source",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_interview_report_session", "interview_report", ["session_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_interview_report_session", table_name="interview_report")
    op.drop_table("interview_report")
