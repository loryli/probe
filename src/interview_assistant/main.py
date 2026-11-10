"""Application factory and ownership of process-scoped resources."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from interview_assistant import __version__
from interview_assistant.api.health import router as health_router
from interview_assistant.ai.router import router as ai_router
from interview_assistant.config import Settings
from interview_assistant.database import Database
from interview_assistant.interview.router import interview_error_handler
from interview_assistant.interview.router import router as interview_router
from interview_assistant.interview.service import InterviewError
from interview_assistant.logging_config import configure_logging
from interview_assistant.materials.router import material_error_handler
from interview_assistant.materials.parse_router import router as parse_router
from interview_assistant.materials.router import router as material_router
from interview_assistant.materials.service import MaterialError
from interview_assistant.planner.router import planner_error_handler
from interview_assistant.planner.router import router as planner_router
from interview_assistant.planner.service import PlannerError
from interview_assistant.question_bank.import_router import import_error_handler
from interview_assistant.question_bank.import_router import router as import_router
from interview_assistant.question_bank.imports import ImportProblem
from interview_assistant.question_bank.router import category_router, question_error_handler
from interview_assistant.question_bank.router import router as question_router
from interview_assistant.question_bank.service import QuestionBankError

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        database_url = settings.resolved_database_url
        database = Database(database_url) if database_url is not None else None
        app.state.database = database
        try:
            if database is not None and settings.environment == "local" and database.is_sqlite:
                await database.initialize_local()
            logger.info("Application started; database_configured=%s", database is not None)
            yield
        finally:
            if database is not None:
                await database.close()
            app.state.database = None
            logger.info("Application stopped")

    app = FastAPI(
        title="Personal AI Interview Assistant",
        description="个人 AI 面试助手 API：个人题库、笔记导入、PDF 简历、项目事实、内容历史、面试计划与文字面试。",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = None
    app.include_router(health_router)
    app.include_router(question_router)
    app.include_router(category_router)
    app.include_router(import_router)
    app.include_router(material_router)
    app.include_router(ai_router)
    app.include_router(parse_router)
    app.include_router(planner_router)
    app.include_router(interview_router)
    app.add_exception_handler(QuestionBankError, question_error_handler)
    app.add_exception_handler(ImportProblem, import_error_handler)
    app.add_exception_handler(MaterialError, material_error_handler)
    app.add_exception_handler(PlannerError, planner_error_handler)
    app.add_exception_handler(InterviewError, interview_error_handler)
    web_directory = Path(__file__).with_name("web")
    app.mount("/assets", StaticFiles(directory=web_directory), name="assets")

    @app.get("/", response_class=FileResponse, include_in_schema=False)
    async def workspace() -> FileResponse:
        return FileResponse(web_directory / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/materials", response_class=FileResponse, include_in_schema=False)
    async def materials_workspace() -> FileResponse:
        return FileResponse(web_directory / "materials.html", headers={"Cache-Control": "no-cache"})

    @app.get("/planner", response_class=FileResponse, include_in_schema=False)
    async def planner_workspace() -> FileResponse:
        return FileResponse(web_directory / "planner.html", headers={"Cache-Control": "no-cache"})

    @app.get("/interview", response_class=FileResponse, include_in_schema=False)
    async def interview_workspace() -> FileResponse:
        return FileResponse(web_directory / "interview.html", headers={"Cache-Control": "no-cache"})

    return app
