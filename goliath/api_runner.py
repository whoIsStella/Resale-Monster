from __future__ import annotations

import uvicorn

from goliath.api import create_app
from goliath.cli import build_agent_registry
from goliath.db.database import build_session_factory, create_production_engine
from goliath.orchestration.service import JobOrchestrationService
from goliath.orchestration.uow import SqlAlchemyJobUnitOfWork


def main() -> None:
    config, registry = build_agent_registry()
    engine = create_production_engine()
    factory = build_session_factory(engine)
    service = JobOrchestrationService(
        uow_factory=lambda: SqlAlchemyJobUnitOfWork(factory), registry=registry, config=config
    )
    app = create_app(service=service, registry=registry, session_factory=factory, config=config)
    uvicorn.run(app, host=config.api_host, port=config.api_port)


if __name__ == "__main__":
    main()
