from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)
from pydantic import BaseModel, ConfigDict, Field, RootModel, SecretStr, ValidationError

from app.providers.base import (
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    StructuredGenerationRequest,
)

PROVIDER_RESPONSE_MAX_CHARACTERS = 16_384


class _OpenAIProviderOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=40)
    timeout_seconds: float = Field(ge=1, le=60)
    max_output_tokens: int = Field(ge=128, le=2_048)


class _StructuredPayload(RootModel[dict[str, Any]]):
    pass


class OpenAIProvider:
    """OpenAI Responses API adapter; credentials and SDK types stop here."""

    def __init__(
        self,
        *,
        api_key: SecretStr | str,
        model: str,
        timeout_seconds: float,
        max_output_tokens: int,
        client: Any | None = None,
    ) -> None:
        options = _OpenAIProviderOptions(
            model=model,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
        )
        secret = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if not secret.strip():
            raise ValueError("An OpenAI API key is required for the OpenAI provider.")

        self._model = options.model
        self._timeout_seconds = options.timeout_seconds
        self._max_output_tokens = options.max_output_tokens
        self._client: Any = client or OpenAI(
            api_key=secret,
            timeout=self._timeout_seconds,
            max_retries=0,
        )

    def generate(self, request: StructuredGenerationRequest) -> dict[str, Any]:
        try:
            response = self._client.responses.create(
                model=self._model,
                input=[message.model_dump(mode="json") for message in request.messages],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": request.schema_name,
                        "schema": request.output_schema,
                        "strict": True,
                    }
                },
                max_output_tokens=self._max_output_tokens,
                store=False,
                timeout=self._timeout_seconds,
            )
        except APITimeoutError as exc:
            raise ProviderTimeoutError("The AI provider request timed out.") from exc
        except RateLimitError as exc:
            raise ProviderUnavailableError("The AI provider is rate limited.") from exc
        except (APIConnectionError, APIStatusError, OpenAIError) as exc:
            raise ProviderUnavailableError("The AI provider is unavailable.") from exc

        if getattr(response, "status", None) != "completed":
            raise ProviderResponseError("The AI provider response was incomplete.")
        output_text = getattr(response, "output_text", None)
        if not isinstance(output_text, str) or not output_text:
            raise ProviderResponseError("The AI provider returned no structured output.")
        if len(output_text) > PROVIDER_RESPONSE_MAX_CHARACTERS:
            raise ProviderResponseError("The AI provider response exceeded the allowed size.")

        try:
            return _StructuredPayload.model_validate_json(output_text).root
        except ValidationError as exc:
            raise ProviderResponseError("The AI provider returned malformed output.") from exc
