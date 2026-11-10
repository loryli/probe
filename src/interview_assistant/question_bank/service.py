import hashlib
import json
import unicodedata
from uuid import UUID

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from interview_assistant.database import utc_now
from interview_assistant.question_bank.models import Question, QuestionCategory, QuestionVersion
from interview_assistant.question_bank.schemas import (
    CategoryAssignment,
    CategoryCreate,
    CategoryList,
    CategoryRead,
    CategoryRename,
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


class CategoryProblem(QuestionBankError):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


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
        # Keep old creation-key fingerprints valid for uncategorized questions.
        fingerprint_data = payload.model_dump(mode="json")
        if payload.category_id is None:
            fingerprint_data.pop("category_id")
        encoded = json.dumps(
            fingerprint_data, sort_keys=True, ensure_ascii=False
        ).encode("utf-8")
        fingerprint = hashlib.sha256(encoded).hexdigest()
        try:
            async with self.session.begin():
                if key is not None:
                    existing = await self._creation_replay(key, fingerprint)
                    if existing is not None:
                        return QuestionRead.model_validate(existing), False
                await CategoryService(self.session).require(payload.category_id)
                now = utc_now()
                question = Question(
                    **payload.model_dump(mode="json"),
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

    async def list_questions(
        self, *, is_active: bool | None, search: str | None, limit: int, offset: int,
        category_id: str | None = None, uncategorized: bool = False,
    ) -> QuestionList:
        conditions: list[ColumnElement[bool]] = [Question.deleted_at.is_(None)]
        if category_id is not None and uncategorized:
            raise CategoryProblem("invalid_category_filter", "分类与未分类筛选不能同时使用", 422)
        if category_id is not None:
            conditions.append(Question.category_id == category_id)
        elif uncategorized:
            conditions.append(Question.category_id.is_(None))
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
                changes = payload.model_dump(
                    mode="json", exclude_unset=True, exclude={"expected_revision"}
                )
                if "category_id" in changes:
                    await CategoryService(self.session).require(changes["category_id"])
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


class CategoryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def name_key(name: str) -> str:
        normalized = unicodedata.normalize("NFKC", name).casefold()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    async def require(self, category_id: object) -> QuestionCategory | None:
        if category_id is None:
            return None
        row = await self.session.get(QuestionCategory, str(category_id))
        if row is None:
            raise CategoryProblem("category_not_found", "分类不存在，请刷新分类列表后重试", 404)
        return row

    async def list_categories(self) -> CategoryList:
        groups = await self.session.execute(
            select(Question.category_id, func.count())
            .where(Question.deleted_at.is_(None))
            .group_by(Question.category_id)
        )
        counts = {category_id: count for category_id, count in groups}
        rows = await self.session.scalars(
            select(QuestionCategory).order_by(QuestionCategory.created_at, QuestionCategory.id)
        )
        return CategoryList(
            items=[CategoryRead(
                id=UUID(row.id), name=row.name, revision=row.revision,
                question_count=counts.get(row.id, 0),
            ) for row in rows],
            total_questions=sum(counts.values()), uncategorized_count=counts.get(None, 0),
        )

    async def create(self, payload: CategoryCreate) -> tuple[CategoryRead, bool]:
        try:
            async with self.session.begin():
                existing = await self.session.get(QuestionCategory, str(payload.request_id))
                if existing is not None:
                    if existing.name != payload.name:
                        raise CategoryProblem("category_request_conflict", "请求标识已用于其他分类")
                    return CategoryRead.model_validate(existing), False
                row = QuestionCategory(
                    id=str(payload.request_id), name=payload.name,
                    name_key=self.name_key(payload.name),
                )
                self.session.add(row)
                await self.session.flush()
                result = CategoryRead.model_validate(row)
            return result, True
        except IntegrityError:
            async with self.session.begin():
                existing = await self.session.get(QuestionCategory, str(payload.request_id))
                if existing is not None and existing.name == payload.name:
                    return CategoryRead.model_validate(existing), False
            raise CategoryProblem("category_name_conflict", "已有同名分类，请使用其他名称") from None

    async def rename(self, category_id: str, payload: CategoryRename) -> CategoryRead:
        try:
            async with self.session.begin():
                row = await self.require(category_id)
                assert row is not None
                if row.revision != payload.expected_revision:
                    raise CategoryProblem("category_revision_conflict", "分类已被修改，请刷新后重试")
                row.name = payload.name
                row.name_key = self.name_key(payload.name)
                row.updated_at = utc_now()
                await self.session.flush()
                result = CategoryRead.model_validate(row)
            return result
        except IntegrityError:
            raise CategoryProblem("category_name_conflict", "已有同名分类，请使用其他名称") from None
        except StaleDataError:
            raise CategoryProblem("category_revision_conflict", "分类已被修改，请刷新后重试") from None

    async def assign(self, payload: CategoryAssignment) -> list[QuestionRead]:
        try:
            async with self.session.begin():
                await self.require(payload.category_id)
                result: list[Question] = []
                for item in sorted(payload.questions, key=lambda item: str(item.id)):
                    row = await self.session.scalar(
                        select(Question).where(Question.id == str(item.id), Question.deleted_at.is_(None))
                        .with_for_update()
                    )
                    if row is None:
                        raise QuestionNotFound
                    if row.revision != item.expected_revision:
                        raise RevisionConflict
                    category_id = str(payload.category_id) if payload.category_id is not None else None
                    if row.category_id != category_id:
                        row.category_id = category_id
                        row.updated_at = utc_now()
                    result.append(row)
                await self.session.flush()
                reads = [QuestionRead.model_validate(row) for row in result]
            return reads
        except StaleDataError:
            raise RevisionConflict from None
