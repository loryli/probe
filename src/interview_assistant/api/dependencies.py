"""Request dependencies resolve resources owned by the current application."""

import logging
from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from interview_assistant.config import Settings
from interview_assistant.database import Database

logger = logging.getLogger(__name__)


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_database(request: Request) -> Database | None:
    return cast(Database | None, request.app.state.database)


SettingsDependency = Annotated[Settings, Depends(get_settings)]
DatabaseDependency = Annotated[Database | None, Depends(get_database)]


async def get_session(database: DatabaseDependency) -> AsyncIterator[AsyncSession]:
    if database is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "database_not_configured", "message": "数据库尚未配置"},
        )
    # Services explicitly own their transaction/commit boundary.
    try:
        async with database.sessions() as session:
            yield session
    except SQLAlchemyError as error:
        logger.warning("Database operation failed; error_type=%s", type(error).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "database_unavailable",
                "message": "数据库暂不可用，请检查连接及迁移状态",
            },
        ) from None


SessionDependency = Annotated[AsyncSession, Depends(get_session)]
