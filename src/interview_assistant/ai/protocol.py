"""Minimal structured-output contract; no provider SDK or network calls."""

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
    ) -> OutputModel:
        """Return schema-validated output; adapters own provider errors and timeouts."""
        ...
