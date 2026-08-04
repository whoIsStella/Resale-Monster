from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from goliath.db import models  # noqa: F401
from goliath.db.base import Base
from goliath.db.database import create_test_engine


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_test_engine()
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
        database_session.rollback()
    Base.metadata.drop_all(engine)
    engine.dispose()
