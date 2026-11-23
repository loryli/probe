"""短事务认领执行权，模型调用期间不占用事务。"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from interview_assistant.database import utc_now
from interview_assistant.interview.errors import InterviewError
from interview_assistant.interview.models import InterviewExecution, InterviewSession

_owners: ContextVar[tuple[tuple[str, str, str], ...]] = ContextVar("interview_execution_owners", default=())


async def check_execution_owner(session: AsyncSession) -> None:
    """在业务写事务中校验租约并锁住该行，拒绝失效任务回写。"""
    for session_id, operation, token in _owners.get():
        result = await session.execute(update(InterviewExecution).where(
            InterviewExecution.session_id == session_id,
            InterviewExecution.operation == operation,
            InterviewExecution.owner_token == token,
            InterviewExecution.expires_at > utc_now(),
        ).values(owner_token=token))
        if result.rowcount != 1:
            raise InterviewError("execution_expired", "生成任务已失效，请读取最新场次状态", 409)


@asynccontextmanager
async def execution_lease(
    session: AsyncSession, session_id: str, operation: str, timeout: float,
) -> AsyncIterator[bool]:
    """失败认领只读取现状；崩溃遗留租约到期后才允许显式重试。"""
    token = str(uuid4())
    scope = (
        InterviewExecution.session_id == session_id,
        InterviewExecution.operation == operation,
    )
    try:
        async with session.begin():
            await session.execute(delete(InterviewExecution).where(
                *scope, InterviewExecution.expires_at <= utc_now(),
            ))
            session.add(InterviewExecution(
                session_id=session_id, operation=operation, owner_token=token,
                expires_at=utc_now() + timedelta(seconds=timeout + 30),
            ))
            await session.flush()
    except IntegrityError:
        async with session.begin():
            exists = await session.scalar(select(InterviewSession.id).where(
                InterviewSession.id == session_id,
            ))
            if exists is None:
                raise InterviewError("session_not_found", "场次不存在或已被删除", 404) from None
        yield False
        return
    context_token = _owners.set((*_owners.get(), (session_id, operation, token)))
    try:
        # 先取消本地调用，再允许接管，避免租约到期而旧任务仍继续落库。
        async with asyncio.timeout(timeout):
            yield True
    except TimeoutError:
        raise InterviewError(
            "execution_timeout", "本次生成超时，已保存内容保留，可手动重试", 504,
        ) from None
    finally:
        _owners.reset(context_token)
        async with session.begin():
            await session.execute(delete(InterviewExecution).where(
                *scope, InterviewExecution.owner_token == token,
            ))
