"""题库、不可变内容版本和独立元数据。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0001_question_bank"
down_revision = None
branch_labels = None
depends_on = None

long_text = sa.Text().with_variant(mysql.LONGTEXT(), "mysql")
timestamp = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.create_table(
        "question",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("question_text", long_text, nullable=False),
        sa.Column("standard_answer_text", long_text, nullable=False),
        sa.Column("user_notes", long_text, nullable=False),
        sa.Column("source", sa.String(32), server_default="manual", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("content_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "creation_key",
            sa.String(128).with_variant(mysql.VARCHAR(128, collation="utf8mb4_bin"), "mysql"),
            nullable=True,
        ),
        sa.Column("creation_hash", sa.String(64), nullable=True),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.Column("deleted_at", timestamp, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_question"),
        sa.UniqueConstraint("creation_key", name="uq_question_creation_key"),
        sa.CheckConstraint("content_version >= 1", name="ck_question_content_version"),
        sa.CheckConstraint("revision >= 1", name="ck_question_revision"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_question_listing", "question", ["deleted_at", "is_active", "created_at", "id"]
    )
    op.create_table(
        "question_version",
        sa.Column("question_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("question_text", long_text, nullable=False),
        sa.Column("standard_answer_text", long_text, nullable=False),
        sa.Column("user_notes", long_text, nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.PrimaryKeyConstraint("question_id", "version", name="pk_question_version"),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["question.id"],
            name="fk_question_version_question_id_question",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("version >= 1", name="ck_question_version_positive"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "question_metadata",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("question_id", sa.String(36), nullable=False),
        sa.Column("content_version", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("origin", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), server_default="draft", nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_question_metadata"),
        sa.ForeignKeyConstraint(
            ["question_id", "content_version"],
            ["question_version.question_id", "question_version.version"],
            name="fk_question_metadata_question_id_question_version",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("origin IN ('ai', 'user')", name="ck_question_metadata_origin"),
        sa.CheckConstraint(
            "status IN ('draft', 'accepted', 'overridden')", name="ck_question_metadata_status"
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_question_metadata_version", "question_metadata", ["question_id", "content_version"]
    )


def downgrade() -> None:
    op.drop_table("question_metadata")
    op.drop_table("question_version")
    op.drop_table("question")
