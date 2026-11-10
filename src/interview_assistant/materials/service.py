import hashlib
import json
from pathlib import Path

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from interview_assistant.database import utc_now
from interview_assistant.materials.errors import MaterialError
from interview_assistant.materials.models import (
    Project, ProjectFact, Resume, ResumeDocument, ResumeExtraction, ResumeVersion,
)
from interview_assistant.materials.pdf import EXTRACTION_VERSION, OcrMode, PdfText, extract_pdf
from interview_assistant.materials.schemas import (
    DocumentRead, FactInput, FactRead, ProjectCreate, ProjectList,
    ProjectRead, ProjectUpdate, ResumeCreate, ResumeList, ResumeRead, ResumeSummary,
    ResumeUpdate, VersionList, VersionSummary,
)


def conflict() -> MaterialError:
    return MaterialError("revision_conflict", "资料已被修改，请保留草稿并读取最新内容", 409)


def fingerprint(payload: ResumeCreate | ProjectCreate) -> str:
    encoded = json.dumps(payload.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def check_replay(item: Resume | Project, expected_hash: str) -> None:
    if item.creation_hash != expected_hash or item.deleted_at is not None:
        raise MaterialError("creation_key_conflict", "创建标识已用于其他内容或资料已删除", 409)


def original_quote(source: str, quote: str) -> str | None:
    """浏览器复制可能转换换行；匹配后仍保存来源中的实际原文。"""
    if quote in source:
        return quote
    characters: list[str] = []
    boundaries = [0]
    offset = 0
    while offset < len(source):
        character = source[offset]
        offset += 1
        if character == "\r":
            if offset < len(source) and source[offset] == "\n":
                offset += 1
            character = "\n"
        characters.append(character)
        boundaries.append(offset)
    normalized = quote.replace("\r\n", "\n").replace("\r", "\n")
    start = "".join(characters).find(normalized)
    return None if start < 0 else source[boundaries[start]:boundaries[start + len(normalized)]]


class MaterialService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def require_resume(self, resume_id: str, *, lock: bool = False) -> Resume:
        query = select(Resume).where(Resume.id == resume_id, Resume.deleted_at.is_(None))
        resume = await self.session.scalar(query.with_for_update() if lock else query)
        if resume is None:
            raise MaterialError("resume_not_found", "简历不存在或已删除", 404)
        return resume

    async def require_version(self, resume_id: str, version: int) -> ResumeVersion:
        item = await self.session.get(ResumeVersion, (resume_id, version))
        if item is None:
            raise MaterialError("version_not_found", "简历版本不存在", 404)
        return item

    async def require_project(self, resume_id: str, project_id: str) -> Project:
        project = await self.session.scalar(select(Project).where(
            Project.id == project_id, Project.resume_id == resume_id,
            Project.deleted_at.is_(None),
        ))
        if project is None:
            raise MaterialError("project_not_found", "项目不存在或已删除", 404)
        return project

    @staticmethod
    def snapshot(resume: Resume) -> ResumeVersion:
        return ResumeVersion(
            resume_id=resume.id, version=resume.content_version, name=resume.name,
            source_text=resume.source_text, source_file_name=resume.source_file_name,
            document_id=resume.document_id,
            content_sha256=hashlib.sha256(resume.source_text.encode("utf-8")).hexdigest(),
            created_at=resume.updated_at,
        )

    async def document_fields(self, payload: ResumeCreate | ResumeUpdate) -> dict[str, object]:
        values = payload.model_dump(exclude={"expected_revision"})
        values["document_id"] = str(payload.document_id) if payload.document_id else None
        if payload.document_id:
            document = await self.session.get(ResumeDocument, str(payload.document_id))
            if document is None:
                raise MaterialError("document_not_found", "PDF 原件不存在，请重新选择文件", 404)
            values["source_file_name"] = document.file_name
        return values

    async def upload_pdf(
        self, file_name: str, content: bytes, mode: OcrMode, model_dir: Path,
    ) -> DocumentRead:
        digest = hashlib.sha256(content).hexdigest()
        key = f"{EXTRACTION_VERSION}:{mode}"
        # OCR 在事务外执行，避免识别/首次下载模型期间占用数据库事务。
        async with self.session.begin():
            document = await self.session.scalar(select(ResumeDocument).where(
                ResumeDocument.content_sha256 == digest
            ))
            if document is not None:
                cached = await self.session.get(ResumeExtraction, (document.id, key))
                if cached is not None:
                    return self.extraction_read(document, cached, mode)
        extracted = await run_in_threadpool(extract_pdf, content, mode, model_dir)
        try:
            async with self.session.begin():
                document = await self.session.scalar(select(ResumeDocument).where(
                    ResumeDocument.content_sha256 == digest
                ))
                if document is None:
                    document = ResumeDocument(
                        file_name=file_name, content_sha256=digest, original_bytes=content,
                        extracted_text=extracted.text, page_count=extracted.page_count,
                        warnings_json=extracted.warnings,
                    )
                    self.session.add(document)
                    await self.session.flush()
                cached = await self.session.get(ResumeExtraction, (document.id, key))
                if cached is None and extracted.cacheable:
                    cached = ResumeExtraction(
                        document_id=document.id, extraction_key=key, extracted_text=extracted.text,
                        warnings_json=extracted.warnings, ocr_pages_json=extracted.ocr_pages,
                        page_methods_json=extracted.page_methods,
                    )
                    self.session.add(cached)
                    await self.session.flush()
                result = self.extraction_read(document, cached or extracted, mode)
            return result
        except IntegrityError:
            async with self.session.begin():
                existing = await self.session.scalar(select(ResumeDocument).where(
                    ResumeDocument.content_sha256 == digest
                ))
                if existing is None:
                    raise
                cached = await self.session.get(ResumeExtraction, (existing.id, key))
                return self.extraction_read(existing, cached or extracted, mode)

    @staticmethod
    def extraction_read(
        document: ResumeDocument, result: ResumeExtraction | PdfText, mode: OcrMode,
    ) -> DocumentRead:
        if isinstance(result, ResumeExtraction):
            output = PdfText(
                text=result.extracted_text, page_count=document.page_count,
                warnings=result.warnings_json, ocr_pages=result.ocr_pages_json,
                page_methods=result.page_methods_json,
            )
        else:
            output = result
        return DocumentRead(
            id=document.id, file_name=document.file_name, content_sha256=document.content_sha256,
            extracted_text=output.text,
            page_count=document.page_count,
            warnings=output.warnings,
            ocr_mode=mode,
            ocr_pages=output.ocr_pages,
            page_methods=output.page_methods,
            extraction_version=EXTRACTION_VERSION,
        )

    async def reextract_pdf(self, document_id: str, mode: OcrMode, model_dir: Path) -> DocumentRead:
        async with self.session.begin():
            document = await self.session.get(ResumeDocument, document_id)
            if document is None:
                raise MaterialError("document_not_found", "PDF 原件不存在", 404)
            content, file_name = document.original_bytes, document.file_name
        return await self.upload_pdf(file_name, content, mode, model_dir)

    async def create_resume(self, payload: ResumeCreate, key: str | None) -> tuple[ResumeRead, bool]:
        digest = fingerprint(payload)
        try:
            async with self.session.begin():
                if key:
                    existing = await self.session.scalar(select(Resume).where(Resume.creation_key == key))
                    if existing is not None:
                        check_replay(existing, digest)
                        return ResumeRead.model_validate(existing), False
                resume = Resume(
                    **await self.document_fields(payload), creation_key=key,
                    creation_hash=digest if key else None,
                )
                self.session.add(resume)
                await self.session.flush()
                self.session.add(self.snapshot(resume))
                await self.session.flush()
                result = ResumeRead.model_validate(resume)
            return result, True
        except IntegrityError:
            if not key:
                raise
            async with self.session.begin():
                existing = await self.session.scalar(select(Resume).where(Resume.creation_key == key))
                if existing is None:
                    raise
                check_replay(existing, digest)
                return ResumeRead.model_validate(existing), False

    async def list_resumes(self, search: str | None, limit: int, offset: int) -> ResumeList:
        conditions = [Resume.deleted_at.is_(None)]
        if search:
            conditions.append(Resume.name.contains(search, autoescape=True))
        total = await self.session.scalar(select(func.count()).select_from(Resume).where(*conditions))
        rows = await self.session.scalars(select(Resume).where(*conditions).order_by(
            Resume.created_at.desc(), Resume.id
        ).limit(limit).offset(offset))
        return ResumeList(items=[ResumeSummary.model_validate(row) for row in rows],
                          total=total or 0, limit=limit, offset=offset)

    async def update_resume(self, resume_id: str, payload: ResumeUpdate) -> ResumeRead:
        try:
            async with self.session.begin():
                resume = await self.require_resume(resume_id, lock=True)
                if resume.revision != payload.expected_revision:
                    raise conflict()
                changes = await self.document_fields(payload)
                changed = {name for name, value in changes.items() if getattr(resume, name) != value}
                for name in changed:
                    setattr(resume, name, changes[name])
                if changed:
                    resume.updated_at = utc_now()
                    content_changed = bool(changed - {"is_active"})
                    if content_changed:
                        resume.content_version += 1
                    await self.session.flush()
                    if content_changed:
                        self.session.add(self.snapshot(resume))
                        await self.session.flush()
                result = ResumeRead.model_validate(resume)
            return result
        except StaleDataError:
            raise conflict() from None

    async def delete_resume(self, resume_id: str, revision: int) -> None:
        try:
            async with self.session.begin():
                resume = await self.require_resume(resume_id, lock=True)
                if resume.revision != revision:
                    raise conflict()
                resume.deleted_at = resume.updated_at = utc_now()
                resume.is_active = False
                await self.session.flush()
        except StaleDataError:
            raise conflict() from None

    async def history(self, resume_id: str, limit: int, offset: int) -> VersionList:
        resume = await self.require_resume(resume_id)
        rows = await self.session.scalars(select(ResumeVersion).where(
            ResumeVersion.resume_id == resume_id
        ).order_by(ResumeVersion.version.desc()).limit(limit).offset(offset))
        return VersionList(items=[VersionSummary.model_validate(row) for row in rows],
                           total=resume.content_version, limit=limit, offset=offset)

    async def facts(self, project_id: str) -> list[ProjectFact]:
        rows = await self.session.scalars(select(ProjectFact).where(
            ProjectFact.project_id == project_id, ProjectFact.deleted_at.is_(None)
        ).order_by(ProjectFact.position))
        return list(rows)

    async def project_read(self, project: Project) -> ProjectRead:
        values = {name: getattr(project, name) for name in ProjectRead.model_fields if name != "facts"}
        values["facts"] = [FactRead.model_validate(fact) for fact in await self.facts(project.id)]
        return ProjectRead.model_validate(values)

    async def replace_facts(self, project: Project, facts: list[FactInput]) -> None:
        version = await self.require_version(project.resume_id, project.source_version)
        quotes: list[str] = []
        for fact in facts:
            quote = original_quote(version.source_text, fact.evidence_quote)
            if quote is None:
                raise MaterialError(
                    "evidence_mismatch",
                    f"事实引用必须是简历 v{project.source_version} 文字中的连续原文；请从来源文字复制",
                )
            quotes.append(quote)
        for existing in await self.facts(project.id):
            existing.deleted_at = utc_now()
        for position, fact in enumerate(facts):
            self.session.add(ProjectFact(
                project_id=project.id, position=position,
                **fact.model_dump(exclude={"evidence_quote"}), evidence_quote=quotes[position],
                source_kind="resume" if fact.evidence_quote else "user_edit",
            ))
        await self.session.flush()

    async def create_project(
        self, resume_id: str, payload: ProjectCreate, key: str | None
    ) -> tuple[ProjectRead, bool]:
        digest = fingerprint(payload)
        try:
            async with self.session.begin():
                await self.require_resume(resume_id, lock=True)
                if key:
                    existing = await self.session.scalar(select(Project).where(Project.creation_key == key))
                    if existing is not None:
                        check_replay(existing, digest)
                        if existing.resume_id != resume_id:
                            raise MaterialError("creation_key_conflict", "创建标识已用于其他简历", 409)
                        return await self.project_read(existing), False
                await self.require_version(resume_id, payload.source_version)
                project = Project(
                    resume_id=resume_id, **payload.model_dump(exclude={"facts"}),
                    creation_key=key, creation_hash=digest if key else None,
                )
                self.session.add(project)
                await self.session.flush()
                await self.replace_facts(project, payload.facts)
                result = await self.project_read(project)
            return result, True
        except IntegrityError:
            if not key:
                raise
            async with self.session.begin():
                await self.require_resume(resume_id)
                existing = await self.session.scalar(select(Project).where(Project.creation_key == key))
                if existing is None:
                    raise
                check_replay(existing, digest)
                if existing.resume_id != resume_id:
                    raise MaterialError("creation_key_conflict", "创建标识已用于其他简历", 409)
                return await self.project_read(existing), False

    async def list_projects(self, resume_id: str, limit: int, offset: int) -> ProjectList:
        await self.require_resume(resume_id)
        conditions = [Project.resume_id == resume_id, Project.deleted_at.is_(None)]
        total = await self.session.scalar(select(func.count()).select_from(Project).where(*conditions))
        rows = await self.session.scalars(select(Project).where(*conditions).order_by(
            Project.created_at.desc(), Project.id
        ).limit(limit).offset(offset))
        return ProjectList(items=[await self.project_read(row) for row in rows],
                           total=total or 0, limit=limit, offset=offset)

    async def update_project(
        self, resume_id: str, project_id: str, payload: ProjectUpdate
    ) -> ProjectRead:
        try:
            async with self.session.begin():
                await self.require_resume(resume_id, lock=True)
                project = await self.require_project(resume_id, project_id)
                if project.revision != payload.expected_revision:
                    raise conflict()
                current = await self.project_read(project)
                fields = payload.model_dump(exclude={"facts", "expected_revision"})
                facts_changed = [fact.model_dump() for fact in payload.facts] != [
                    fact.model_dump(exclude={"id", "source_kind"}) for fact in current.facts
                ]
                changed = any(getattr(project, key) != value for key, value in fields.items())
                if changed or facts_changed:
                    for key, value in fields.items():
                        setattr(project, key, value)
                    project.updated_at = utc_now()
                    await self.session.flush()
                    if facts_changed:
                        await self.replace_facts(project, payload.facts)
                result = await self.project_read(project)
            return result
        except StaleDataError:
            raise conflict() from None

    async def delete_project(self, resume_id: str, project_id: str, revision: int) -> None:
        try:
            async with self.session.begin():
                await self.require_resume(resume_id, lock=True)
                project = await self.require_project(resume_id, project_id)
                if project.revision != revision:
                    raise conflict()
                project.deleted_at = project.updated_at = utc_now()
                await self.session.flush()
        except StaleDataError:
            raise conflict() from None
