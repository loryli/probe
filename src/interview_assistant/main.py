"""Application factory and ownership of process-scoped resources."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
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
    frontend_build = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    frontend_index = frontend_build / "index.html"
    if frontend_index.is_file():
        # Preserve Vite's assets/ subdirectory beneath its configured base URL.
        app.mount(
            "/frontend-assets",
            StaticFiles(directory=frontend_build),
            name="frontend-assets",
        )

    @app.get("/app", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/app/{path:path}", response_class=HTMLResponse, include_in_schema=False)
    async def frontend_workspace(path: str = "") -> Response:
        if frontend_index.is_file():
            return FileResponse(frontend_index, headers={"Cache-Control": "no-cache"})
        return HTMLResponse(
            '<!doctype html><html lang="zh-CN"><meta charset="UTF-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>前端尚未构建 · 练习场</title><body>'
            '<h1>前端尚未构建</h1>'
            '<p>开发时在 frontend 目录运行 npm ci 和 npm run dev，'
            '然后访问 <a href="http://127.0.0.1:5173/">开发前端</a>。</p>'
            '<p>使用构建版时，在 frontend 目录运行 npm run build，'
            '然后重启后端。API 仍可正常使用。</p></body></html>',
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/", response_class=RedirectResponse, include_in_schema=False)
    async def workspace() -> RedirectResponse:
        return RedirectResponse("/app/", status_code=307)

    @app.get("/materials", response_class=RedirectResponse, include_in_schema=False)
    async def materials_workspace() -> RedirectResponse:
        return RedirectResponse("/app/materials", status_code=307)

    @app.get("/planner", response_class=RedirectResponse, include_in_schema=False)
    async def planner_workspace() -> RedirectResponse:
        return RedirectResponse("/app/planner", status_code=307)

    @app.get("/interview", response_class=RedirectResponse, include_in_schema=False)
    async def interview_workspace() -> RedirectResponse:
        return RedirectResponse("/app/interviews", status_code=307)

    return app
