import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import Request, Response
from openai import APIConnectionError, APITimeoutError, RateLimitError
from pydantic import SecretStr, ValidationError

from app.agents import AgentEngine, AgentExecutionError, LLMPlanner
from app.core.config import PlannerBackend, Settings
from app.core.constraints import PLAN_SUMMARY_MAX_LENGTH
from app.main import build_agent_engine
from app.providers import (
    OpenAIProvider,
    ProviderError,
    ProviderMessage,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    StructuredGenerationRequest,
)
from app.tools import ToolExecutor, ToolRegistry, WorkspaceListTool


def valid_plan_payload() -> dict[str, Any]:
    return {
        "summary": "Inspect the configured workspace.",
        "steps": [
            {
                "sequence": 1,
                "title": "List top-level workspace entries",
                "tool_name": "workspace_list",
                "arguments": {},
            }
        ],
    }


class RecordingProvider:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.requests: list[StructuredGenerationRequest] = []

    def generate(self, request: StructuredGenerationRequest) -> dict[str, Any]:
        self.requests.append(request)
        return deepcopy(self._payload)


class FailingProvider:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def generate(self, request: StructuredGenerationRequest) -> dict[str, Any]:
        raise self._error


class FakeResponses:
    def __init__(self, response: object) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class FakeOpenAIClient:
    def __init__(self, response: object) -> None:
        self.responses = FakeResponses(response)


def build_llm_engine(
    tmp_path: Path, provider: RecordingProvider | FailingProvider
) -> tuple[LLMPlanner, AgentEngine]:
    registry = ToolRegistry()
    registry.register(WorkspaceListTool(tmp_path))
    planner = LLMPlanner(provider, registry.definitions())
    engine = AgentEngine(
        planner,
        ToolExecutor(registry),
        planner_name="contract-test-llm",
    )
    return planner, engine


def structured_request() -> StructuredGenerationRequest:
    return StructuredGenerationRequest(
        messages=(
            ProviderMessage(role="system", content="Return a bounded plan."),
            ProviderMessage(role="user", content="List the workspace."),
        ),
        schema_name="aura_plan",
        output_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )


def test_llm_planner_accepts_valid_structured_output_and_limits_context(
    tmp_path: Path,
) -> None:
    provider = RecordingProvider(valid_plan_payload())
    planner, engine = build_llm_engine(tmp_path, provider)

    plan = engine.create_plan("  List this workspace.  ")

    assert plan.planner == "contract-test-llm"
    assert plan.steps[0].tool_name == "workspace_list"
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.messages[1].content == "List this workspace."
    assert '"name":"workspace_list"' in request.messages[0].content
    assert str(tmp_path) not in request.messages[0].content
    assert request.output_schema["properties"]["steps"]["maxItems"] == 1
    assert request.output_schema["properties"]["steps"]["items"]["properties"]["tool_name"][
        "enum"
    ] == ["workspace_list"]
    assert planner.create_plan("List this workspace.").steps[0].arguments == {}


@pytest.mark.parametrize(
    "payload",
    [
        {"steps": []},
        {
            "summary": "Two steps are forbidden.",
            "steps": valid_plan_payload()["steps"] * 2,
        },
        {
            "summary": "x" * (PLAN_SUMMARY_MAX_LENGTH + 1),
            "steps": valid_plan_payload()["steps"],
        },
        {
            **valid_plan_payload(),
            "unexpected": "field",
        },
    ],
)
def test_llm_planner_rejects_malformed_extra_and_oversized_output(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    planner, _ = build_llm_engine(tmp_path, RecordingProvider(payload))

    with pytest.raises(ValidationError):
        planner.create_plan("List this workspace.")


def test_engine_rejects_unknown_tool_selection(tmp_path: Path) -> None:
    payload = valid_plan_payload()
    payload["steps"][0]["tool_name"] = "shell"
    _, engine = build_llm_engine(tmp_path, RecordingProvider(payload))

    with pytest.raises(AgentExecutionError) as error:
        engine.create_plan("List this workspace.")

    assert error.value.stage == "plan_validation"
    assert str(error.value) == "The generated plan was rejected."


def test_engine_rejects_invalid_arguments_before_execution(tmp_path: Path) -> None:
    payload = valid_plan_payload()
    payload["steps"][0]["arguments"] = {"path": "../outside"}
    _, engine = build_llm_engine(tmp_path, RecordingProvider(payload))

    with pytest.raises(AgentExecutionError) as error:
        engine.create_plan("List another path.")

    assert error.value.stage == "plan_validation"
    assert str(error.value) == "The generated plan was rejected."


@pytest.mark.parametrize(
    "provider_error",
    [
        ProviderTimeoutError("safe timeout"),
        ProviderUnavailableError("safe service failure"),
    ],
)
def test_engine_handles_provider_timeout_and_service_errors_predictably(
    tmp_path: Path, provider_error: Exception
) -> None:
    _, engine = build_llm_engine(tmp_path, FailingProvider(provider_error))

    with pytest.raises(AgentExecutionError) as error:
        engine.create_plan("List this workspace.")

    assert error.value.stage == "planner"
    assert str(error.value) == "The planner could not create a plan."


def test_openai_provider_sends_strict_stateless_bounded_request() -> None:
    payload = valid_plan_payload()
    client = FakeOpenAIClient(SimpleNamespace(status="completed", output_text=json.dumps(payload)))
    provider = OpenAIProvider(
        api_key=SecretStr("not-a-real-key"),
        model="gpt-5-mini",
        timeout_seconds=7,
        max_output_tokens=256,
        client=client,
    )

    result = provider.generate(structured_request())

    assert result == payload
    assert len(client.responses.calls) == 1
    call = client.responses.calls[0]
    assert call["model"] == "gpt-5-mini"
    assert call["store"] is False
    assert call["timeout"] == 7
    assert call["max_output_tokens"] == 256
    assert call["text"] == {
        "format": {
            "type": "json_schema",
            "name": "aura_plan",
            "schema": structured_request().output_schema,
            "strict": True,
        }
    }


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(status="incomplete", output_text="{}"),
        SimpleNamespace(status="completed", output_text="not-json"),
        SimpleNamespace(status="completed", output_text="[]"),
        SimpleNamespace(status="completed", output_text=""),
    ],
)
def test_openai_provider_rejects_incomplete_or_malformed_responses(response: object) -> None:
    provider = OpenAIProvider(
        api_key="not-a-real-key",
        model="gpt-5-mini",
        timeout_seconds=7,
        max_output_tokens=256,
        client=FakeOpenAIClient(response),
    )

    with pytest.raises(ProviderResponseError):
        provider.generate(structured_request())


@pytest.mark.parametrize(
    ("sdk_error", "expected_error"),
    [
        (
            APITimeoutError(Request("POST", "https://api.openai.com/v1/responses")),
            ProviderTimeoutError,
        ),
        (
            APIConnectionError(request=Request("POST", "https://api.openai.com/v1/responses")),
            ProviderUnavailableError,
        ),
        (
            RateLimitError(
                "rate limited",
                response=Response(
                    429,
                    request=Request("POST", "https://api.openai.com/v1/responses"),
                ),
                body=None,
            ),
            ProviderUnavailableError,
        ),
    ],
)
def test_openai_provider_maps_sdk_failures_to_sanitized_errors(
    sdk_error: Exception, expected_error: type[ProviderError]
) -> None:
    provider = OpenAIProvider(
        api_key="not-a-real-key",
        model="gpt-5-mini",
        timeout_seconds=7,
        max_output_tokens=256,
        client=FakeOpenAIClient(sdk_error),
    )

    with pytest.raises(expected_error) as error:
        provider.generate(structured_request())

    assert "not-a-real-key" not in str(error.value)


def test_application_composition_selects_llm_planner_without_engine_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = RecordingProvider(valid_plan_payload())

    def provider_factory(**kwargs: object) -> RecordingProvider:
        return provider

    monkeypatch.setattr("app.main.OpenAIProvider", provider_factory)
    engine = build_agent_engine(
        Settings(
            database_url="sqlite+pysqlite:///:memory:",
            workspace_root=tmp_path,
            planner_backend=PlannerBackend.OPENAI,
            openai_api_key=SecretStr("not-a-real-key"),
        )
    )

    plan = engine.create_plan("List this workspace.")

    assert plan.planner == "openai-gpt-5-mini"
    assert plan.steps[0].tool_name == "workspace_list"
    assert len(provider.requests) == 1
