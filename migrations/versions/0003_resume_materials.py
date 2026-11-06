"""PDF 原件、简历版本、项目与人工事实。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0003_resume_materials"
down_revision = "0002_markdown_import"
branch_labels = None
depends_on = None


def timestamp(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
                     nullable=nullable)


def longtext(name: str) -> sa.Column:
    return sa.Column(name, sa.Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False)


def upgrade() -> None:
    options = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
    op.create_table(
        "resume_document",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("original_bytes", sa.LargeBinary().with_variant(mysql.LONGBLOB(), "mysql"), nullable=False),
        longtext("extracted_text"),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("warnings_json", sa.JSON(), nullable=False), timestamp("created_at"),
        **options,
    )
    op.create_table(
        "resume",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False), longtext("source_text"),
        sa.Column("source_file_name", sa.String(255), nullable=False),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("resume_document.id", ondelete="RESTRICT")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("content_version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("creation_key", sa.String(36), unique=True),
        sa.Column("creation_hash", sa.String(64)),
        timestamp("created_at"), timestamp("updated_at"), timestamp("deleted_at", True),
        sa.CheckConstraint("content_version >= 1", name="ck_resume_content_version"),
        sa.CheckConstraint("revision >= 1", name="ck_resume_revision"), **options,
    )
    op.create_index("ix_resume_listing", "resume", ["deleted_at", "created_at", "id"])
    op.create_table(
        "resume_version",
        sa.Column("resume_id", sa.String(36), sa.ForeignKey("resume.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False), longtext("source_text"),
        sa.Column("source_file_name", sa.String(255), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("resume_document.id", ondelete="RESTRICT")),
        timestamp("created_at"),
        sa.CheckConstraint("version >= 1", name="ck_resume_version_positive"), **options,
    )
    op.create_table(
        "project",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("resume_id", sa.String(36), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False), longtext("summary"),
        sa.Column("role", sa.String(500), nullable=False),
        sa.Column("duration", sa.String(100), nullable=False),
        sa.Column("tech_stack", sa.JSON(), nullable=False),
        sa.Column("user_verified", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("creation_key", sa.String(36), unique=True),
        sa.Column("creation_hash", sa.String(64)),
        timestamp("created_at"), timestamp("updated_at"), timestamp("deleted_at", True),
        sa.ForeignKeyConstraint(["resume_id", "source_version"],
                                ["resume_version.resume_id", "resume_version.version"], ondelete="RESTRICT"),
        sa.CheckConstraint("revision >= 1", name="ck_project_revision"), **options,
    )
    op.create_index("ix_project_listing", "project", ["resume_id", "deleted_at", "created_at", "id"])
    op.create_table(
        "project_fact",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("fact_type", sa.String(50), nullable=False), longtext("content"),
        sa.Column("source_kind", sa.String(16), nullable=False), longtext("evidence_quote"),
        timestamp("created_at"), timestamp("deleted_at", True),
        sa.CheckConstraint("source_kind IN ('resume', 'user_edit')", name="ck_project_fact_source"),
        **options,
    )
    op.create_index("ix_project_fact_listing", "project_fact", ["project_id", "deleted_at", "position"])


def downgrade() -> None:
    op.drop_table("project_fact")
    op.drop_table("project")
    op.drop_table("resume_version")
    op.drop_table("resume")
    op.drop_table("resume_document")
