"""Qwen 简历提取草稿、调用记录与逐项目确认映射。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0005_resume_parse_draft"
down_revision = "0004_pdf_ocr"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "resume_parse_draft",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("resume_id", sa.String(36), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(150), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("confirmed_projects_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.String(500)),
        sa.Column("elapsed_ms", sa.Integer()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("created_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        sa.Column("expires_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        sa.ForeignKeyConstraint(["resume_id", "source_version"],
                                ["resume_version.resume_id", "resume_version.version"], ondelete="RESTRICT"),
        sa.CheckConstraint("status IN ('running', 'ready', 'confirmed', 'failed')", name="ck_resume_parse_status"),
        sa.CheckConstraint("revision >= 1", name="ck_resume_parse_revision"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4",
    )
    op.create_index("ix_resume_parse_listing", "resume_parse_draft", ["resume_id", "created_at", "id"])


def downgrade() -> None:
    op.drop_table("resume_parse_draft")
