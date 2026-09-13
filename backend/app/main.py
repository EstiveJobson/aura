import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session, sessionmaker

from app.agents import AgentEngine, LLMPlanner, MockPlanner, Planner
from app.api.routes import router as api_router
from app.core.config import PlannerBackend, Settings, get_settings
from app.database.ownership import (
    ExecutionOwnership,
    PostgreSQLExecutionOwnership,
    session_factory_engine,
)
from app.database.session import create_database_engine, create_session_factory
from app.providers import OpenAIProvider
from app.services import TaskService
from app.tools import (
    ToolExecutor,
    ToolRegistry,
    WorkspaceListTool,
    WorkspaceMoveTool,
    WorkspaceReadTool,
    WorkspaceSearchTool,
)
from app.tools.workspace_access import build_workspace_write_access

logger = logging.getLogger(__name__)


def build_agent_engine(settings: Settings) -> AgentEngine:
    registry = ToolRegistry()
    registry.register(WorkspaceListTool(settings.workspace_root))
    registry.register(WorkspaceSearchTool(settings.workspace_root))
    registry.register(WorkspaceReadTool(settings.workspace_root))
    registry.register(
        WorkspaceMoveTool(
            settings.workspace_root,
            build_workspace_write_access(
                settings.workspace_root,
                settings.workspace_write_mode,
            ),
        )
    )
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
    execution_ownership: ExecutionOwnership | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    if session_factory is None:
        database_engine = create_database_engine(app_settings.database_url)
        session_factory = create_session_factory(database_engine)
    resolved_session_factory = session_factory
    resolved_agent_engine = agent_engine or build_agent_engine(app_settings)

    application = FastAPI(
        title=app_settings.app_name,
        version="0.1.0",
        description="AURA agentic workspace API",
    )
    application.state.execution_ownership = execution_ownership

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        acquired_ownership: PostgreSQLExecutionOwnership | None = None
        owner = execution_ownership
        if owner is None:
            database_engine = session_factory_engine(resolved_session_factory.kw.get("bind"))
            acquired_ownership = PostgreSQLExecutionOwnership.acquire(database_engine)
            owner = acquired_ownership
        owner.assert_owned()
        application.state.execution_ownership = owner
        try:
            with resolved_session_factory() as session:
                report = TaskService(
                    session,
                    resolved_agent_engine,
                    owner,
                ).reconcile_stranded_execution_batch()
            if report.reconciled:
                logger.warning(
                    "reconciled stranded executions count=%s remaining=%s",
                    report.reconciled,
                    report.remaining,
                )
            if report.remaining:
                logger.warning(
                    "stranded execution reconciliation backlog remains; "
                    "run the bounded continuation command"
                )
            yield
        finally:
            application.state.execution_ownership = None
            if acquired_ownership is not None:
                acquired_ownership.release()

    application.router.lifespan_context = lifespan
    application.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["Content-Type", "X-AURA-Decision"],
    )
    application.state.session_factory = resolved_session_factory
    application.state.agent_engine = resolved_agent_engine
    application.state.cors_origins = frozenset(app_settings.cors_origins)
    application.include_router(api_router, prefix=app_settings.api_prefix)
    return application


app = create_app()
