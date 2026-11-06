"""Persistent import drafts and atomic, replay-safe confirmation."""

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from interview_assistant.database import utc_now
from interview_assistant.question_bank.markdown import (
    MAX_CANDIDATES,
    MAX_FILE_BYTES,
    PARSER_VERSION,
    Candidate,
    ImportStrategy,
    MarkdownLimitError,
    parse_markdown,
)
from interview_assistant.question_bank.models import MarkdownImport, Question, QuestionImportSource
from interview_assistant.question_bank.service import QuestionService


class ImportProblem(Exception):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class ImportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_name: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=MAX_FILE_BYTES)
    strategy: ImportStrategy = "auto"

    @field_validator("file_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.lower().endswith((".md", ".markdown")) or any(
            char in "/\\" or ord(char) < 32 for char in value
        ):
            raise ValueError("请提供 Markdown 文件名，不包含目录或控制字符。")
        return value

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("文件内容须为有效 UTF-8 文本。") from None
        if len(encoded) > MAX_FILE_BYTES or "\x00" in value:
            raise ValueError("请使用不超过 1 MiB、不含空字符的 UTF-8 Markdown 文件。")
        return value


class CandidateRead(Candidate):
    raw_text: str


class ImportPreview(BaseModel):
    id: UUID
    file_name: str
    content_sha256: str
    requested_strategy: ImportStrategy
    resolved_strategy: ImportStrategy
    parser_version: str
    status: Literal["draft", "confirmed"]
    created_at: datetime
    warnings: list[str]
    candidates: list[CandidateRead]
    selected_indices: list[int]


class ImportConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_indices: list[Annotated[int, Field(ge=0, strict=True)]] = Field(
        min_length=1, max_length=MAX_CANDIDATES
    )

    @field_validator("selected_indices")
    @classmethod
    def unique_indices(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("候选题序号不能重复。")
        return sorted(value)


class ImportResult(BaseModel):
    import_id: UUID
    question_ids: list[UUID]
    imported_count: int
    replayed: bool


class SourceRead(BaseModel):
    import_id: UUID
    file_name: str
    content_sha256: str
    parser_version: str
    candidate: CandidateRead


class ImportService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def require(self, import_id: str, *, lock: bool = False) -> MarkdownImport:
        statement = select(MarkdownImport).where(MarkdownImport.id == import_id)
        batch = await self.session.scalar(statement.with_for_update() if lock else statement)
        if batch is None:
            raise ImportProblem("import_not_found", "导入批次不存在。", 404)
        return batch

    async def _sources(self, batch: MarkdownImport) -> list[QuestionImportSource]:
        return list(
            await self.session.scalars(
                select(QuestionImportSource)
                .where(QuestionImportSource.import_id == batch.id)
                .order_by(QuestionImportSource.candidate_index)
            )
        )

    @staticmethod
    def _candidate(batch: MarkdownImport, index: int) -> CandidateRead:
        candidate = Candidate.model_validate(batch.candidates_json[index])
        return CandidateRead(
            **candidate.model_dump(),
            raw_text=batch.original_text[candidate.start_offset : candidate.end_offset],
        )

    async def read(self, import_id: str) -> ImportPreview:
        batch = await self.require(import_id)
        return await self._preview(batch)

    async def _preview(self, batch: MarkdownImport) -> ImportPreview:
        return ImportPreview.model_validate(
            {
                "id": batch.id,
                "file_name": batch.file_name,
                "content_sha256": batch.content_sha256,
                "requested_strategy": batch.requested_strategy,
                "resolved_strategy": batch.resolved_strategy,
                "parser_version": batch.parser_version,
                "status": batch.status,
                "created_at": batch.created_at,
                "warnings": batch.warnings_json,
                "candidates": [
                    self._candidate(batch, i) for i in range(len(batch.candidates_json))
                ],
                "selected_indices": [
                    source.candidate_index for source in await self._sources(batch)
                ],
            }
        )

    async def preview(self, payload: ImportCreate) -> tuple[ImportPreview, bool]:
        fingerprint = hashlib.sha256(
            json.dumps([PARSER_VERSION, payload.strategy, payload.content]).encode("utf-8")
        ).hexdigest()
        try:
            async with self.session.begin():
                existing = await self.session.scalar(
                    select(MarkdownImport).where(MarkdownImport.fingerprint == fingerprint)
                )
                if existing:
                    return await self._preview(existing), False
                try:
                    parsed = parse_markdown(payload.content, payload.strategy)
                except MarkdownLimitError as error:
                    raise ImportProblem("import_limit", str(error), 422) from None
                batch = MarkdownImport(
                    fingerprint=fingerprint,
                    file_name=payload.file_name,
                    original_text=payload.content,
                    content_sha256=hashlib.sha256(payload.content.encode("utf-8")).hexdigest(),
                    requested_strategy=payload.strategy,
                    resolved_strategy=parsed.strategy,
                    parser_version=PARSER_VERSION,
                    candidates_json=[candidate.model_dump() for candidate in parsed.candidates],
                    warnings_json=parsed.warnings,
                )
                self.session.add(batch)
                await self.session.flush()
                return await self._preview(batch), True
        except IntegrityError:
            async with self.session.begin():
                existing = await self.session.scalar(
                    select(MarkdownImport).where(MarkdownImport.fingerprint == fingerprint)
                )
                if existing is None:
                    raise
                return await self._preview(existing), False

    async def _result(self, batch: MarkdownImport, replayed: bool) -> ImportResult:
        sources = await self._sources(batch)
        return ImportResult(
            import_id=UUID(batch.id),
            question_ids=[UUID(source.question_id) for source in sources],
            imported_count=len(sources),
            replayed=replayed,
        )

    async def confirm(self, import_id: str, payload: ImportConfirm) -> ImportResult:
        selection_hash = hashlib.sha256(json.dumps(payload.selected_indices).encode()).hexdigest()
        try:
            async with self.session.begin():
                batch = await self.require(import_id, lock=True)
                if batch.status == "confirmed":
                    return await self._replay(batch, selection_hash)
                count = len(batch.candidates_json)
                if any(index >= count for index in payload.selected_indices):
                    raise ImportProblem("invalid_selection", "选中的候选题不存在。", 422)
                candidates = [self._candidate(batch, index) for index in payload.selected_indices]
                if any(not candidate.question_text.strip() for candidate in candidates):
                    raise ImportProblem("empty_question", "空题干不能入库，请取消勾选。", 422)
                batch.status = "confirmed"
                batch.selection_hash = selection_hash
                now = utc_now()
                questions = [
                    Question(
                        id=str(uuid4()),
                        question_text=candidate.question_text,
                        standard_answer_text=candidate.standard_answer_text,
                        user_notes="",
                        source="markdown",
                        created_at=now,
                        updated_at=now,
                    )
                    for candidate in candidates
                ]
                self.session.add_all(questions)
                await self.session.flush()
                for candidate, question in zip(candidates, questions, strict=True):
                    self.session.add(QuestionService._snapshot(question))
                    self.session.add(
                        QuestionImportSource(
                            question_id=question.id,
                            import_id=batch.id,
                            candidate_index=candidate.index,
                        )
                    )
                await self.session.flush()
                return await self._result(batch, False)
        except (IntegrityError, StaleDataError):
            async with self.session.begin():
                batch = await self.require(import_id)
                if batch.status != "confirmed":
                    raise
                return await self._replay(batch, selection_hash)

    async def _replay(self, batch: MarkdownImport, selection_hash: str) -> ImportResult:
        if batch.selection_hash != selection_hash:
            raise ImportProblem(
                "import_already_confirmed", "该批次已确认，不能改选后再次导入。", 409
            )
        return await self._result(batch, True)

    async def source(self, question_id: str) -> SourceRead:
        await QuestionService(self.session).require_question(question_id)
        source = await self.session.get(QuestionImportSource, question_id)
        if source is None:
            raise ImportProblem("source_not_found", "此题没有 Markdown 来源。", 404)
        batch = await self.require(source.import_id)
        return SourceRead(
            import_id=UUID(batch.id),
            file_name=batch.file_name,
            content_sha256=batch.content_sha256,
            parser_version=batch.parser_version,
            candidate=self._candidate(batch, source.candidate_index),
        )
