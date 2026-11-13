"""纯题库计划不再强制关联简历；保留已有计划与场次。"""

import sqlalchemy as sa
from alembic import op

revision = "0011_optional_plan_resume"
down_revision = "0010_question_categories"
branch_labels = None
depends_on = None


def _require_rebuild_connection() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite" and not op.get_context().as_sql:
        foreign_keys = bind.exec_driver_sql("PRAGMA foreign_keys").scalar()
        referenced = bind.exec_driver_sql("SELECT COUNT(*) FROM interview_session").scalar()
        if foreign_keys and referenced:
            raise RuntimeError("SQLite 已有面试场次，请通过项目启动迁移或独立执行 alembic，不在外部事务内重建计划表")


def upgrade() -> None:
    _require_rebuild_connection()
    with op.batch_alter_table("interview_plan") as batch:
        batch.alter_column("resume_id", existing_type=sa.String(36), nullable=True)
        batch.alter_column("source_version", existing_type=sa.Integer(), nullable=True)
        batch.create_check_constraint(
            "ck_interview_plan_resume_pair",
            "(resume_id IS NULL AND source_version IS NULL) OR "
            "(resume_id IS NOT NULL AND source_version IS NOT NULL)",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not op.get_context().as_sql and bind.execute(sa.text(
        "SELECT COUNT(*) FROM interview_plan WHERE resume_id IS NULL"
    )).scalar():
        raise RuntimeError("已有纯题库计划，不能降级为强制关联简历；不会删除这些记录")
    _require_rebuild_connection()
    with op.batch_alter_table("interview_plan") as batch:
        batch.drop_constraint("ck_interview_plan_resume_pair", type_="check")
        batch.alter_column("resume_id", existing_type=sa.String(36), nullable=False)
        batch.alter_column("source_version", existing_type=sa.Integer(), nullable=False)
