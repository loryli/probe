"""Personal question categories; existing questions remain uncategorized."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0010_question_categories"
down_revision = "0009_interview_report"
branch_labels = None
depends_on = None

timestamp = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.create_table(
        "question_category",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("name_key", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.UniqueConstraint("name_key", name="uq_question_category_name_key"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4",
    )
    if op.get_bind().dialect.name == "sqlite":
        # SQLite permits an inline reference when the added column defaults to
        # NULL. Avoid rebuilding question, which is referenced by history tables.
        op.execute(
            "ALTER TABLE question ADD COLUMN category_id VARCHAR(36) "
            "CONSTRAINT fk_question_category_id_question_category "
            "REFERENCES question_category(id) ON DELETE RESTRICT"
        )
    else:
        op.add_column("question", sa.Column("category_id", sa.String(36), nullable=True))
        op.create_foreign_key(
            "fk_question_category_id_question_category", "question", "question_category",
            ["category_id"], ["id"], ondelete="RESTRICT",
        )
    op.create_index("ix_question_category_id", "question", ["category_id"])


def downgrade() -> None:
    op.drop_index("ix_question_category_id", table_name="question")
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("fk_question_category_id_question_category", "question", type_="foreignkey")
    op.drop_column("question", "category_id")
    op.drop_table("question_category")
