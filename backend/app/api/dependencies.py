from collections.abc import Iterator
from typing import Annotated, cast

from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from app.agents import AgentEngine
from app.database.ownership import ExecutionOwnership
from app.services import TaskService


def get_database_session(request: Request) -> Iterator[Session]:
    session_factory = cast(sessionmaker[Session], request.app.state.session_factory)
    with session_factory() as session:
        yield session


DatabaseSession = Annotated[Session, Depends(get_database_session)]


def get_task_service(request: Request, session: DatabaseSession) -> TaskService:
    agent_engine = cast(AgentEngine, request.app.state.agent_engine)
    ownership = cast(ExecutionOwnership, request.app.state.execution_ownership)
    return TaskService(session, agent_engine, ownership)


TaskServiceDependency = Annotated[TaskService, Depends(get_task_service)]
