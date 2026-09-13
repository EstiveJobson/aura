from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Header, Request, status
from pydantic import BaseModel
from starlette.responses import JSONResponse

from app.api.dependencies import TaskServiceDependency
from app.api.schemas import ErrorResponse, TaskCreate, TaskResponse
from app.services import (
    TaskNotFoundError,
    TaskPersistenceError,
    TaskStateConflictError,
)

router = APIRouter()
DECISION_HEADER_NAME = "X-AURA-Decision"
DecisionHeader = Annotated[str | None, Header(alias=DECISION_HEADER_NAME)]


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
    summary="Create, plan, and conditionally execute one task",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse}},
)
def create_task(payload: TaskCreate, service: TaskServiceDependency) -> TaskResponse | JSONResponse:
    try:
        task = service.create_and_execute(payload.instruction)
    except TaskPersistenceError as exc:
        error = ErrorResponse(code=exc.code, message=exc.message)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=error.model_dump(),
        )
    return TaskResponse.model_validate(task)


@router.get(
    "/tasks/{task_id}",
    response_model=TaskResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    },
    tags=["tasks"],
    summary="Get persisted task execution status",
)
def get_task(task_id: UUID, service: TaskServiceDependency) -> TaskResponse | JSONResponse:
    try:
        task = service.get(task_id)
    except TaskPersistenceError as exc:
        error = ErrorResponse(code=exc.code, message=exc.message)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=error.model_dump(),
        )
    if task is None:
        error = ErrorResponse(code="task_not_found", message="Task was not found.")
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content=error.model_dump())
    return TaskResponse.model_validate(task)


def _decision_error_response(error: TaskNotFoundError | TaskStateConflictError) -> JSONResponse:
    error_body = ErrorResponse(code=error.code, message=error.message)
    response_status = (
        status.HTTP_404_NOT_FOUND
        if isinstance(error, TaskNotFoundError)
        else status.HTTP_409_CONFLICT
    )
    return JSONResponse(status_code=response_status, content=error_body.model_dump())


def _validate_decision_request(
    request: Request,
    decision_header: str | None,
    expected_decision: Literal["approve", "reject"],
) -> JSONResponse | None:
    origin = request.headers.get("origin")
    trusted_origins = cast(frozenset[str], request.app.state.cors_origins)
    if origin is not None and origin not in trusted_origins:
        error = ErrorResponse(
            code="decision_origin_rejected",
            message="Decision request origin is not allowed.",
        )
        return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content=error.model_dump())
    if decision_header != expected_decision:
        error = ErrorResponse(
            code="decision_header_invalid",
            message="Decision request header is missing or invalid.",
        )
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content=error.model_dump())
    return None


@router.post(
    "/tasks/{task_id}/approve",
    response_model=TaskResponse,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorResponse},
        status.HTTP_403_FORBIDDEN: {"model": ErrorResponse},
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_409_CONFLICT: {"model": ErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    },
    tags=["tasks"],
    summary="Approve and execute one persisted pending action",
)
def approve_task(
    task_id: UUID,
    request: Request,
    service: TaskServiceDependency,
    decision_header: DecisionHeader = None,
) -> TaskResponse | JSONResponse:
    request_error = _validate_decision_request(request, decision_header, "approve")
    if request_error is not None:
        return request_error
    try:
        task = service.approve(task_id)
    except (TaskNotFoundError, TaskStateConflictError) as exc:
        return _decision_error_response(exc)
    except TaskPersistenceError as exc:
        error = ErrorResponse(code=exc.code, message=exc.message)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=error.model_dump(),
        )
    return TaskResponse.model_validate(task)


@router.post(
    "/tasks/{task_id}/reject",
    response_model=TaskResponse,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorResponse},
        status.HTTP_403_FORBIDDEN: {"model": ErrorResponse},
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_409_CONFLICT: {"model": ErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    },
    tags=["tasks"],
    summary="Reject one persisted pending action",
)
def reject_task(
    task_id: UUID,
    request: Request,
    service: TaskServiceDependency,
    decision_header: DecisionHeader = None,
) -> TaskResponse | JSONResponse:
    request_error = _validate_decision_request(request, decision_header, "reject")
    if request_error is not None:
        return request_error
    try:
        task = service.reject(task_id)
    except (TaskNotFoundError, TaskStateConflictError) as exc:
        return _decision_error_response(exc)
    except TaskPersistenceError as exc:
        error = ErrorResponse(code=exc.code, message=exc.message)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=error.model_dump(),
        )
    return TaskResponse.model_validate(task)
