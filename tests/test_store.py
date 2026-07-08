import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import claude_trail_store as store

# The 12 objects the schema bootstrap must create: 8 tables + 4 views. Both show
# up in information_schema.tables (views included), so one membership check
# covers the whole schema.
EXPECTED_OBJECTS = {
    "actions",
    "api_requests",
    "blobs",
    "command_tokens",
    "events",
    "file_versions",
    "ingest_state",
    "raw_events",
    "request_context",
    "schema_meta",
    "session_tokens",
    "sessions",
}


def test_schema_bootstraps_and_reopens_read_only(tmp_path, monkeypatch):
    """connect() creates the full schema, then the file reopens read-only.

    STORE_PATH is read from $CLAUDE_TRAIL_DB at import, so point it at a temp
    file via monkeypatch (connect() reads the module attribute at call time).
    This is the automated form of the plan's Step 1.4 bootstrap check.
    """
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")

    con = store.connect()
    try:
        names = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables").fetchall()}
    finally:
        con.close()
    assert EXPECTED_OBJECTS <= names

    con_ro = store.connect(read_only=True)
    con_ro.close()
