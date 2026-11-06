"""显式执行迁移；连接配置复用应用 Settings，不在配置文件中保存密钥。"""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import SQLAlchemyError

from interview_assistant.config import Settings
from interview_assistant.database import Database
from interview_assistant.materials.models import Resume
from interview_assistant.question_bank.models import Question  # noqa: F401 — 注册题库表

config = context.config
target_metadata = Resume.metadata


def run_migrations(connection: Connection) -> None:
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
