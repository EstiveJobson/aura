from typing import Literal
from uuid import UUID

from fastapi import APIRouter, status
from pydantic import BaseModel
from starlette.responses import JSONResponse

from app.api.dependencies import TaskServiceDependency
from app.api.schemas import ErrorResponse, TaskCreate, TaskResponse

router = APIRouter()


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str


@router.get(
    "/health",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    tags=["system"],
    summary="Check API liveness",
)
def health() -> HealthResponse:
    return HealthResponse(status="ok", service="aura-api", version="0.1.0")


@router.post(
    "/tasks",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["tasks"],
    summary="Create and execute a deterministic task",
)
def create_task(payload: TaskCreate, service: TaskServiceDependency) -> TaskResponse:
    task = service.create_and_execute(payload.instruction)
    return TaskResponse.model_validate(task)


@router.get(
    "/tasks/{task_id}",
    response_model=TaskResponse,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
    tags=["tasks"],
    summary="Get persisted task execution status",
)
def get_task(task_id: UUID, service: TaskServiceDependency) -> TaskResponse | JSONResponse:
    task = service.get(task_id)
    if task is None:
        error = ErrorResponse(code="task_not_found", message="Task was not found.")
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content=error.model_dump())
    return TaskResponse.model_validate(task)
