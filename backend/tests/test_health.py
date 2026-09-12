import asyncio

from httpx2 import ASGITransport, AsyncClient, Response

from app.core.config import PlannerBackend, Settings
from app.main import create_app


async def request_health() -> Response:
    application = create_app(
        Settings(
            api_prefix="/api",
            database_url="postgresql+psycopg://aura:aura@localhost:5432/aura",
            cors_origins=["http://localhost:5173"],
            planner_backend=PlannerBackend.MOCK,
        )
    )
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get("/api/health")


def test_health_endpoint_returns_typed_service_status() -> None:
    response = asyncio.run(request_health())

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "aura-api",
        "version": "0.1.0",
    }
