"""供应商无关的结构化输出契约；具体调用由适配器实现。"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel

OutputModel = TypeVar("OutputModel", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: Literal["system", "user", "assistant"]
    content: str


class StructuredModel(Protocol):
    async def generate(
        self,
        *,
        messages: Sequence[ModelMessage],
        output_type: type[OutputModel],
        prompt_version: str,
        schema_version: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> OutputModel:
        """Return schema-validated output; adapters own provider errors and timeouts."""
        ...
