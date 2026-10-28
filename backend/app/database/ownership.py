from __future__ import annotations

from typing import Protocol

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

EXECUTION_OWNER_LOCK_ID = 0x41555241


class BackendOwnershipError(RuntimeError):
    """The process cannot safely own synchronous execution and reconciliation."""


class ExecutionOwnership(Protocol):
    def assert_owned(self) -> None: ...


class PostgreSQLExecutionOwnership:
    """One session-scoped PostgreSQL advisory lock held for the process lifetime."""

    def __init__(self, connection: Connection, backend_pid: int) -> None:
        self._connection = connection
        self._backend_pid = backend_pid
        self._released = False

    @classmethod
    def acquire(cls, engine: Engine) -> PostgreSQLExecutionOwnership:
        if engine.dialect.name != "postgresql":
            raise BackendOwnershipError("Synchronous execution ownership requires PostgreSQL.")

        connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            acquired = connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtext(current_database()), :lock_id)"),
                {"lock_id": EXECUTION_OWNER_LOCK_ID},
            )
            if acquired is not True:
                raise BackendOwnershipError(
                    "Another active AURA backend already owns synchronous execution."
                )
            backend_pid = connection.scalar(text("SELECT pg_backend_pid()"))
            if not isinstance(backend_pid, int):
                raise BackendOwnershipError("PostgreSQL execution ownership could not be verified.")
            return cls(connection, backend_pid)
        except Exception:
            connection.close()
            raise

    def assert_owned(self) -> None:
        if self._released or self._connection.closed:
            raise BackendOwnershipError("AURA backend execution ownership has been released.")
        try:
            current_pid = self._connection.scalar(text("SELECT pg_backend_pid()"))
        except SQLAlchemyError as exc:
            raise BackendOwnershipError(
                "AURA backend execution ownership could not be verified."
            ) from exc
        if current_pid != self._backend_pid:
            raise BackendOwnershipError("AURA backend execution ownership was lost.")

    def release(self) -> None:
        if self._released:
            return
        try:
            if not self._connection.closed:
                self._connection.scalar(
                    text("SELECT pg_advisory_unlock(hashtext(current_database()), :lock_id)"),
                    {"lock_id": EXECUTION_OWNER_LOCK_ID},
                )
        finally:
            self._released = True
            self._connection.close()

    def __enter__(self) -> PostgreSQLExecutionOwnership:
        self.assert_owned()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def session_factory_engine(bind: object) -> Engine:
    if not isinstance(bind, Engine):
        raise BackendOwnershipError("AURA backend ownership requires a database engine.")
    return bind
