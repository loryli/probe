"""SSE framing only; durable event ordering and replay belong to the future runtime."""

import json
from collections.abc import AsyncIterable, AsyncIterator

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from starlette.responses import StreamingResponse


class ServerEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event: str = Field(min_length=1, pattern=r"^[^\r\n\x00]+$")
    data: JsonValue
    id: str | None = Field(default=None, min_length=1, pattern=r"^[^\r\n\x00]+$")

    def encode(self) -> str:
        payload = json.dumps(self.data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        identifier = f"id: {self.id}\n" if self.id is not None else ""
        return f"{identifier}event: {self.event}\ndata: {payload}\n\n"


def event_stream(events: AsyncIterable[ServerEvent]) -> StreamingResponse:
    async def encode_events() -> AsyncIterator[str]:
        async for event in events:
            yield event.encode()

    return StreamingResponse(
        encode_events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
