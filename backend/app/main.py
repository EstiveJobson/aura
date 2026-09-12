from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session, sessionmaker

from app.agents import AgentEngine, MockPlanner
from app.api.routes import router as api_router
from app.core.config import Settings, get_settings
from app.database.session import create_database_engine, create_session_factory
from app.tools import ToolExecutor, ToolRegistry, WorkspaceListTool


def build_agent_engine(settings: Settings) -> AgentEngine:
    registry = ToolRegistry()
    registry.register(WorkspaceListTool(settings.workspace_root))
    return AgentEngine(
        MockPlanner(),
        ToolExecutor(registry),
        planner_name="mock-planner-v1",
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
