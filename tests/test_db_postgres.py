"""Integração com PostgreSQL real (defina ORQ_TEST_POSTGRES_URL para rodar).

Exemplo::

    docker run -d -p 5433:5432 -e POSTGRES_PASSWORD=teste postgres:16
    export ORQ_TEST_POSTGRES_URL=postgresql+psycopg://postgres:teste@localhost:5433/postgres
    pytest -m postgres
"""

from __future__ import annotations

import os
import uuid

import pytest

from orquestrador.db import Database, NewLogLine, RunRepository, RunStatus
from orquestrador.db.migrations import current_revision, head_revision, upgrade_database
from orquestrador.execution import Status
from tests.test_db_repository import BASE_TIME, completed, make_request, make_result

POSTGRES_URL = os.environ.get("ORQ_TEST_POSTGRES_URL")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not POSTGRES_URL, reason="ORQ_TEST_POSTGRES_URL não definida"),
]


def test_postgres_roundtrip() -> None:
    assert POSTGRES_URL is not None
    upgrade_database(POSTGRES_URL)
    assert current_revision(POSTGRES_URL) == head_revision()

    database = Database(POSTGRES_URL)
    repository = RunRepository(database)
    run_id = f"pg-{uuid.uuid4().hex[:12]}"
    try:
        repository.create_run(make_request(run_id))
        ids = repository.register_pipeline(run_id, make_result(run_id))
        repository.append_logs(
            [NewLogLine(ids.steps[("build", 0)], 1, "stdout", "olá do postgres", BASE_TIME)]
        )
        repository.finish_run(completed(run_id, Status.SUCCESS))

        run = repository.get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.SUCCESS
        assert run.created_at == BASE_TIME
        assert [line.text for line in repository.get_logs(run_id, "build", 0) or []] == [
            "olá do postgres"
        ]
        assert run_id in [item.run_id for item in repository.list_runs(limit=200).items]
    finally:
        repository.delete_run(run_id)
        database.dispose()
