"""面试准备计划：固定资料版本、考察目标、预算与确认状态。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0006_interview_plan"
down_revision = "0005_resume_parse_draft"
branch_labels = None
depends_on = None

timestamp = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.create_table(
        "interview_plan",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("resume_id", sa.String(36), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("model", sa.String(150), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("inputs_json", sa.JSON(), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.String(500)),
        sa.Column("elapsed_ms", sa.Integer()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.Column("confirmed_at", timestamp),
        sa.Column("expires_at", timestamp, nullable=False),
        sa.Column("deleted_at", timestamp),
        sa.ForeignKeyConstraint(
            ["resume_id", "source_version"],
            ["resume_version.resume_id", "resume_version.version"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'ready', 'confirmed', 'failed')",
            name="ck_interview_plan_status",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_interview_plan_revision"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_interview_plan_listing", "interview_plan", ["deleted_at", "created_at", "id"]
    )


def downgrade() -> None:
    op.drop_table("interview_plan")
