"""Liveness is independent of MySQL; readiness checks it with a bounded timeout."""

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from interview_assistant.api.dependencies import DatabaseDependency, SettingsDependency

router = APIRouter(prefix="/health", tags=["health"])
logger = logging.getLogger(__name__)


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    status: Literal["ok", "not_ready"]
    database: Literal["ok", "not_configured", "unavailable"]


@router.get("/live", response_model=LivenessResponse)
async def liveness() -> LivenessResponse:
    return LivenessResponse()


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse}},
)
async def readiness(
    response: Response,
    database: DatabaseDependency,
    settings: SettingsDependency,
) -> ReadinessResponse:
    if database is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(status="not_ready", database="not_configured")
    try:
        async with asyncio.timeout(settings.database_probe_timeout_seconds):
            await database.ping()
    except (SQLAlchemyError, OSError, TimeoutError) as error:
        # Driver exceptions may contain credentials or server details. Log only their type.
        logger.warning("Database readiness check failed; error_type=%s", type(error).__name__)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(status="not_ready", database="unavailable")
    return ReadinessResponse(status="ok", database="ok")
