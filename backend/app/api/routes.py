from typing import Literal

from fastapi import APIRouter, status
from pydantic import BaseModel

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
