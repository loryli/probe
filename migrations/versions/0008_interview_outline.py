"""面试场次增加基础问题骨架（outline_json）。

骨架是创建场次时一次批量生成的「每目标最多 2 个基础问题」，
之后取题优先读它，缺失才回退到即时生成。旧场次该列为 NULL。
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_interview_outline"
down_revision = "0007_interview_session"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "interview_session",
        sa.Column("outline_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("interview_session") as batch:
        batch.drop_column("outline_json")
