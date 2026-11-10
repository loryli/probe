"""Qwen Chat Completions 适配器：单次/流式调用、限时、JSON 校验与脱敏错误。"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

import httpx2
from pydantic import ValidationError

from interview_assistant.ai.protocol import ModelMessage, OutputModel
from interview_assistant.config import Settings

logger = logging.getLogger(__name__)


class ModelError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code, self.message = code, message


@dataclass
class ModelUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None


def configuration_status(settings: Settings) -> dict[str, object]:
    key = settings.qwen_api_key
    missing = []
    if key is None or not key.get_secret_value().strip():
        missing.append("INTERVIEW_QWEN_API_KEY")
    if not settings.qwen_model:
        missing.append("INTERVIEW_QWEN_MODEL")
    return {"provider": "qwen", "configured": not missing,
            "model": settings.qwen_model, "missing": missing}


def _raise_for_status(code: int) -> None:
    """按状态码给出可读的失败原因；由流式与非流式调用共用。"""
    if code in {401, 403}:
        raise ModelError("model_auth_failed", "Qwen 鉴权或权限校验失败，请检查密钥、地域和模型权限")
    if code == 429:
        raise ModelError("model_rate_limited", "Qwen 请求受限，请检查额度或稍后重试")
    if code in {400, 404, 422}:
        raise ModelError("model_request_rejected", "Qwen 拒绝请求，请检查模型名称、接口地址及 JSON/非思考模式支持情况")
    if not 200 <= code < 300:
        raise ModelError("model_upstream_error", "Qwen 服务返回错误，请稍后重试")


class QwenModel:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.usage = ModelUsage()
        self.elapsed_ms = 0
        self.first_token_ms = 0
        self.result: OutputModel | None = None
        self.finish_reason: str | None = None

    async def generate(
        self, *, messages: Sequence[ModelMessage], output_type: type[OutputModel],
        prompt_version: str, schema_version: str, temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> OutputModel:
        settings = self.settings
        if not configuration_status(settings)["configured"]:
            raise ModelError("model_not_configured", "请先在本地 .env 配置 Qwen API Key 和模型名称")
        assert settings.qwen_api_key is not None
        schema = json.dumps(output_type.model_json_schema(), ensure_ascii=False)
        payload: dict[str, object] = {
            "model": settings.qwen_model,
            "messages": [{"role": message.role, "content": message.content} for message in messages]
            + [{"role": "user", "content": "请仅返回符合以下 JSON Schema 的 JSON 对象：\n" + schema}],
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens or settings.qwen_max_output_tokens,
            "stream": False,
        }
        if settings.qwen_enable_thinking is not None:
            payload["enable_thinking"] = settings.qwen_enable_thinking
        if temperature is not None:
            # 面试需要同题不同措辞；抽取类调用不传，保持供应商默认行为。
            payload["temperature"] = temperature
        started = time.monotonic()
        try:
            async with asyncio.timeout(settings.qwen_timeout_seconds):
                async with httpx2.AsyncClient(
                    timeout=httpx2.Timeout(settings.qwen_timeout_seconds, connect=10),
                    follow_redirects=False,
                    trust_env=settings.qwen_trust_env,
                ) as client:
                    async with client.stream(
                        "POST", settings.qwen_base_url + "/chat/completions", json=payload,
                        headers={"Authorization": "Bearer " + settings.qwen_api_key.get_secret_value().strip()},
                    ) as response:
                        if response.status_code >= 400:
                            logger.warning("Qwen request rejected; status=%d", response.status_code)
                        _raise_for_status(response.status_code)
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > 4 * 1024 * 1024:
                                raise ModelError("model_response_too_large", "模型返回过大，请精简简历或减少提取内容")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Invalid response envelope")
            usage = data.get("usage")
            if isinstance(usage, dict):
                for source, target in [("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens")]:
                    value = usage.get(source)
                    if type(value) is int and value >= 0:
                        setattr(self.usage, target, value)
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise ValueError("Missing choice")
            choice = choices[0]
            if choice.get("finish_reason") == "length":
                raise ModelError("model_output_truncated", "模型输出被截断，请精简简历或调整 INTERVIEW_QWEN_MAX_OUTPUT_TOKENS")
            if choice.get("finish_reason") != "stop":
                raise ModelError("model_output_incomplete", "模型未正常完成提取，请检查资料或稍后重新提取")
            message = choice.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Missing JSON content")
            return output_type.model_validate_json(content)
        except ModelError:
            raise
        except (TimeoutError, httpx2.TimeoutException):
            raise ModelError("model_timeout", "Qwen 调用超时，本次未写入项目；可稍后重新提取") from None
        except httpx2.HTTPError:
            raise ModelError("model_connection_failed", "无法连接 Qwen，请检查网络及接口地址") from None
        except (ValueError, ValidationError):
            raise ModelError("model_invalid_output", "模型结果未通过 JSON 结构校验，本次未保存结果") from None
        finally:
            self.elapsed_ms = round((time.monotonic() - started) * 1000)
            logger.info("Model call finished; provider=qwen prompt=%s schema=%s elapsed_ms=%d",
                        prompt_version, schema_version, self.elapsed_ms)

    async def generate_stream(
        self, *, messages: Sequence[ModelMessage], output_type: type[OutputModel],
        prompt_version: str, schema_version: str, temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """流式调用：逐段吐出模型正文，结束后把校验结果放在 self.result。

        正文在流式到达时就已经交给调用方展示；若最终 JSON 不合法，
        仍然抛 ModelError，由调用方决定丢弃已展示的文本并提示重试。
        """
        settings = self.settings
        if not configuration_status(settings)["configured"]:
            raise ModelError("model_not_configured", "请先在本地 .env 配置 Qwen API Key 和模型名称")
        assert settings.qwen_api_key is not None
        schema = json.dumps(output_type.model_json_schema(), ensure_ascii=False)
        payload: dict[str, object] = {
            "model": settings.qwen_model,
            "messages": [{"role": message.role, "content": message.content} for message in messages]
            + [{"role": "user", "content": "请仅返回符合以下 JSON Schema 的 JSON 对象：\n" + schema}],
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens or settings.qwen_max_output_tokens,
            "stream": True,
            # 最后一帧附带 token 用量；否则流式响应不返回 usage，落库统计为空。
            "stream_options": {"include_usage": True},
        }
        if settings.qwen_enable_thinking is not None:
            payload["enable_thinking"] = settings.qwen_enable_thinking
        if temperature is not None:
            payload["temperature"] = temperature
        started = time.monotonic()
        self.result = None
        self.finish_reason = None
        raw = bytearray()
        buffer = ""
        try:
            async with asyncio.timeout(settings.qwen_timeout_seconds):
                async with httpx2.AsyncClient(
                    timeout=httpx2.Timeout(settings.qwen_timeout_seconds, connect=10),
                    follow_redirects=False,
                    trust_env=settings.qwen_trust_env,
                ) as client:
                    async with client.stream(
                        "POST", settings.qwen_base_url + "/chat/completions", json=payload,
                        headers={"Authorization": "Bearer " + settings.qwen_api_key.get_secret_value().strip()},
                    ) as response:
                        _raise_for_status(response.status_code)
                        async for chunk in response.aiter_bytes():
                            if not chunk:
                                continue
                            buffer += chunk.decode("utf-8", errors="ignore")
                            # 供应商的 SSE 帧以空行分隔；半帧留在 buffer 里等下一次。
                            while "\n" in buffer:
                                line, buffer = buffer.split("\n", 1)
                                line = line.strip()
                                if not line or line.startswith(":"):
                                    continue
                                if not line.startswith("data:"):
                                    continue
                                data = line[5:].strip()
                                if data == "[DONE]":
                                    continue
                                try:
                                    frame = json.loads(data)
                                except ValueError:
                                    continue
                                if not isinstance(frame, dict):
                                    continue
                                usage = frame.get("usage")
                                if isinstance(usage, dict):
                                    for source, target in (
                                        ("prompt_tokens", "input_tokens"),
                                        ("completion_tokens", "output_tokens"),
                                    ):
                                        value = usage.get(source)
                                        if type(value) is int and value >= 0:
                                            setattr(self.usage, target, value)
                                choices = frame.get("choices")
                                if not isinstance(choices, list) or not choices:
                                    continue
                                choice = choices[0]
                                if not isinstance(choice, dict):
                                    continue
                                if choice.get("finish_reason"):
                                    self.finish_reason = str(choice["finish_reason"])
                                delta = choice.get("delta")
                                text = delta.get("content") if isinstance(delta, dict) else None
                                if not isinstance(text, str) or not text:
                                    continue
                                raw.extend(text.encode("utf-8"))
                                if not self.first_token_ms:
                                    self.first_token_ms = round((time.monotonic() - started) * 1000)
                                yield text
                                if len(raw) > 64 * 1024:
                                    raise ModelError(
                                        "model_response_too_large", "模型返回过长，已中止本次生成",
                                    )
            if self.finish_reason == "length":
                raise ModelError(
                    "model_output_truncated",
                    "模型输出被截断，请精简资料或调整 INTERVIEW_QWEN_MAX_OUTPUT_TOKENS",
                )
            content = raw.decode("utf-8", errors="ignore").strip()
            if not content:
                raise ValueError("Missing JSON content")
            self.result = output_type.model_validate_json(content)
        except ModelError:
            raise
        except (TimeoutError, httpx2.TimeoutException):
            raise ModelError("model_timeout", "Qwen 调用超时，本步未写入；可点「重试下一步」") from None
        except httpx2.HTTPError:
            raise ModelError("model_connection_failed", "无法连接 Qwen，请检查网络及接口地址") from None
        except (ValueError, ValidationError):
            raise ModelError("model_invalid_output", "模型结果未通过 JSON 结构校验，本步未保存") from None
        finally:
            self.elapsed_ms = round((time.monotonic() - started) * 1000)
            logger.info(
                "Model stream finished; provider=qwen prompt=%s schema=%s first_token_ms=%d elapsed_ms=%d",
                prompt_version, schema_version, self.first_token_ms, self.elapsed_ms,
            )
