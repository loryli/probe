import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from interview_assistant.api.sse import ServerEvent, event_stream


def test_stream_preserves_unicode_and_multiline_data_without_injecting_events():
    app = FastAPI()
    payload = {"text": "第一行\n第二行\nevent: forged"}

    async def events():
        yield ServerEvent(id="evt-1", event="analysis.completed", data=payload)
        yield ServerEvent(id="evt-2", event="interview.status_changed", data={"status": "paused"})

    @app.get("/events")
    async def stream():
        return event_stream(events())

    with TestClient(app) as client:
        response = client.get("/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    frames = response.text.strip().split("\n\n")
    assert len(frames) == 2
    first_lines = frames[0].splitlines()
    assert first_lines[:2] == ["id: evt-1", "event: analysis.completed"]
    assert json.loads(first_lines[2].removeprefix("data: ")) == payload


@pytest.mark.parametrize("field", ["event", "id"])
@pytest.mark.parametrize("value", ["bad\nevent", "bad\revent", "bad\x00event", ""])
def test_event_metadata_cannot_break_sse_framing(field, value):
    fields = {"event": "valid", "id": "valid", "data": {}}
    fields[field] = value
    with pytest.raises(ValidationError):
        ServerEvent(**fields)
