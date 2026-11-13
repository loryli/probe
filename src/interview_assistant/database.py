"""数据库连接生命周期与项目独立的 SQLite 开发模式。"""

from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import DateTime, MetaData, event, text
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.engine import Connection, Dialect, make_url
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import ConnectionPoolEntry
from sqlalchemy.types import TypeDecorator


def utc_now() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """MySQL stores naive UTC; API values always carry the UTC timezone."""

    impl = DateTime().with_variant(DATETIME(fsp=6), "mysql")
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Timestamp must include a timezone")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class Database:
    def __init__(self, url: str) -> None:
        parsed_url = make_url(url)
        self.is_sqlite = parsed_url.get_backend_name() == "sqlite"
        if self.is_sqlite and parsed_url.database and parsed_url.database != ":memory:":
            database_path = Path(parsed_url.database).expanduser().resolve()
            database_path.parent.mkdir(parents=True, exist_ok=True)
            parsed_url = parsed_url.set(database=database_path.as_posix())
        # Engine construction opens no network connection, including explicit MySQL URLs.
        self.engine = create_async_engine(
            parsed_url,
            pool_pre_ping=True,
            pool_recycle=1800,
            hide_parameters=True,
            connect_args={"timeout": 5} if self.is_sqlite else {"connect_timeout": 5},
        )
        if self.is_sqlite:
            event.listen(self.engine.sync_engine, "connect", self._configure_sqlite)
        self.sessions = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    @staticmethod
    def _configure_sqlite(connection: DBAPIConnection, record: ConnectionPoolEntry) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async def initialize_local(self) -> None:
        """仅允许 SQLite 开发库通过已有迁移初始化，不对 MySQL 自动执行。"""
        if not self.is_sqlite:
            raise ValueError("Automatic local initialization only supports SQLite")

        def upgrade(connection: Connection) -> None:
            migration_config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
            migration_config.attributes["connection"] = connection
            command.upgrade(migration_config, "head")

        # 迁移环境自行管理事务；SQLite 重建被场次引用的计划表前需在事务外设置外键策略。
        async with self.engine.connect() as connection:
            await connection.run_sync(upgrade)

    async def ping(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def close(self) -> None:
        await self.engine.dispose()
