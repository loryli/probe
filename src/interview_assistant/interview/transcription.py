"""语音只生成临时草稿，回答仍走原有文字提交。"""

import asyncio
import base64
import hashlib
import io
import logging
import wave
from datetime import timedelta
from typing import Literal
from uuid import UUID

import httpx2
from pydantic import BaseModel
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from interview_assistant.config import Settings
from interview_assistant.database import Database, utc_now
from interview_assistant.interview.errors import InterviewError
from interview_assistant.interview.models import InterviewSession, InterviewTranscription
from interview_assistant.interview.schemas import MAX_ANSWER_CHARS

MAX_SECONDS = 180
SAMPLE_RATE = 16_000
MAX_AUDIO_BYTES = MAX_SECONDS * SAMPLE_RATE * 2 + 1024
TRANSCRIBE_TIMEOUT = 45
RUNNING_LIMIT = timedelta(seconds=TRANSCRIBE_TIMEOUT + 20)
DRAFT_TTL = timedelta(minutes=30)

logger = logging.getLogger(__name__)


class TranscriptionRead(BaseModel):
    request_id: UUID
    turn_id: UUID
    status: Literal["running", "ready", "failed", "expired"]
    transcript: str | None = None
    error_code: str | None = None
    error_message: str | None = None


MESSAGES = {
    "transcription_interrupted": "转写没有完成。可以重录或继续打字。",
    "transcription_timeout": "转写超时。可以重录或继续打字。",
    "transcription_rejected": "语音服务拒绝了请求，请检查百炼语音模型、密钥和接入地址。",
    "transcription_unavailable": "暂时无法访问语音服务，可以继续打字。",
    "transcription_empty": "没有识别到可用文字，请检查麦克风后重录。",
    "transcription_too_long": "识别内容超过回答字数上限，请缩短录音或分段录制。",
    "transcription_expired": "这段转写已过期，可以重新录音。",
}


def validate_audio(audio: bytes) -> None:
    try:
        with wave.open(io.BytesIO(audio), "rb") as reader:
            frames = reader.getnframes()
            if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) != (
                1, 2, SAMPLE_RATE,
            ):
                raise ValueError
            if not 0 < frames <= MAX_SECONDS * SAMPLE_RATE:
                raise ValueError
            if len(reader.readframes(frames)) != frames * 2:
                raise ValueError
    except (EOFError, ValueError, wave.Error) as error:
        raise InterviewError("invalid_voice_audio", "请上传最长 3 分钟的有效录音。", 422) from error


def read_task(row: InterviewTranscription) -> TranscriptionRead:
    now = utc_now()
    status: Literal["running", "ready", "failed", "expired"]
    code = row.error_code
    if row.expires_at <= now:
        status, code = "expired", "transcription_expired"
    elif row.status == "ready":
        status = "ready"
    elif row.status == "running" and now < row.created_at + RUNNING_LIMIT:
        status = "running"
    else:
        status, code = "failed", code or "transcription_interrupted"
    return TranscriptionRead(
        request_id=UUID(row.request_id), turn_id=UUID(row.turn_id), status=status,
        transcript=row.transcript if status == "ready" else None,
        error_code=code, error_message=MESSAGES.get(code) if code else None,
    )


async def clear_expired_drafts(session: AsyncSession) -> None:
    # 留下轻量幂等记录直至场次删除；过期记录不能再次触发收费调用。
    await session.execute(
        update(InterviewTranscription)
        .where(
            InterviewTranscription.expires_at <= utc_now(),
            InterviewTranscription.transcript.is_not(None),
        )
        .values(transcript=None)
    )


async def get_task(
    session: AsyncSession, session_id: str, request_id: str,
) -> TranscriptionRead:
    async with session.begin():
        row = await session.get(InterviewTranscription, request_id)
        if row is None or row.session_id != session_id:
            raise InterviewError("transcription_not_found", "未找到这段录音的转写任务。", 404)
        if row.transcript is not None and row.expires_at <= utc_now():
            row.transcript = None
        return read_task(row)


def _existing_task(
    row: InterviewTranscription, session_id: str, turn_id: str, digest: str,
) -> TranscriptionRead:
    if (row.session_id, row.turn_id, row.audio_sha256) != (session_id, turn_id, digest):
        raise InterviewError("transcription_request_conflict", "录音请求与原任务不一致。", 409)
    return read_task(row)


async def accept_audio(
    session: AsyncSession, settings: Settings, session_id: str, turn_id: str,
    request_id: str, audio: bytes,
) -> tuple[TranscriptionRead, bool]:
    validate_audio(audio)
    digest = hashlib.sha256(audio).hexdigest()
    try:
        async with session.begin():
            row = await session.get(InterviewTranscription, request_id)
            if row is not None:
                return _existing_task(row, session_id, turn_id, digest), False
            if not settings.voice_transcription_configured:
                raise InterviewError(
                    "transcription_not_configured",
                    "语音转写未启用，请先配置百炼密钥和语音模型，或继续打字。",
                    503,
                )
            interview = await session.get(InterviewSession, session_id, with_for_update=True)
            if interview is None:
                raise InterviewError("session_not_found", "面试场次不存在。", 404)
            if interview.status != "active" or interview.current_turn_id != turn_id:
                raise InterviewError("voice_turn_changed", "当前问题已变化，未开始转写。", 409)
            now = utc_now()
            row = InterviewTranscription(
                request_id=request_id, session_id=session_id, turn_id=turn_id,
                audio_sha256=digest, status="running", created_at=now,
                expires_at=now + DRAFT_TTL,
            )
            session.add(row)
            return read_task(row), True
    except IntegrityError:
        async with session.begin():
            existing = await session.get(InterviewTranscription, request_id)
            if existing is None:
                raise
            return _existing_task(existing, session_id, turn_id, digest), False


async def _transcribe(settings: Settings, audio: bytes) -> tuple[str | None, str | None]:
    assert settings.qwen_api_key is not None and settings.qwen_asr_model is not None
    encoded = base64.b64encode(audio).decode("ascii")
    payload = {
        "model": settings.qwen_asr_model,
        "messages": [{
            "role": "user",
            "content": [{
                "type": "input_audio",
                "input_audio": {"data": f"data:audio/wav;base64,{encoded}"},
            }],
        }],
        "stream": False,
    }
    try:
        async with asyncio.timeout(TRANSCRIBE_TIMEOUT):
            async with httpx2.AsyncClient(trust_env=settings.qwen_trust_env) as client:
                response = await client.post(
                    f"{settings.qwen_base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.qwen_api_key.get_secret_value()}"},
                    json=payload,
                    timeout=TRANSCRIBE_TIMEOUT,
                )
        if response.is_error:
            logger.warning("Voice provider rejected call; status=%d", response.status_code)
            return None, (
                "transcription_rejected" if response.is_client_error
                else "transcription_unavailable"
            )
        content = response.json()["choices"][0]["message"]["content"]
    except (TimeoutError, httpx2.TimeoutException):
        return None, "transcription_timeout"
    except (httpx2.HTTPError, ValueError, TypeError, IndexError, KeyError):
        return None, "transcription_unavailable"
    if not isinstance(content, str) or not content.strip():
        return None, "transcription_empty"
    transcript = content.strip()
    if len(transcript) > MAX_ANSWER_CHARS:
        return None, "transcription_too_long"
    return transcript, None


async def run_transcription(
    database: Database, settings: Settings, request_id: str, audio: bytes,
) -> None:
    try:
        transcript, error_code = await _transcribe(settings, audio)
        async with database.sessions() as session, session.begin():
            await clear_expired_drafts(session)
            await session.execute(
                update(InterviewTranscription)
                .where(
                    InterviewTranscription.request_id == request_id,
                    InterviewTranscription.status == "running",
                    InterviewTranscription.expires_at > utc_now(),
                )
                .values(
                    status="ready" if transcript is not None else "failed",
                    transcript=transcript,
                    error_code=error_code,
                )
            )
    except Exception as error:
        # 此处不记录异常详情：第三方异常和数据库参数可能包含录音或转写。
        logger.error("Voice task failed; request_id=%s error=%s", request_id, type(error).__name__)
