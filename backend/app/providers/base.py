from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class ProviderMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["system", "user"]
    content: str = Field(min_length=1, max_length=16_000)


class StructuredGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    messages: tuple[ProviderMessage, ...] = Field(min_length=1, max_length=4)
    schema_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    output_schema: dict[str, Any]


class AIProvider(Protocol):
    def generate(self, request: StructuredGenerationRequest) -> dict[str, Any]: ...


class ProviderError(RuntimeError):
    """Sanitized base error for failures at the external provider boundary."""


class ProviderTimeoutError(ProviderError):
    """The provider did not respond within the configured deadline."""


class ProviderUnavailableError(ProviderError):
    """The provider could not service the request."""


class ProviderResponseError(ProviderError):
    """The provider returned an unusable structured response."""
