from __future__ import annotations

import argparse
import json

from app.core.config import get_settings
from app.database.ownership import BackendOwnershipError, PostgreSQLExecutionOwnership
from app.database.session import create_database_engine, create_session_factory
from app.main import build_agent_engine
from app.services import TaskService
from app.services.tasks import STRANDED_RECONCILIATION_LIMIT


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile one bounded batch while holding AURA backend ownership."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=STRANDED_RECONCILIATION_LIMIT,
        choices=range(1, STRANDED_RECONCILIATION_LIMIT + 1),
    )
    arguments = parser.parse_args()
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with PostgreSQLExecutionOwnership.acquire(engine) as ownership:
            with session_factory() as session:
                report = TaskService(
                    session,
                    build_agent_engine(settings),
                    ownership,
                ).reconcile_stranded_execution_batch(limit=arguments.limit)
        print(
            json.dumps(
                {
                    "reconciled": report.reconciled,
                    "remaining": report.remaining,
                }
            )
        )
    except BackendOwnershipError as exc:
        parser.exit(1, f"AURA reconciliation refused: {exc}\n")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
