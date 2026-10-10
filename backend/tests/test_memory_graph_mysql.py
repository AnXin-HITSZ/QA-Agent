"""Opt-in graph schema verification on a dedicated migrated MySQL database."""
import os
import pytest
from sqlalchemy import create_engine, inspect
from app.memory.graph.tables import Base


def test_real_graph_schema():
    url = os.environ.get("MEMORY_TEST_MYSQL_URL")
    if not url:
        pytest.skip("MEMORY_TEST_MYSQL_URL not configured")
    engine = create_engine(url, connect_args={"connect_timeout": 5})
    try:
        inspector = inspect(engine)
        names = set(inspector.get_table_names())
        for table in Base.metadata.sorted_tables:
            assert table.name in names
            columns = {c["name"] for c in inspector.get_columns(table.name)}
            assert {c.name for c in table.columns} <= columns
            assert inspector.get_pk_constraint(table.name)["constrained_columns"] == [
                c.name for c in table.primary_key.columns]
    finally:
        engine.dispose()
