from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self

from sqlalchemy.orm import Session, sessionmaker

from goliath.db.repositories import AgentJobRepository, AuditEventRepository


class JobUnitOfWork(Protocol):
    jobs: AgentJobRepository
    audits: AuditEventRepository

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class SqlAlchemyJobUnitOfWork:
    """Commits job transitions and their audit events as one database transaction."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None

    def __enter__(self) -> Self:
        self._session = self._session_factory()
        self.session = self._session
        self.pending_submissions = []
        self.jobs = AgentJobRepository(self._session)
        self.audits = AuditEventRepository(self._session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        assert self._session is not None
        try:
            if exc_type is None:
                self._session.commit()
            else:
                self._session.rollback()
        finally:
            self._session.close()
        return None
