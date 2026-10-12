"""Optional real MySQL check: SQLite cannot expose cross-table collation failures."""
import os
import pytest
from sqlalchemy import create_engine, select, text
from app.memory.tables import MemoryEpisodeRow, MemorySourceRow, MemoryItemRow


@pytest.mark.skipif(not os.getenv("MEMORY_TEST_MYSQL_URL"), reason="dedicated development/test MySQL not configured")
def test_source_corpus_cross_table_comparisons_are_supported():
    engine = create_engine(os.environ["MEMORY_TEST_MYSQL_URL"], connect_args={"connect_timeout":5})
    try:
        with engine.connect() as conn:
            database = conn.execute(text("SELECT DATABASE()")).scalar()
            assert database == "qa_agent_dev" or "test" in database.lower(), "development/test database required"
            linked = select(MemorySourceRow.turn_id).join(MemoryItemRow,
                MemorySourceRow.memory_id == MemoryItemRow.id).where(MemoryItemRow.status == "active")
            # This read-only join raises error 1267 even for an empty corpus with mixed collations.
            conn.execute(select(MemoryEpisodeRow.id).where(
                MemoryEpisodeRow.turn_id.in_(linked), MemoryEpisodeRow.scope == "eval:collation-check").limit(1)).all()
    finally:
        engine.dispose()
