from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session, sessionmaker

from app.agents import AgentEngine, LLMPlanner, MockPlanner, Planner
from app.api.routes import router as api_router
from app.core.config import PlannerBackend, Settings, get_settings
from app.database.session import create_database_engine, create_session_factory
from app.providers import OpenAIProvider
from app.tools import ToolExecutor, ToolRegistry, WorkspaceListTool


def build_agent_engine(settings: Settings) -> AgentEngine:
    registry = ToolRegistry()
    registry.register(WorkspaceListTool(settings.workspace_root))
    planner: Planner
    planner_name: str
    if settings.planner_backend is PlannerBackend.MOCK:
        planner = MockPlanner()
        planner_name = "mock-planner-v1"
    else:
        if settings.openai_api_key is None:
            raise ValueError("OpenAI provider configuration is incomplete.")
        provider = OpenAIProvider(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            timeout_seconds=settings.ai_provider_timeout_seconds,
            max_output_tokens=settings.ai_provider_max_output_tokens,
        )
        planner = LLMPlanner(provider, registry.definitions())
        planner_name = f"openai-{settings.openai_model}"
    return AgentEngine(
        planner,
        ToolExecutor(registry),
        planner_name=planner_name,
    )


def create_app(
    settings: Settings | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
    agent_engine: AgentEngine | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    if session_factory is None:
        database_engine = create_database_engine(app_settings.database_url)
        session_factory = create_session_factory(database_engine)

    application = FastAPI(
        title=app_settings.app_name,
        version="0.1.0",
        description="AURA agentic workspace API",
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.state.session_factory = session_factory
    application.state.agent_engine = agent_engine or build_agent_engine(app_settings)
    application.include_router(api_router, prefix=app_settings.api_prefix)
    return application


app = create_app()
