import hashlib
import json

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from interview_assistant.database import utc_now
from interview_assistant.question_bank.models import Question, QuestionVersion
from interview_assistant.question_bank.schemas import (
    QuestionCreate,
    QuestionHistory,
    QuestionList,
    QuestionPatch,
    QuestionRead,
    QuestionVersionRead,
)


class QuestionBankError(Exception):
    pass


class QuestionNotFound(QuestionBankError):
    pass


class RevisionConflict(QuestionBankError):
    pass


class CreationKeyConflict(QuestionBankError):
    pass


class QuestionService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def require_question(self, question_id: str) -> Question:
        question = await self.session.scalar(
            select(Question).where(Question.id == question_id, Question.deleted_at.is_(None))
        )
        if question is None:
            raise QuestionNotFound
        return question

    async def _creation_replay(self, key: str, fingerprint: str) -> Question | None:
        question = await self.session.scalar(select(Question).where(Question.creation_key == key))
        if question is not None:
            if question.creation_hash != fingerprint or question.deleted_at is not None:
                raise CreationKeyConflict
        return question

    @staticmethod
    def _snapshot(question: Question) -> QuestionVersion:
        return QuestionVersion(
            question_id=question.id,
            version=question.content_version,
            question_text=question.question_text,
            standard_answer_text=question.standard_answer_text,
            user_notes=question.user_notes,
            source=question.source,
            created_at=question.updated_at,
        )

    async def create(self, payload: QuestionCreate, key: str | None) -> tuple[QuestionRead, bool]:
        encoded = json.dumps(payload.model_dump(), sort_keys=True, ensure_ascii=False).encode(
            "utf-8"
        )
        fingerprint = hashlib.sha256(encoded).hexdigest()
        try:
            async with self.session.begin():
                if key is not None:
                    existing = await self._creation_replay(key, fingerprint)
                    if existing is not None:
                        return QuestionRead.model_validate(existing), False
                now = utc_now()
                question = Question(
                    **payload.model_dump(),
                    source="manual",
                    creation_key=key,
                    creation_hash=fingerprint if key is not None else None,
                    created_at=now,
                    updated_at=now,
                )
                self.session.add(question)
                await self.session.flush()
                self.session.add(self._snapshot(question))
                await self.session.flush()
                result = QuestionRead.model_validate(question)
            return result, True
        except IntegrityError:
            # A concurrent request may have committed the same creation key first.
            if key is None:
                raise
            async with self.session.begin():
                existing = await self._creation_replay(key, fingerprint)
                if existing is None:
                    raise
                return QuestionRead.model_validate(existing), False

    async def get(self, question_id: str) -> QuestionRead:
        return QuestionRead.model_validate(await self.require_question(question_id))

    async def list(
        self, *, is_active: bool | None, search: str | None, limit: int, offset: int
    ) -> QuestionList:
        conditions: list[ColumnElement[bool]] = [Question.deleted_at.is_(None)]
        if is_active is not None:
            conditions.append(Question.is_active == is_active)
        if search is not None:
            conditions.append(Question.question_text.contains(search, autoescape=True))
        total = await self.session.scalar(
            select(func.count()).select_from(Question).where(*conditions)
        )
        questions = await self.session.scalars(
            select(Question)
            .where(*conditions)
            .order_by(Question.created_at.desc(), Question.id)
            .limit(limit)
            .offset(offset)
        )
        return QuestionList(
            items=[QuestionRead.model_validate(item) for item in questions],
            total=total or 0,
            limit=limit,
            offset=offset,
        )

    async def patch(self, question_id: str, payload: QuestionPatch) -> QuestionRead:
        try:
            async with self.session.begin():
                question = await self.require_question(question_id)
                if question.revision != payload.expected_revision:
                    raise RevisionConflict
                changes = payload.model_dump(exclude_unset=True, exclude={"expected_revision"})
                changed_fields = {
                    name for name, value in changes.items() if getattr(question, name) != value
                }
                for name in changed_fields:
                    setattr(question, name, changes[name])
                content_changed = bool(
                    changed_fields & {"question_text", "standard_answer_text", "user_notes"}
                )
                if changed_fields:
                    question.updated_at = utc_now()
                    if content_changed:
                        question.content_version += 1
                    await self.session.flush()
                    if content_changed:
                        self.session.add(self._snapshot(question))
                        await self.session.flush()
                result = QuestionRead.model_validate(question)
            return result
        except StaleDataError:
            raise RevisionConflict from None

    async def delete(self, question_id: str, expected_revision: int) -> None:
        try:
            async with self.session.begin():
                question = await self.require_question(question_id)
                if question.revision != expected_revision:
                    raise RevisionConflict
                question.deleted_at = question.updated_at = utc_now()
                question.is_active = False
                await self.session.flush()
        except StaleDataError:
            raise RevisionConflict from None

    async def history(self, question_id: str, *, limit: int, offset: int) -> QuestionHistory:
        question = await self.require_question(question_id)
        versions = await self.session.scalars(
            select(QuestionVersion)
            .where(QuestionVersion.question_id == question_id)
            .order_by(QuestionVersion.version.desc())
            .limit(limit)
            .offset(offset)
        )
        return QuestionHistory(
            items=[QuestionVersionRead.model_validate(item) for item in versions],
            total=question.content_version,
            limit=limit,
            offset=offset,
        )

    async def version(self, question_id: str, version: int) -> QuestionVersionRead:
        await self.require_question(question_id)
        snapshot = await self.session.get(QuestionVersion, (question_id, version))
        if snapshot is None:
            raise QuestionNotFound
        return QuestionVersionRead.model_validate(snapshot)
