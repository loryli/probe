"""Markdown 草稿、不可变来源文件和题目来源引用。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0002_markdown_import"
down_revision = "0001_question_bank"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "markdown_import",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column(
            "original_text", sa.Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False
        ),
        sa.Column("requested_strategy", sa.String(16), nullable=False),
        sa.Column("resolved_strategy", sa.String(16), nullable=False),
        sa.Column("parser_version", sa.String(32), nullable=False),
        sa.Column("candidates_json", sa.JSON(), nullable=False),
        sa.Column("warnings_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("selection_hash", sa.String(64), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_markdown_import"),
        sa.UniqueConstraint("fingerprint", name="uq_markdown_import_fingerprint"),
        sa.CheckConstraint("status IN ('draft', 'confirmed')", name="ck_markdown_import_status"),
        sa.CheckConstraint("revision >= 1", name="ck_markdown_import_revision"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "question_import_source",
        sa.Column("question_id", sa.String(36), nullable=False),
        sa.Column("import_id", sa.String(36), nullable=False),
        sa.Column("candidate_index", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("question_id", name="pk_question_import_source"),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["question.id"],
            name="fk_question_import_source_question_id_question",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["import_id"],
            ["markdown_import.id"],
            name="fk_question_import_source_import_id_markdown_import",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("import_id", "candidate_index", name="uq_import_source_candidate"),
        sa.CheckConstraint("candidate_index >= 0", name="ck_import_source_candidate"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )


def downgrade() -> None:
    op.drop_table("question_import_source")
    op.drop_table("markdown_import")
