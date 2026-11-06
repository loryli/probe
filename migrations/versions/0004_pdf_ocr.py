"""按 PDF 原件、识别方式与提取版本保存独立结果，不覆盖旧简历文字。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0004_pdf_ocr"
down_revision = "0003_resume_materials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "resume_extraction",
        sa.Column("document_id", sa.String(36), nullable=False),
        sa.Column("extraction_key", sa.String(100), nullable=False),
        sa.Column("extracted_text", sa.Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False),
        sa.Column("warnings_json", sa.JSON(), nullable=False),
        sa.Column("ocr_pages_json", sa.JSON(), nullable=False),
        sa.Column("page_methods_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        sa.PrimaryKeyConstraint("document_id", "extraction_key", name="pk_resume_extraction"),
        sa.ForeignKeyConstraint(["document_id"], ["resume_document.id"], ondelete="RESTRICT",
                                name="fk_resume_extraction_document_id_resume_document"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4",
    )


def downgrade() -> None:
    op.drop_table("resume_extraction")
