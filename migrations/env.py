"""显式执行迁移；连接配置复用应用 Settings，不在配置文件中保存密钥。"""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import SQLAlchemyError

from interview_assistant.config import Settings
from interview_assistant.database import Database
from interview_assistant.interview.models import (  # noqa: F401 — 注册面试场次表
    InterviewSession,
    InterviewTurn,
)
from interview_assistant.materials.models import Resume
from interview_assistant.planner.models import InterviewPlan  # noqa: F401 — 注册面试计划表
from interview_assistant.question_bank.models import Question  # noqa: F401 — 注册题库表

config = context.config
target_metadata = Resume.metadata


def run_migrations(connection: Connection) -> None:
    if connection.dialect.name == "sqlite" and not connection.in_transaction():
        # Alembic batch 重建父表时临时关闭当前迁移连接的外键；提交前检查完整性，
        # 普通应用连接仍始终启用外键。不改动传入的外部事务的提交边界。
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        try:
            with connection.begin():
                context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
                with context.begin_transaction():
                    context.run_migrations()
                if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
                    raise RuntimeError("迁移后发现外键引用异常，已回滚，请检查数据库")
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
        return
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    settings = Settings()
    database_url = settings.resolved_database_url
    if database_url is None:
        raise SystemExit("请先在 .env 或环境变量中设置 INTERVIEW_DATABASE_URL。")
    database = Database(database_url)
    try:
        async with database.engine.connect() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await database.close()


if context.is_offline_mode():
    dialect = context.get_x_argument(as_dictionary=True).get("dialect")
    if dialect is None:
        database_url = Settings().resolved_database_url
        dialect = make_url(database_url).get_backend_name() if database_url else "mysql"
    if dialect not in {"mysql", "sqlite"}:
        raise SystemExit("离线 SQL 方言仅支持 mysql 或 sqlite。")
    context.configure(dialect_name=dialect, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
elif isinstance(config.attributes.get("connection"), Connection):
    # Used by offline tests against a temporary SQLite file, without touching MySQL.
    run_migrations(config.attributes["connection"])
else:
    try:
        asyncio.run(run_online())
    except SQLAlchemyError as error:
        raise SystemExit(
            f"数据库迁移失败（{type(error).__name__}），请检查连接和数据库状态。"
        ) from None
