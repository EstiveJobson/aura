# AURA

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.13](https://img.shields.io/badge/Python-3.13-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![React](https://img.shields.io/badge/React-TypeScript-149ECA.svg?logo=react&logoColor=white)](https://react.dev/)

AURA is an agentic workspace and AI orchestration platform conceived as a portfolio-grade software engineering project. The repository is currently at **Phase 2: real AI provider**: a user can create a task, run one bounded read-only agent workflow with either the deterministic planner or an OpenAI-backed planner, and inspect its persisted result.

The project scope and implementation order are governed by [`AURA_Project_Blueprint_A3.pdf`](AURA_Project_Blueprint_A3.pdf).

## Phase 2 capabilities

- FastAPI application with a typed `GET /api/health` endpoint and OpenAPI documentation.
- Typed `POST /api/tasks` and `GET /api/tasks/{task_id}` endpoints for the synchronous task flow.
- Deterministic `MockPlanner` for tests and local development, selected by default.
- Vendor-neutral `AIProvider` contract and an OpenAI Responses API adapter selected through environment configuration.
- `LLMPlanner` that requests strict structured output and locally validates one plan step, registered tool identity, field bounds, and tool arguments before persistence or execution.
- Agent engine, tool interface, registry, executor, and exactly one registered `workspace_list` tool.
- Read-only workspace listing constrained to the configured `WORKSPACE_ROOT`.
- PostgreSQL persistence for tasks, plans, executions, tool calls, statuses, and results through SQLAlchemy and Alembic.
- React, TypeScript, and Vite task execution UI with operational status, plan, tool, and result details.
- Reproducible local services through Docker Compose.
- Backend linting, formatting, static typing, tests, and coverage enforcement.
- Frontend linting, formatting, static typing, tests, and production builds.
- GitHub Actions checks for every push and pull request.

Additional tools, approvals, authentication, memory, multiple agents, streaming, autonomous loops, arbitrary filesystem access, and destructive actions remain intentionally out of scope.

## Architecture

Phase 2 preserves the modular monolith and the existing end-to-end execution path:

```mermaid
flowchart LR
    UI[React task UI] --> API[FastAPI task API]
    API --> SERVICE[Task Service]
    SERVICE --> ENGINE[Agent Engine]
    ENGINE --> PLANNER[Planner contract]
    PLANNER --> MOCK[MockPlanner]
    PLANNER --> LLM[LLMPlanner]
    LLM --> PROVIDER[AIProvider]
    PROVIDER --> OPENAI[OpenAI Responses API]
    ENGINE --> REGISTRY[Tool Registry]
    REGISTRY --> TOOL[workspace_list]
    SERVICE --> DB[(PostgreSQL)]
    MIG[Alembic] --> DB
```

Backend packages mirror the target architecture under `backend/app/`; placeholder packages contain no speculative implementation.

## Run with Docker Compose

Requirements: Docker with the Compose plugin.

```powershell
Copy-Item .env.example .env
docker compose up --build
```

The backend applies pending Alembic migrations before starting the API. The configured container workspace is `/app`, so `workspace_list` lists the backend application workspace without accepting arbitrary paths.

The local development defaults in `.env.example` are not production credentials. Change them for any shared environment.

- Frontend: <http://localhost:5173>
- API health: <http://localhost:8000/api/health>
- API documentation: <http://localhost:8000/docs>

Stop the stack with `docker compose down`. Add `--volumes` only when you explicitly want to delete the local PostgreSQL data volume.

## Run without containers

Use Python 3.13, Node.js 22 or newer, and an accessible PostgreSQL instance.

```powershell
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r backend/requirements-dev.txt
Set-Location backend
alembic upgrade head
uvicorn app.main:app --reload
```

In a second terminal:

```powershell
Set-Location frontend
npm ci
npm run dev
```

Copy `.env.example` to `.env` before starting the backend and adjust `DATABASE_URL` when PostgreSQL is not on `localhost:5432`.

The backend always loads the repository-root `.env`, matching the documented copy location. The example uses `WORKSPACE_ROOT=..` because the local backend command runs from `backend/`; Docker Compose overrides it with `/app`.

`PLANNER_BACKEND=mock` is the default and requires no provider credentials. To use the real provider, set these values in the untracked root `.env` before starting the backend:

```dotenv
PLANNER_BACKEND=openai
OPENAI_API_KEY=<your-api-key>
OPENAI_MODEL=gpt-5-mini
```

Provider requests have a configurable 1-60 second deadline, no SDK retries, a bounded output-token budget, strict structured output, and `store=false`. Startup fails with a sanitized configuration error when the OpenAI planner is selected without a key.

## Quality checks

```powershell
Set-Location backend
ruff check .
ruff format --check .
mypy app tests
pytest

Set-Location ../frontend
npm run format:check
npm run lint
npm run type-check
npm test
npm run build
```

Backend coverage is enforced at 80%. Frontend tests use Vitest. The automated suite uses deterministic provider doubles and never calls the real provider.

## Environment variables

| Variable            | Purpose                                           |
| ------------------- | ------------------------------------------------- |
| `POSTGRES_DB`       | Local PostgreSQL database name used by Compose.   |
| `POSTGRES_USER`     | Local PostgreSQL user used by Compose.            |
| `POSTGRES_PASSWORD` | Local PostgreSQL password used by Compose.        |
| `DATABASE_URL`      | SQLAlchemy PostgreSQL connection URL.             |
| `CORS_ORIGINS`      | JSON array of browser origins allowed by FastAPI. |
| `WORKSPACE_ROOT`    | Fixed directory available to the read-only tool.  |
| `PLANNER_BACKEND`   | `mock` (default) or `openai`.                      |
| `OPENAI_API_KEY`    | Required only when `PLANNER_BACKEND=openai`.       |
| `OPENAI_MODEL`      | OpenAI model used for structured planning.         |
| `AI_PROVIDER_TIMEOUT_SECONDS` | Provider deadline from 1 through 60 seconds. |
| `AI_PROVIDER_MAX_OUTPUT_TOKENS` | Structured response budget from 128 through 2048 tokens. |
| `VITE_API_BASE_URL` | Browser-visible base URL for the API.             |

## Roadmap

- **Phase 0 - Foundation:** complete; repository, docs, Docker, application skeletons, PostgreSQL, and CI.
- **Phase 1 - MVP vertical slice:** complete; create task, deterministic mock plan, one tool, result persistence, and dashboard display.
- **Phase 2 - Real AI provider:** complete; switchable provider abstraction and validated single-step structured planning.
- **Phases 3-5:** approvals, broader tooling, memory, observability, and portfolio polish.

See [Architecture](docs/architecture.md) for boundaries and [Contributing](CONTRIBUTING.md) for the development workflow.

## License

Distributed under the [MIT License](LICENSE).
