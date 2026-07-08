# Phase 1 review: Dependency, module skeleton, DB bootstrap

Verdict: clean
Diff: main...HEAD (phase-1-skeleton, 7332d2e), 4 files, +193/-2

## Blockers
none

## Major
none

## Minor
- `tests/test_store.py:42-43` The read-only reopen asserts nothing beyond "does not raise" and the schema_meta seed is never checked. Fix: on the RO connection, `assert con_ro.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0] == store.SCHEMA_VERSION` - one line covers both the seed and a real read through the RO handle.

## Nits / follow-ups
- `claude_trail_store.py:10` `STORE_PATH` reads `$CLAUDE_TRAIL_DB` at import time, so setting the env var after import has no effect. This is exactly what the spec prescribes (and the test monkeypatches the module attribute, which `connect()` reads at call time), so no change now; keep in mind if a later phase wants runtime override.
- `tests/test_store.py:8-10` Comment says "12 objects"; the set literal has 12 entries. Fine as is, just keep the comment in sync if the schema grows.

## Notes
- **DDL fidelity (the critical check):** `SCHEMA_SQL` was compared programmatically against the plan's `## Schema` block and is byte-identical after whitespace strip. All 8 tables (`schema_meta`, `raw_events`, `sessions`, `events`, `api_requests`, `file_versions`, `blobs`, `ingest_state`), all 6 indexes (`raw_session_ts`, `raw_type`, `events_session_ts`, `events_tooluse`, `api_session_ts`, `fv_path`), and all 4 views (`actions`, `session_tokens`, `command_tokens`, `request_context`) are present with the exact spec columns: `events` has `request_id` + `agent_id`, `api_requests` has the four token columns + `service_tier`, `command_tokens` uses `count(*) OVER (PARTITION BY e.request_id)`, `request_context` uses `lag(...) OVER (PARTITION BY session_id, agent_id ORDER BY ts, request_id)`.
- **connect():** duckdb import is lazy inside the function; schema execution and the `ON CONFLICT DO NOTHING` schema_meta seed run only when `not read_only`. Matches spec verbatim.
- **Lazy-import guarantee verified:** imported `claude_trail` then `claude_trail_store` and confirmed `duckdb` is absent from `sys.modules` in both cases. TUI/hook pay nothing.
- **Dispatch:** `main()` keeps the `hook` branch first, gates the six store subcommands behind a lazy `from claude_trail_store import cli_main`, and leaves the rest of `main()` (the TUI) untouched. `cli_main` not existing yet is per-plan (lands in Task 4); the import is only reached when a store subcommand is invoked.
- **Test:** uses `tmp_path` + `monkeypatch.setattr(store, "STORE_PATH", ...)`, never touches `~/.claude/trail.duckdb`; asserts the 12 expected objects via `information_schema.tables` (subset check, correct since views appear there too); reopens read-only. Follows the existing `sys.path.insert` convention from `tests/test_claude_trail.py`.
- **Scope:** exactly the 4 intended files, nothing else. `pyproject.toml` adds only `"duckdb>=1.0"`.
- **Style:** no em dash / en dash anywhere in the diff (scanned for U+2013/U+2014).
- **Verify gate:** `~/.pyenv/versions/global/bin/python -m pytest -q` -> 291 passed in 1.01s.
