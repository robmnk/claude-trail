import contextlib
import importlib
import importlib.util
import json
import os
import select
import sqlite3
import subprocess
import sys
import textwrap
import time
import zlib
from dataclasses import fields
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import claude_trail as feed          # for the records the turn queries map onto
import claude_trail_store as store

# The 12 objects the schema bootstrap must create in DuckDB: 8 tables + 4 views.
# Both show up in information_schema.tables (views included), so one membership
# check covers the whole schema. Blob content lives in the SQLite blob file.
EXPECTED_OBJECTS = {
    "actions",
    "api_requests",
    "command_tokens",
    "events",
    "file_history",
    "file_versions",
    "ingest_state",
    "raw_events",
    "request_context",
    "schema_meta",
    "session_tokens",
    "sessions",
}


@pytest.fixture(autouse=True)
def _isolate_file_history(tmp_path, monkeypatch):
    """Point FILE_HISTORY_DIR at a nonexistent dir by default so any ingest_all in
    the suite stays hermetic (the file-history pass returns early on a missing dir)
    and never reads the developer's real ~/.claude/file-history. The Phase 8 tests
    that exercise the backfill override this with their own populated tmp dir."""
    monkeypatch.setattr(store, "FILE_HISTORY_DIR", tmp_path / "no-file-history")


@pytest.fixture(autouse=True)
def _restore_pandas_entry(monkeypatch):
    """cmd_ingest writes sys.modules['pandas'] = None when pandas is missing
    (`_block_missing_pandas`). Register the entry with monkeypatch so every test
    hands it back as it found it, set or absent."""
    if "pandas" in sys.modules:
        monkeypatch.setitem(sys.modules, "pandas", sys.modules["pandas"])
    else:
        monkeypatch.setitem(sys.modules, "pandas", None)   # undo deletes it again
        monkeypatch.delitem(sys.modules, "pandas")


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
    assert "blobs" not in names                       # content is in the blob file
    blob_tables = sqlite3.connect(store._blob_path()).execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert blob_tables == [("blobs",)]

    con_ro = store.connect(read_only=True)
    try:
        assert con_ro.execute("SELECT value FROM schema_meta WHERE key='version'"
                              ).fetchone()[0] == store.SCHEMA_VERSION == "3"
    finally:
        con_ro.close()


# --- Transcript parser (Task 2) -----------------------------------------------
#
# Fixtures below are inline, trimmed-down copies of real transcript lines (shapes
# captured from ~/.claude/projects/**/*.jsonl while writing). They are hermetic:
# the parser is pure (dict in, list-of-row-dicts out), so no DB and no live
# transcript reads are involved.


def _assistant_text_and_bash():
    """An assistant line carrying a text block plus a Bash tool_use block.

    Real assistant lines repeat one `requestId` across every content-block line;
    both derived rows must carry it.
    """
    return {
        "type": "assistant",
        "requestId": "req_011CcYd4b3ixz4o1TRGY3jah",
        "uuid": "a292e4ea-8fbe-4941-b12a-830691e46728",
        "parentUuid": "64d215b5-814c-415a-bf93-aa909b870ff1",
        "sessionId": "bd9991f8-fd64-4b76-bd19-0491c42134e0",
        "timestamp": "2026-06-30T02:04:50.003Z",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-8",
            "id": "msg_011CcoBXFE9QUvVhstQRXFjs",
            "content": [
                {"type": "text", "text": "Let me check the caller identity."},
                {
                    "type": "tool_use",
                    "id": "toolu_01CaS4MXGt38NSvkBUgVeqsZ",
                    "name": "Bash",
                    "input": {
                        "command": "aws sts get-caller-identity",
                        "description": "Check AWS caller identity",
                    },
                },
            ],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 200,
                "cache_creation_input_tokens": 30,
                "service_tier": "standard",
            },
        },
    }


def _user_tool_result():
    """A user line carrying a tool_result block plus top-level toolUseResult."""
    return {
        "type": "user",
        "uuid": "e8a795be-21cc-40ee-ae0a-8dab626c6882",
        "parentUuid": "a292e4ea-8fbe-4941-b12a-830691e46728",
        "sessionId": "bd9991f8-fd64-4b76-bd19-0491c42134e0",
        "timestamp": "2026-06-30T02:04:51.200Z",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01CaS4MXGt38NSvkBUgVeqsZ",
                    "content": "account 935030491752",
                    "is_error": True,
                }
            ],
        },
        "toolUseResult": {
            "stdout": "account 935030491752",
            "stderr": "a warning line",
            "interrupted": False,
            "isImage": False,
        },
    }


def test_parse_event_assistant_text_and_tool_use():
    rows = store.parse_event(_assistant_text_and_bash())
    assert len(rows) == 2
    by_type = {r["type"]: r for r in rows}

    assistant = by_type["assistant"]
    assert assistant["text"] == "Let me check the caller identity."
    assert assistant["request_id"] == "req_011CcYd4b3ixz4o1TRGY3jah"

    tool = by_type["tool_use"]
    assert tool["tool_name"] == "Bash"
    assert tool["tool_use_id"] == "toolu_01CaS4MXGt38NSvkBUgVeqsZ"
    assert tool["event_id"] == "toolu_01CaS4MXGt38NSvkBUgVeqsZ"
    assert tool["text"] == "aws sts get-caller-identity"
    assert json.loads(tool["tool_input"])["command"] == "aws sts get-caller-identity"
    assert tool["request_id"] == "req_011CcYd4b3ixz4o1TRGY3jah"


def test_parse_event_user_tool_result():
    rows = store.parse_event(_user_tool_result())
    assert len(rows) == 1
    r = rows[0]
    assert r["type"] == "tool_result"
    assert r["tool_use_id"] == "toolu_01CaS4MXGt38NSvkBUgVeqsZ"
    assert r["stdout"] == "account 935030491752"
    assert r["stderr"] == "a warning line"
    assert r["is_error"] is True


def test_parse_event_user_prompt_string():
    ev = {
        "type": "user",
        "uuid": "u1",
        "sessionId": "s1",
        "timestamp": "2026-06-30T02:00:00.000Z",
        "message": {"role": "user", "content": "please run the tests"},
    }
    rows = store.parse_event(ev)
    assert len(rows) == 1
    assert rows[0]["type"] == "user"
    assert rows[0]["text"] == "please run the tests"


def test_parse_event_user_prompt_list_of_text():
    ev = {
        "type": "user",
        "uuid": "u2",
        "sessionId": "s1",
        "timestamp": "2026-06-30T02:00:00.000Z",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": "first line"},
                {"type": "text", "text": "second line"},
            ],
        },
    }
    rows = store.parse_event(ev)
    assert len(rows) == 1
    assert rows[0]["type"] == "user"
    assert rows[0]["text"] == "first line\nsecond line"


def test_parse_event_malformed_returns_empty_and_never_raises():
    assert store.parse_event("not a dict") == []
    assert store.parse_event(None) == []
    assert store.parse_event(42) == []
    # a dict missing the keys the parser reaches for
    assert store.parse_event({}) == []
    assert store.parse_event({"type": "assistant"}) == []
    assert store.parse_event({"type": "user", "message": {"content": None}}) == []
    # a content list holding junk blocks must not raise
    assert store.parse_event(
        {"type": "assistant", "message": {"content": ["oops", 3, None]}}
    ) == []


def test_parse_usage_returns_row_keyed_by_request_id():
    row = store.parse_usage(_assistant_text_and_bash())
    assert row is not None
    assert row["request_id"] == "req_011CcYd4b3ixz4o1TRGY3jah"
    assert row["message_id"] == "msg_011CcoBXFE9QUvVhstQRXFjs"
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 50
    assert row["cache_read_tokens"] == 200
    assert row["cache_creation_tokens"] == 30
    assert row["model"] == "claude-opus-4-8"
    assert row["service_tier"] == "standard"


def test_parse_usage_synthetic_returns_none():
    ev = {
        "type": "assistant",
        "requestId": "req_synth",
        "uuid": "syn1",
        "sessionId": "s1",
        "timestamp": "2026-06-30T02:00:00.000Z",
        "message": {
            "role": "assistant",
            "model": "<synthetic>",
            "id": "msg_synth",
            "content": [{"type": "text", "text": "(no api call)"}],
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    }
    assert store.parse_usage(ev) is None


def test_parse_usage_dedup_key_shared_across_content_block_lines():
    """One API request is written as several content-block lines that repeat the
    same requestId and identical usage; parse_usage keys each on request_id so
    the insert can dedup (dedup is the ingester's job, not the parser's)."""
    base = _assistant_text_and_bash()
    line_a = dict(base)
    line_b = dict(base)
    line_b["uuid"] = "second-content-block-line"
    row_a = store.parse_usage(line_a)
    row_b = store.parse_usage(line_b)
    assert row_a["request_id"] == row_b["request_id"] == "req_011CcYd4b3ixz4o1TRGY3jah"
    assert row_a["output_tokens"] == row_b["output_tokens"] == 50


def test_parse_usage_non_assistant_returns_none():
    assert store.parse_usage(_user_tool_result()) is None
    assert store.parse_usage("not a dict") is None
    assert store.parse_usage({"type": "assistant"}) is None  # no usage


def test_attribution_stamps_agent_id_on_every_row():
    rows = store.parse_event(_assistant_text_and_bash(), agent_id="abc")
    assert rows and all(r["agent_id"] == "abc" for r in rows)
    result_rows = store.parse_event(_user_tool_result(), agent_id="abc")
    assert result_rows and all(r["agent_id"] == "abc" for r in result_rows)
    # default (main thread) leaves agent_id None
    assert all(r["agent_id"] is None for r in store.parse_event(_user_tool_result()))


def test_attribution_parse_usage_sets_agent_id():
    row = store.parse_usage(_assistant_text_and_bash(), agent_id="abc")
    assert row["agent_id"] == "abc"
    assert store.parse_usage(_assistant_text_and_bash())["agent_id"] is None


# --- Incremental ingester (Task 3) --------------------------------------------
#
# These are hermetic: a tiny synthetic transcript is written under a fake
# ~/.claude/projects layout in tmp_path, and both PROJECTS_DIR and STORE_PATH are
# monkeypatched so nothing touches the live corpus or the real DB.


def _write_jsonl(path, events):
    """Write one JSON object per line, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def _main_transcript_events(sid):
    """Five transcript lines: two assistant content-block lines sharing ONE
    requestId + identical usage (the second carries a Bash tool_use), a user
    tool_result line, a plain user prompt, and one <synthetic> assistant line."""
    usage = {"input_tokens": 100, "output_tokens": 50,
             "cache_read_input_tokens": 200, "cache_creation_input_tokens": 30,
             "service_tier": "standard"}
    return [
        {  # assistant text block, request R1
            "type": "assistant", "uuid": "m-a-text", "requestId": "req-R1",
            "sessionId": sid, "timestamp": "2026-06-30T02:00:00.000Z",
            "message": {"role": "assistant", "model": "claude-opus-4-8",
                        "id": "msg-R1",
                        "content": [{"type": "text", "text": "Checking things."}],
                        "usage": usage}},
        {  # assistant Bash tool_use block, same request R1 + identical usage
            "type": "assistant", "uuid": "m-a-bash", "requestId": "req-R1",
            "sessionId": sid, "timestamp": "2026-06-30T02:00:01.000Z",
            "message": {"role": "assistant", "model": "claude-opus-4-8",
                        "id": "msg-R1",
                        "content": [{"type": "tool_use", "id": "toolu-bash-1",
                                     "name": "Bash",
                                     "input": {"command": "echo hi",
                                               "description": "say hi"}}],
                        "usage": usage}},
        {  # user tool_result with top-level toolUseResult
            "type": "user", "uuid": "m-u-result", "sessionId": sid,
            "timestamp": "2026-06-30T02:00:02.000Z",
            "message": {"role": "user",
                        "content": [{"type": "tool_result",
                                     "tool_use_id": "toolu-bash-1",
                                     "content": "hi", "is_error": False}]},
            "toolUseResult": {"stdout": "hi", "stderr": "",
                              "interrupted": False, "isImage": False}},
        {  # plain user prompt (string content)
            "type": "user", "uuid": "m-u-prompt", "sessionId": sid,
            "timestamp": "2026-06-30T02:00:03.000Z",
            "message": {"role": "user", "content": "please continue"}},
        {  # synthetic assistant line: parse_usage skips it, api_requests excludes it
            "type": "assistant", "uuid": "m-a-synth", "requestId": "req-synth",
            "sessionId": sid, "timestamp": "2026-06-30T02:00:04.000Z",
            "message": {"role": "assistant", "model": "<synthetic>",
                        "id": "msg-synth",
                        "content": [{"type": "text", "text": "(no api call)"}],
                        "usage": {"input_tokens": 0, "output_tokens": 0,
                                  "cache_read_input_tokens": 0,
                                  "cache_creation_input_tokens": 0}}},
    ]


def test_ingest_all_main_transcript(tmp_path, monkeypatch):
    """Full sweep over one synthetic main transcript, then idempotent re-run."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")

    sid = "sess-main-0001"
    events = _main_transcript_events(sid)
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", events)

    con = store.connect()
    try:
        total = store.ingest_all(con)
        assert total == len(events)  # every transcript line ingested

        def count(sql):
            return con.execute(sql).fetchone()[0]

        assert count("SELECT count(*) FROM raw_events") == len(events)   # 5, one per line
        assert count("SELECT count(*) FROM events") == 5
        by_type = dict(con.execute(
            "SELECT type, count(*) FROM events GROUP BY 1").fetchall())
        assert by_type == {"assistant": 2, "tool_use": 1,
                           "tool_result": 1, "user": 1}
        assert count("SELECT count(*) FROM events WHERE tool_name='Bash'") == 1
        # two content-block lines collapse to 1 request; synthetic excluded
        assert count("SELECT count(*) FROM api_requests") == 1
        assert con.execute("SELECT request_id FROM api_requests").fetchone()[0] == "req-R1"
        assert count("SELECT count(*) FROM sessions") == 1
        assert con.execute(
            "SELECT event_count FROM sessions").fetchone()[0] == len(events)

        # idempotent: re-running ingests nothing and leaves every table unchanged
        assert store.ingest_all(con) == 0
        assert count("SELECT count(*) FROM raw_events") == len(events)
        assert count("SELECT count(*) FROM events") == 5
        assert count("SELECT count(*) FROM api_requests") == 1
        assert con.execute(
            "SELECT event_count FROM sessions").fetchone()[0] == len(events)
    finally:
        con.close()


def test_ingest_all_subagent_attribution(tmp_path, monkeypatch):
    """Subagent transcript is swept in and its rows carry agent_id from the
    agent-<id>.jsonl filename; main-thread rows stay agent_id NULL."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")

    sid = "sess-sub-0001"
    main_line = {
        "type": "assistant", "uuid": "main-a-bash", "requestId": "req-main",
        "sessionId": sid, "timestamp": "2026-06-30T02:59:00.000Z",
        "message": {"role": "assistant", "model": "claude-opus-4-8", "id": "msg-main",
                    "content": [{"type": "tool_use", "id": "toolu-main-1",
                                 "name": "Bash",
                                 "input": {"command": "pwd", "description": "pwd"}}],
                    "usage": {"input_tokens": 20, "output_tokens": 10,
                              "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0,
                              "service_tier": "standard"}}}
    sub_line = {
        "type": "assistant", "uuid": "s-a-bash", "requestId": "req-sub",
        "sessionId": sid, "timestamp": "2026-06-30T03:00:00.000Z",
        "message": {"role": "assistant", "model": "claude-opus-4-8", "id": "msg-sub",
                    "content": [{"type": "tool_use", "id": "toolu-sub-1",
                                 "name": "Bash",
                                 "input": {"command": "ls", "description": "list"}}],
                    "usage": {"input_tokens": 10, "output_tokens": 5,
                              "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0,
                              "service_tier": "standard"}}}
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", [main_line])
    _write_jsonl(projects / "-fake-proj" / sid / "subagents" / "agent-XYZ.jsonl",
                 [sub_line])

    con = store.connect()
    try:
        assert store.ingest_all(con) == 2  # both files, one line each

        def count(sql):
            return con.execute(sql).fetchone()[0]

        # subagent rows are attributed to XYZ; main-thread rows stay NULL
        assert count("SELECT count(*) FROM events WHERE agent_id='XYZ'") == 1
        assert con.execute(
            "SELECT tool_name FROM events WHERE agent_id='XYZ'").fetchone()[0] == "Bash"
        assert count("SELECT count(*) FROM events WHERE agent_id IS NULL "
                     "AND tool_name='Bash'") == 1
        # api_requests carry the agent_id too
        assert count("SELECT count(*) FROM api_requests") == 2
        assert count("SELECT count(*) FROM api_requests WHERE agent_id='XYZ'") == 1
        assert con.execute(
            "SELECT request_id FROM api_requests WHERE agent_id='XYZ'").fetchone()[0] == "req-sub"
        assert count("SELECT count(*) FROM api_requests WHERE agent_id IS NULL") == 1
        # both files share the parent sessionId -> one session, two lines
        assert count("SELECT count(*) FROM sessions") == 1
        assert con.execute("SELECT event_count FROM sessions").fetchone()[0] == 2
    finally:
        con.close()


def _user_prompt(uuid, sid, text, ts):
    """One plain user-prompt transcript line (string content -> exactly one
    `events` row), so line counts and event counts move in lockstep."""
    return {"type": "user", "uuid": uuid, "sessionId": sid, "timestamp": ts,
            "message": {"role": "user", "content": text}}


def test_ingest_incremental_append_and_partial_line(tmp_path, monkeypatch):
    """Tailing: appended complete lines ingest exactly once and never re-count
    the seed, and a trailing partial line (no newline yet) is withheld until its
    terminating newline arrives. Exercises the offset math in ingest_transcript.
    """
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")

    sid = "sess-append-0001"
    path = projects / "-fake-proj" / f"{sid}.jsonl"
    seed = [_user_prompt("u-seed-1", sid, "first", "2026-06-30T02:00:00.000Z"),
            _user_prompt("u-seed-2", sid, "second", "2026-06-30T02:00:01.000Z")]
    _write_jsonl(path, seed)

    con = store.connect()
    try:
        def count(sql):
            return con.execute(sql).fetchone()[0]

        assert store.ingest_all(con) == len(seed)               # 2 seed lines
        assert count("SELECT count(*) FROM raw_events") == 2
        assert count("SELECT count(*) FROM events") == 2

        # append 2 complete lines PLUS a trailing partial line with NO newline
        complete = "".join(json.dumps(e) + "\n" for e in [
            _user_prompt("u-app-1", sid, "third", "2026-06-30T02:00:02.000Z"),
            _user_prompt("u-app-2", sid, "fourth", "2026-06-30T02:00:03.000Z")])
        partial = json.dumps(
            _user_prompt("u-app-3", sid, "fifth-partial", "2026-06-30T02:00:04.000Z"))
        with open(path, "a", encoding="utf-8") as f:
            f.write(complete + partial)                          # partial: no '\n'

        # only the 2 completed lines land; the seed is not re-counted and the
        # partial trailing line is withheld
        assert store.ingest_all(con) == 2
        assert count("SELECT count(*) FROM raw_events") == 4
        assert count("SELECT count(*) FROM events") == 4
        assert count("SELECT count(*) FROM raw_events WHERE uuid='u-app-3'") == 0

        # re-running while the line is still partial changes nothing
        assert store.ingest_all(con) == 0
        assert count("SELECT count(*) FROM raw_events") == 4

        # terminate the partial line -> it now ingests, exactly once
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n")
        assert store.ingest_all(con) == 1
        assert count("SELECT count(*) FROM raw_events") == 5
        assert count("SELECT count(*) FROM events") == 5
        assert count("SELECT count(*) FROM raw_events WHERE uuid='u-app-3'") == 1
    finally:
        con.close()


class _CountingCon:
    """Wraps a DuckDB connection and records the SQL of every execute/executemany,
    so a test can count the queries a sweep issues. Everything else delegates."""

    def __init__(self, con):
        self._con = con
        self.sql = []

    def execute(self, sql, *args, **kwargs):
        self.sql.append(sql)
        return self._con.execute(sql, *args, **kwargs)

    def executemany(self, sql, *args, **kwargs):
        self.sql.append(sql)
        return self._con.executemany(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._con, name)


def test_idle_sweep_runs_one_state_query_not_one_per_transcript(tmp_path, monkeypatch):
    """The sweep loads every stored offset in one query, so an idle sweep over N
    unchanged transcripts (main and subagent) runs that query and nothing per
    file: an unchanged file is skipped after its stat."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")

    for i in range(4):
        sid = f"sess-idle-{i:04d}"
        _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl",
                     [_user_prompt(f"u-idle-{i}", sid, "hi", "2026-06-30T02:00:00.000Z")])
    _write_jsonl(projects / "-fake-proj" / "sess-idle-0000" / "subagents" / "agent-A1.jsonl",
                 [_user_prompt("u-idle-sub", "sess-idle-0000", "task",
                               "2026-06-30T02:00:01.000Z")])
    n = len(store._transcript_paths())
    assert n == 5

    con = store.connect()
    try:
        assert store._sweep_transcripts(con) == (n, n, n)    # first sweep stores all
        counting = _CountingCon(con)
        assert store._sweep_transcripts(counting) == (n, 0, 0)
        assert len(counting.sql) == 1                        # not 1 + n
        assert "ingest_state" in counting.sql[0]
        assert "WHERE" not in counting.sql[0]                # the bulk load, not a lookup
    finally:
        con.close()


def test_sweep_starts_a_new_transcript_at_zero_and_rescans_a_truncated_one(
        tmp_path, monkeypatch):
    """With the offsets preloaded, a transcript absent from ingest_state still
    starts at byte 0, and one rewritten shorter than its stored offset rescans
    from 0 (`size < last`), exactly as the per-file lookup did."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")

    sid_a, sid_b = "sess-trunc-000a", "sess-new-000b"
    path_a = projects / "-fake-proj" / f"{sid_a}.jsonl"
    path_b = projects / "-fake-proj" / f"{sid_b}.jsonl"
    _write_jsonl(path_a, [
        _user_prompt(f"u-a-{i}", sid_a, f"line {i}", f"2026-06-30T02:00:0{i}.000Z")
        for i in range(3)])

    con = store.connect()
    try:
        def offset(path):
            row = con.execute("SELECT last_offset FROM ingest_state "
                              "WHERE transcript_path=?", [str(path)]).fetchone()
            return row[0] if row else None

        assert store._sweep_transcripts(con) == (1, 1, 3)
        stored_a = offset(path_a)
        assert stored_a == path_a.stat().st_size

        # a: rewritten with one new line, shorter than its stored offset.
        # b: a brand-new transcript with no ingest_state row yet.
        _write_jsonl(path_a, [_user_prompt("u-a-new", sid_a, "x",
                                           "2026-06-30T03:00:00.000Z")])
        assert path_a.stat().st_size < stored_a
        _write_jsonl(path_b, [
            _user_prompt(f"u-b-{i}", sid_b, f"b {i}", f"2026-06-30T04:00:0{i}.000Z")
            for i in range(2)])
        assert offset(path_b) is None

        # b ingests both lines from 0; a rescans from 0 and finds its one line
        assert store._sweep_transcripts(con) == (2, 2, 3)
        assert offset(path_a) == path_a.stat().st_size
        assert offset(path_b) == path_b.stat().st_size
        assert con.execute("SELECT count(*) FROM raw_events "
                           "WHERE uuid IN ('u-a-new', 'u-b-0', 'u-b-1')").fetchone()[0] == 3
        assert store._sweep_transcripts(con) == (2, 0, 0)    # and both are now idle
    finally:
        con.close()


def test_ingest_transcript_alone_still_looks_up_its_stored_offset(tmp_path, monkeypatch):
    """Called without a preloaded offset, ingest_transcript queries ingest_state
    itself, so a standalone caller keeps the old contract: an unchanged file
    ingests nothing and an appended one only its new tail."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")

    sid = "sess-alone-0001"
    path = projects / "-fake-proj" / f"{sid}.jsonl"
    _write_jsonl(path, [_user_prompt("u-alone-1", sid, "one", "2026-06-30T02:00:00.000Z")])

    con = store.connect()
    try:
        assert store.ingest_transcript(con, path) == 1
        counting = _CountingCon(con)
        assert store.ingest_transcript(counting, path) == 0
        assert len(counting.sql) == 1 and "WHERE transcript_path" in counting.sql[0]
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_user_prompt("u-alone-2", sid, "two",
                                            "2026-06-30T02:00:01.000Z")) + "\n")
        assert store.ingest_transcript(con, path) == 1
    finally:
        con.close()


def test_ingest_drops_nonsnapshot_raw_payload_keeps_index(tmp_path, monkeypatch):
    """Going-forward leanness: ingest keeps one raw_events index row per line
    (so tailing/idempotency are unchanged) but drops the bulky verbatim `raw`
    payload for non-snapshot lines, while every derived table is fully built."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    sid = "sess-lean-0001"
    events = _main_transcript_events(sid)
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", events)
    con = store.connect()
    try:
        store.ingest_all(con)
        c = lambda sql: con.execute(sql).fetchone()[0]
        assert c("SELECT count(*) FROM raw_events") == len(events)           # index kept
        assert c("SELECT count(*) FROM raw_events WHERE raw IS NOT NULL") == 0  # payload dropped
        assert c("SELECT count(*) FROM events") == 5                          # projection intact
        assert c("SELECT count(*) FROM api_requests") == 1
    finally:
        con.close()


def test_ingest_keeps_snapshot_raw(tmp_path, monkeypatch):
    """The file-history snapshot and delta payloads ARE retained (the full
    backfill pass reads them back), even as the neighbouring user line's payload
    is dropped."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    sid = "sess-snap-0001"
    events = [
        _user_prompt("u1", sid, "hi", "2026-07-01T10:00:00.000Z"),
        _fh_snapshot_event("m1", {"/tmp/a.txt": {
            "backupFileName": "h1@v1", "version": 1,
            "backupTime": "2026-07-01T10:00:00.000Z"}}),
        _fh_delta_event("m2", "/tmp/b.txt", "h2@v1", 1, "2026-07-01T10:00:01.000Z", "/tmp"),
    ]
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", events)
    con = store.connect()
    try:
        store.ingest_all(con)
        c = lambda sql: con.execute(sql).fetchone()[0]
        assert c("SELECT raw FROM raw_events WHERE type='file-history-snapshot'") is not None
        assert c("SELECT raw FROM raw_events WHERE type='file-history-delta'") is not None
        assert c("SELECT count(*) FROM raw_events WHERE type='user' AND raw IS NOT NULL") == 0
        assert c("SELECT count(*) FROM raw_events") == 3                      # every index row kept
    finally:
        con.close()


def test_prune_raw_events_clears_payload_keeps_snapshots_and_malformed(tmp_path, monkeypatch):
    """prune_raw_events nulls only the payloads nothing reads: file-history
    snapshot and delta rows (read back) and malformed (type NULL) rows keep
    theirs, and it is idempotent."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    con = store.connect()
    try:
        con.executemany(
            "INSERT INTO raw_events(uuid,session_id,ts,type,transcript_path,raw) VALUES (?,?,?,?,?,?)",
            [("u1", "s", None, "user", "/p", '{"big":"payload"}'),
             ("snap", "s", None, "file-history-snapshot", "/p", '{"snapshot":1}'),
             ("delta", "s", None, "file-history-delta", "/p", '{"backup":1}'),
             ("bad", "s", None, None, "/p", "not-json")])          # malformed: type NULL
        cleared, nbytes = store.prune_raw_events(con)
        assert cleared == 1 and nbytes > 0
        val = lambda u: con.execute("SELECT raw FROM raw_events WHERE uuid=?", [u]).fetchone()[0]
        assert val("u1") is None            # non-snapshot payload cleared
        assert val("snap") is not None      # snapshot kept
        assert val("delta") is not None     # delta kept
        assert val("bad") is not None       # malformed kept
        assert con.execute("SELECT count(*) FROM raw_events").fetchone()[0] == 4  # index intact
        assert store.prune_raw_events(con) == (0, 0)  # idempotent
    finally:
        con.close()


def test_cmd_prune_raw_clears_and_compacts(tmp_path, monkeypatch, capsys):
    """End-to-end: prune-raw -y clears a lingering old-style payload row, compacts,
    and reports; the row's index survives with a NULL payload."""
    sid = _build_cli_store(tmp_path, monkeypatch)
    con = store.connect()  # inject a pre-change row that still carries a payload
    con.execute("INSERT INTO raw_events(uuid,session_id,ts,type,transcript_path,raw) "
                "VALUES ('manual-1',?,NULL,'user','/p','{\"big\":\"x\"}')", [sid])
    con.close()
    assert store.cmd_prune_raw(["prune-raw", "-y"]) == 0
    out = capsys.readouterr().out
    assert "cleared 1" in out and "compacted" in out
    con = store.connect(read_only=True)
    try:
        assert con.execute("SELECT raw FROM raw_events WHERE uuid='manual-1'").fetchone()[0] is None
    finally:
        con.close()


def test_decode_project_maps_encoded_dir_name(tmp_path, monkeypatch):
    """A leading '-' cwd-encoded dir name decodes to a slash path label."""
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    path = tmp_path / "-home-naka-Projects-personal" / "abc.jsonl"
    assert store._decode_project(path) == "/home/naka/Projects/personal"


# --- sessions.cwd: the exact launch dir, and the backfill for old rows ---------


def _cwd_of(con, sid):
    return con.execute("SELECT cwd FROM sessions WHERE session_id=?", [sid]).fetchone()[0]


def _cwd_transcript(sid, cwd, uuid="c-u1", ts="2026-06-30T04:00:00.000Z"):
    """One main-transcript user line carrying a top-level cwd."""
    return [{**_user_prompt(uuid, sid, "hello", ts), "cwd": cwd}]


def test_ingest_writes_the_true_cwd_for_a_hyphenated_project_dir(tmp_path, monkeypatch):
    """The whole point of the column: a project dir whose name contains a `-`
    decodes to a path that does not exist, while the transcript's own cwd is
    exact. Both are stored; only one is right."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    sid, cwd = "sess-cwd-0001", "/home/u/Projects/personal/claude-trail"
    _write_jsonl(projects / "-home-u-Projects-personal-claude-trail" / f"{sid}.jsonl",
                 _cwd_transcript(sid, cwd))
    con = store.connect()
    try:
        store.ingest_all(con)
        project, stored = con.execute(
            "SELECT project, cwd FROM sessions WHERE session_id=?", [sid]).fetchone()
    finally:
        con.close()
    assert stored == cwd                                   # exact, dashes intact
    assert project == "/home/u/Projects/personal/claude/trail"  # lossy, not a real path
    assert project != stored


def test_ingest_keeps_the_first_cwd_when_the_session_cds(tmp_path, monkeypatch):
    """A session's cwd is its LAUNCH dir: Claude Code rewrites the top-level cwd
    after a `cd`, so later lines (and later incremental sweeps) must not move the
    column to whatever scratchpad the run wandered into."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    sid, launched = "sess-cwd-0002", "/home/u/Projects/personal"
    path = projects / "-home-u-Projects-personal" / f"{sid}.jsonl"
    _write_jsonl(path, [
        {**_user_prompt("cd-u1", sid, "first", "2026-06-30T04:00:00.000Z"),
         "cwd": launched},
        {**_user_prompt("cd-u2", sid, "second", "2026-06-30T04:00:01.000Z"),
         "cwd": "/tmp/scratch"},
    ])
    con = store.connect()
    try:
        store.ingest_all(con)
        assert _cwd_of(con, sid) == launched
        # a later sweep of appended lines, all of them post-`cd`, changes nothing
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {**_user_prompt("cd-u3", sid, "third", "2026-06-30T04:00:02.000Z"),
                 "cwd": "/tmp/scratch"}) + "\n")
        store.ingest_all(con)
        assert _cwd_of(con, sid) == launched
    finally:
        con.close()


def test_subagent_cwd_never_overwrites_the_parent_session(tmp_path, monkeypatch):
    """A subagent transcript is ingested under the PARENT session id but often
    runs somewhere else (a /tmp scratchpad), so its cwd is ignored outright:
    it neither replaces the parent's nor fills in for a missing one."""
    sid = _build_turn_store(tmp_path, monkeypatch)[0]
    con = store.connect(read_only=True)
    try:
        assert _cwd_of(con, sid) == _TURN_CWD          # not the subagent's scratchpad
    finally:
        con.close()

    # And with ONLY the subagent transcript on disk, the session row it creates
    # keeps a NULL cwd rather than claiming the scratchpad as the project.
    projects = tmp_path / "projects2"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail2.duckdb")
    sid2 = "sess-cwd-0003"
    _write_jsonl(projects / "-fake-proj" / sid2 / "subagents" / "agent-ag9.jsonl",
                 _cwd_transcript(sid2, _TURN_SUB_CWD, uuid="s-only-1"))
    con = store.connect()
    try:
        store.ingest_all(con)
        assert _cwd_of(con, sid2) is None
    finally:
        con.close()


def test_backfill_session_cwd_fills_null_rows_in_place(tmp_path, monkeypatch):
    """The upgrade path for rows ingested before the column was written: no
    reset-db, just a bounded head read per stale session. Idempotent - a second
    run has nothing to do."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    sid, cwd = "sess-cwd-0004", "/home/u/Projects/personal/keypad-macros"
    _write_jsonl(projects / "-home-u-Projects-personal-keypad-macros" / f"{sid}.jsonl",
                 _cwd_transcript(sid, cwd))
    con = store.connect()
    try:
        store.ingest_all(con)
        con.execute("UPDATE sessions SET cwd=NULL")      # the pre-change store
        assert store.backfill_session_cwd(con) == 1
        assert _cwd_of(con, sid) == cwd
        assert store.backfill_session_cwd(con) == 0      # nothing NULL left
        assert _cwd_of(con, sid) == cwd
    finally:
        con.close()


def test_backfill_session_cwd_skips_a_session_whose_transcript_is_gone(tmp_path, monkeypatch):
    """Most rows of an old store outlive their transcripts. Those stay NULL (the
    Project column falls back to `project`) instead of raising or guessing."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    sid = "sess-cwd-0005"
    path = projects / "-fake-proj" / f"{sid}.jsonl"
    _write_jsonl(path, _cwd_transcript(sid, "/home/u/gone"))
    con = store.connect()
    try:
        store.ingest_all(con)
        con.execute("UPDATE sessions SET cwd=NULL")
        path.unlink()
        assert store.backfill_session_cwd(con) == 0
        assert _cwd_of(con, sid) is None
    finally:
        con.close()


def test_backfill_session_cwd_reads_only_the_head_of_a_transcript(tmp_path, monkeypatch):
    """The backfill's read is BOUNDED, unlike the ingest sweep (which is parsing
    every line anyway): a cwd that first appears past SESSION_CWD_SCAN_LINES is
    not worth re-reading a multi-hundred-MB transcript for, and finding none in
    the head leaves the row NULL rather than raising."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    sid = "sess-cwd-0006"
    deep = [_user_prompt(f"d-u{i}", sid, f"ask {i}",
                         f"2026-06-30T04:{i:02d}:00.000Z")
            for i in range(store.SESSION_CWD_SCAN_LINES + 2)]
    deep[-1] = {**deep[-1], "cwd": "/home/u/too-deep"}
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", deep)
    con = store.connect()
    try:
        store.ingest_all(con)
        # The sweep reads the whole file, so it does find the late cwd ...
        assert _cwd_of(con, sid) == "/home/u/too-deep"
        con.execute("UPDATE sessions SET cwd=NULL")
        # ... while the backfill stops at the head and leaves the row alone.
        assert store.backfill_session_cwd(con) == 0
        assert _cwd_of(con, sid) is None
    finally:
        con.close()


def test_cmd_ingest_backfills_session_cwd_and_reports_it(tmp_path, monkeypatch, capsys):
    """The backfill rides the normal `ingest` sweep, so the user upgrades by
    doing nothing. Modeled on the real upgrade: a store that already ingested
    everything (so the sweep finds no new line) and whose cwd column is empty.
    It logs only when it actually filled rows."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    sid, cwd = "sess-cwd-0007", "/home/u/Projects/personal/personal-fin"
    _write_jsonl(projects / "-home-u-Projects-personal-personal-fin" / f"{sid}.jsonl",
                 _cwd_transcript(sid, cwd))
    con = store.connect()
    try:
        store.ingest_all(con)
        con.execute("UPDATE sessions SET cwd=NULL")   # the pre-change store shape
    finally:
        con.close()
    capsys.readouterr()                               # drop the setup's output
    assert store.cmd_ingest(["ingest"]) == 0
    out = capsys.readouterr().out
    assert "transcripts: 0/1 changed" in out          # nothing re-parsed, just the fill
    assert "sessions: 1 cwd backfilled" in out
    # A backfilled cwd is an UPDATE, so no table count moves and the run looks
    # empty to `_checkpoint_due`. It must still flush, or the fill sits in the
    # WAL behind a "stored nothing" skip.
    assert "checkpoint: flushing WAL to disk" in out
    assert "checkpoint: skipped" not in out
    con = store.connect(read_only=True)
    try:
        assert _cwd_of(con, sid) == cwd
    finally:
        con.close()
    assert store.cmd_ingest(["ingest"]) == 0          # silent once nothing is NULL
    assert "cwd backfilled" not in capsys.readouterr().out


def _post_cd_upgrade_store(tmp_path, monkeypatch, sid, launched):
    """A store in the exact shape the upgrade run meets it in: the transcript's
    launch-dir head is ALREADY ingested (the offset is past it), the session row
    predates the column so its cwd is NULL, and one post-`cd` line is waiting to
    be swept. Returns the transcript path."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    path = projects / "-home-u-Projects-personal-claude-trail" / f"{sid}.jsonl"
    _write_jsonl(path, _cwd_transcript(sid, launched))
    con = store.connect()
    try:
        store.ingest_all(con)                          # head ingested, offset advanced
        con.execute("UPDATE sessions SET cwd=NULL")    # the pre-change store shape
    finally:
        con.close()
    with open(path, "a", encoding="utf-8") as fh:      # the tail, written after a `cd`
        fh.write(json.dumps(
            {**_user_prompt("cd-tail", sid, "after cd", "2026-06-30T04:00:05.000Z"),
             "cwd": "/tmp/scratchpad"}) + "\n")
    return path


def test_ingest_all_keeps_the_launch_dir_when_the_sweep_lands_a_post_cd_tail(
        tmp_path, monkeypatch):
    """The two writers of this column have to agree, and nothing between them
    says so. An incremental sweep parses a chunk that starts MID-FILE, so the
    first cwd it sees is wherever the run had `cd`-ed to; writing that is
    permanent (`coalesce` never replaces a value and the backfill skips a row
    that is no longer NULL), which would label the session with a scratchpad
    forever. Pins the outcome, not the mechanism: whoever ends up writing the
    row, it must be the launch dir."""
    sid, launched = "sess-cwd-0008", "/home/u/Projects/personal/claude-trail"
    _post_cd_upgrade_store(tmp_path, monkeypatch, sid, launched)
    con = store.connect()
    try:
        store.ingest_all(con)
        assert _cwd_of(con, sid) == launched      # not "/tmp/scratchpad"
    finally:
        con.close()


def test_cmd_ingest_keeps_the_launch_dir_when_the_sweep_lands_a_post_cd_tail(
        tmp_path, monkeypatch, capsys):
    """The same ordering guarantee on the path the user actually runs (the Stop
    hook and the 30s timer both land here), where the sweep really does ingest a
    new tail chunk rather than finding nothing to do."""
    sid, launched = "sess-cwd-0009", "/home/u/Projects/personal/claude-trail"
    _post_cd_upgrade_store(tmp_path, monkeypatch, sid, launched)
    capsys.readouterr()                                # drop the setup's output
    assert store.cmd_ingest(["ingest"]) == 0
    out = capsys.readouterr().out
    assert "transcripts: 1/1 changed, +1 lines" in out  # the tail WAS swept
    assert "sessions: 1 cwd backfilled" in out
    con = store.connect(read_only=True)
    try:
        assert _cwd_of(con, sid) == launched
    finally:
        con.close()


def test_backfill_session_cwd_rereads_only_transcripts_whose_row_is_still_null(
        tmp_path, monkeypatch):
    """The name pre-filter is what keeps the steady state cheap. Rows that can
    never be filled (transcript gone) keep `pending` non-empty forever on a real
    store, so without the filter every sweep would re-read the head of every
    transcript on disk instead of only the ones whose own row is still NULL."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    done, todo = "sess-cwd-0010", "sess-cwd-0011"
    _write_jsonl(projects / "-fake-proj" / f"{done}.jsonl",
                 _cwd_transcript(done, "/home/u/done", uuid="pf-u1"))
    _write_jsonl(projects / "-fake-proj" / f"{todo}.jsonl",
                 _cwd_transcript(todo, "/home/u/todo", uuid="pf-u2"))
    reads = []
    real_head_cwd = store._head_cwd

    def counting_head_cwd(path):
        reads.append(path.stem)
        return real_head_cwd(path)

    monkeypatch.setattr(store, "_head_cwd", counting_head_cwd)
    con = store.connect()
    try:
        store.ingest_all(con)
        con.execute("UPDATE sessions SET cwd=NULL WHERE session_id=?", [todo])
        # a session whose transcript is long gone: NULL forever, and the reason
        # the cheap early return never fires on a real store
        con.execute("INSERT INTO sessions(session_id, project) VALUES (?,?)",
                    ["sess-cwd-gone", "/fake/proj"])
        reads.clear()
        assert store.backfill_session_cwd(con) == 1
        assert reads == [todo]                       # not the already-filled one
        assert _cwd_of(con, todo) == "/home/u/todo"
        assert _cwd_of(con, done) == "/home/u/done"
        assert _cwd_of(con, "sess-cwd-gone") is None
    finally:
        con.close()


def test_compact_db_reclaims_free_blocks_and_preserves_rows(tmp_path, monkeypatch):
    """compact_db rewrites the store into a fresh file: every row survives, the
    file is no larger, PRAGMA reports zero free blocks, and the temp is cleaned
    up. This is the opt-in remedy for the ~2x free-block bloat a bulk ingest
    leaves (CHECKPOINT alone never coalesces those blocks). Hermetic:
    monkeypatched PROJECTS_DIR/STORE_PATH, no live corpus."""
    projects = tmp_path / "projects"
    store_path = tmp_path / "trail.duckdb"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", store_path)

    sid = "sess-compact-0001"
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", _main_transcript_events(sid))

    tables = ("raw_events", "events", "api_requests", "sessions")
    con = store.connect()
    try:
        store.ingest_all(con)
        before = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        con.close()  # release the write lock before compacting (compact_db's contract)
    assert all(v > 0 for v in before.values())  # the fixture actually populated every table

    old_size, new_size = store.compact_db(store_path)
    assert new_size <= old_size
    assert not (store_path.with_suffix(store_path.suffix + ".compact-tmp")).exists()

    con_ro = store.connect(read_only=True)
    try:
        after = {t: con_ro.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
        # PRAGMA database_size columns: ...,used_blocks, free_blocks(=idx 5),...
        free_blocks = con_ro.execute("PRAGMA database_size").fetchone()[5]
    finally:
        con_ro.close()
    assert after == before   # no data loss across the rewrite
    assert free_blocks == 0  # fully compacted, no interspersed free blocks left


# --- Search / stats / tokens CLI (Task 4) -------------------------------------
#
# Hermetic: a tiny synthetic store is built by ingest_all from the shared
# _main_transcript_events fixture (a Bash 'echo hi' command emitted by request R1
# with 50 output tokens, model claude-opus-4-8, dated 2026-06-30, plus a plain
# user prompt and a skipped <synthetic> line) under a fake ~/.claude/projects
# layout in tmp_path. PROJECTS_DIR and STORE_PATH are monkeypatched so the CLI
# commands, which open their OWN read-only connection, hit that store and never
# the live corpus or the real DB. The writer is closed before the commands run so
# the file is unlocked for the read-only reopen. stdout is captured via capsys.


def _build_cli_store(tmp_path, monkeypatch, sid="sess-cli-0001"):
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", _main_transcript_events(sid))
    con = store.connect()
    try:
        store.ingest_all(con)
    finally:
        con.close()  # release the write lock so the cmd_* read-only reopen works
    return sid


def test_cmd_search_matches_known_bash_command(tmp_path, monkeypatch, capsys):
    """A pattern hitting a known Bash command returns that row (the command text
    and the 8-char session prefix render)."""
    sid = _build_cli_store(tmp_path, monkeypatch)
    assert store.cmd_search(["search", "echo hi", "--tool", "Bash"]) == 0
    out = capsys.readouterr().out
    assert "echo hi" in out
    assert sid[:8] in out           # substr(session_id,1,8)
    assert "Bash" in out


def test_cmd_search_non_matching_pattern_returns_no_rows(tmp_path, monkeypatch, capsys):
    """A pattern that matches nothing yields no data row (no session prefix, no
    command text) even though the header still prints."""
    sid = _build_cli_store(tmp_path, monkeypatch)
    assert store.cmd_search(["search", "no_such_token_zzz"]) == 0
    out = capsys.readouterr().out
    assert sid[:8] not in out
    assert "echo hi" not in out


def test_cmd_search_tool_filter_excludes_non_matching_tool(tmp_path, monkeypatch, capsys):
    """--tool Bash restricts to Bash rows: the plain user prompt (tool_name NULL)
    matches without the filter but is excluded with it."""
    _build_cli_store(tmp_path, monkeypatch)
    assert store.cmd_search(["search", "please continue"]) == 0
    assert "please continue" in capsys.readouterr().out
    assert store.cmd_search(["search", "please continue", "--tool", "Bash"]) == 0
    assert "please continue" not in capsys.readouterr().out


def test_cmd_search_project_filter_matches_either_path_column(tmp_path, monkeypatch, capsys):
    """`--project` has to accept the name the user can see. The Prompts tab shows
    `sessions.cwd`, so `--project claude-trail` must hit it; the lossy
    `sessions.project` (`claude/trail`, all dashes turned into slashes) still
    matches too, since that is all a row with no cwd has."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    sid = "sess-proj-0001"
    _write_jsonl(projects / "-home-u-Projects-personal-claude-trail" / f"{sid}.jsonl",
                 [{**e, "cwd": "/home/u/Projects/personal/claude-trail"}
                  for e in _main_transcript_events(sid)])
    con = store.connect()
    try:
        store.ingest_all(con)
    finally:
        con.close()                 # release the write lock for the read-only reopen
    assert store.cmd_search(["search", "echo hi", "--project", "claude-trail"]) == 0
    assert "echo hi" in capsys.readouterr().out          # the real dir name
    assert store.cmd_search(["search", "echo hi", "--project", "claude/trail"]) == 0
    assert "echo hi" in capsys.readouterr().out          # the decoded one
    assert store.cmd_search(["search", "echo hi", "--project", "keypad-macros"]) == 0
    assert "echo hi" not in capsys.readouterr().out      # and it still filters


def test_cmd_tokens_default_per_session(tmp_path, monkeypatch, capsys):
    """Default (no --session) is per-session totals: the session prints with its
    50-token output total."""
    sid = _build_cli_store(tmp_path, monkeypatch)
    assert store.cmd_tokens(["tokens"]) == 0
    out = capsys.readouterr().out
    assert sid[:8] in out
    assert "50" in out              # sum(output_tokens) for the session


def test_cmd_tokens_default_per_session_without_pytz(tmp_path, monkeypatch, capsys):
    """The default per-session view must not require the optional `pytz` package.

    DuckDB's Python client needs `pytz` to materialize a raw TIMESTAMPTZ into a
    datetime; the session branch dodges that by strftime-ing min/max(ts) to
    strings. Simulate a clean install without pytz by mapping the module to None
    (so any `import pytz` raises ModuleNotFoundError), then run the default view:
    it must return 0 and print the session row without raising. Regression for the
    raw-min(ts)/max(ts) footgun that only passed while the dev env had pytz.
    """
    monkeypatch.setitem(sys.modules, "pytz", None)  # any `import pytz` now raises
    sid = _build_cli_store(tmp_path, monkeypatch)
    assert store.cmd_tokens(["tokens"]) == 0
    out = capsys.readouterr().out
    assert sid[:8] in out
    assert "50" in out              # sum(output_tokens) for the session


def test_cmd_tokens_session_per_command(tmp_path, monkeypatch, capsys):
    """--session <sid> switches the default --by to command: the Bash tool_use row
    prints with its generation-cost output tokens."""
    sid = _build_cli_store(tmp_path, monkeypatch)
    assert store.cmd_tokens(["tokens", "--session", sid]) == 0
    out = capsys.readouterr().out
    assert "Bash" in out
    assert "echo hi" in out
    assert "50" in out


def test_cmd_tokens_by_day_and_by_model_run(tmp_path, monkeypatch, capsys):
    """--by day and --by model both run and print their expected columns."""
    _build_cli_store(tmp_path, monkeypatch)
    assert store.cmd_tokens(["tokens", "--by", "day"]) == 0
    out_day = capsys.readouterr().out
    assert "day" in out_day             # header
    # strftime on a TIMESTAMPTZ renders in the session's local tz, so the UTC
    # 02:00 fixture lands on 2026-06-29 or -30 depending on the machine: assert
    # the tz-stable year-month rather than an exact day.
    assert "2026-06" in out_day         # the grouped day

    assert store.cmd_tokens(["tokens", "--by", "model"]) == 0
    out_model = capsys.readouterr().out
    assert "model" in out_model         # header
    assert "claude-opus-4-8" in out_model


def test_command_tokens_for_matches_bash_command(tmp_path, monkeypatch):
    """command_tokens_for maps (session, command text) to the emitting request's
    generation cost: the 'echo hi' fixture cost 50 output tokens on a 1-call
    request, model claude-opus-4-8."""
    sid = _build_cli_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        t = store.command_tokens_for(con, sid, "echo hi")
        # ts tie-break path must return the same single match
        t_ts = store.command_tokens_for(con, sid, "echo hi", ts="2026-06-30T02:00:01Z")
    finally:
        con.close()
    assert t == {"output_tokens": 50, "calls_in_request": 1,
                 "share": 50.0, "model": "claude-opus-4-8"}
    assert t_ts == t


def test_command_tokens_for_no_match_returns_none(tmp_path, monkeypatch):
    """A command/session that never emitted a request, or empty args, yield None
    (the TUI then omits the Tokens row)."""
    sid = _build_cli_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        assert store.command_tokens_for(con, sid, "no_such_command_zzz") is None
        assert store.command_tokens_for(con, "no-such-session", "echo hi") is None
        assert store.command_tokens_for(con, sid, "") is None
        assert store.command_tokens_for(con, "", "echo hi") is None
    finally:
        con.close()


def _prompt_transcript_events(sid):
    """A main thread whose turns exercise every prompt-lookup rule: a first user
    prompt, a Bash tool_use after it, then a reminder-only turn, an interrupt
    marker, and a second prompt carrying a system-reminder block, followed by a
    second Bash tool_use."""
    return [
        {"type": "user", "uuid": "p-u1", "sessionId": sid,
         "timestamp": "2026-06-30T02:00:00.000Z",
         "message": {"role": "user", "content": "first ask\nsecond line"}},
        {"type": "assistant", "uuid": "p-a1", "requestId": "req-P1",
         "sessionId": sid, "timestamp": "2026-06-30T02:00:01.000Z",
         "message": {"role": "assistant", "model": "claude-opus-4-8", "id": "msg-P1",
                     "content": [{"type": "tool_use", "id": "toolu-p1", "name": "Bash",
                                  "input": {"command": "echo one"}}]}},
        {"type": "user", "uuid": "p-u2", "sessionId": sid,
         "timestamp": "2026-06-30T02:00:02.000Z",
         "message": {"role": "user",
                     "content": "<system-reminder>\nbackground note\n</system-reminder>"}},
        {"type": "user", "uuid": "p-u3", "sessionId": sid,
         "timestamp": "2026-06-30T02:00:03.000Z",
         "message": {"role": "user", "content": "[Request interrupted by user]"}},
        {"type": "user", "uuid": "p-u3b", "sessionId": sid,
         "timestamp": "2026-06-30T02:00:03.200Z",
         "message": {"role": "user",
                     "content": "<task-notification>agent bp8 done</task-notification>"}},
        {"type": "user", "uuid": "p-u3c", "sessionId": sid,
         "timestamp": "2026-06-30T02:00:03.400Z",
         "message": {"role": "user",
                     "content": "<local-command-stdout>Resume cancelled</local-command-stdout>"}},
        {"type": "user", "uuid": "p-u4", "sessionId": sid,
         "timestamp": "2026-06-30T02:00:04.000Z",
         "message": {"role": "user",
                     "content": "second ask<system-reminder>noise</system-reminder>"}},
        {"type": "assistant", "uuid": "p-a2", "requestId": "req-P2",
         "sessionId": sid, "timestamp": "2026-06-30T02:00:05.000Z",
         "message": {"role": "assistant", "model": "claude-opus-4-8", "id": "msg-P2",
                     "content": [{"type": "tool_use", "id": "toolu-p2", "name": "Bash",
                                  "input": {"command": "echo two"}}]}},
    ]


def _build_prompt_store(tmp_path, monkeypatch, sid="sess-prompt-01"):
    """Ingest `_prompt_transcript_events` plus a subagent transcript whose first
    user turn is the task prompt it was spawned with."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", _prompt_transcript_events(sid))
    _write_jsonl(
        projects / "-fake-proj" / sid / "subagents" / "agent-ag1.jsonl",
        [{"type": "user", "uuid": "s-u1", "sessionId": sid,
          "timestamp": "2026-06-30T02:00:06.000Z",
          "message": {"role": "user", "content": "task prompt for the subagent"}},
         {"type": "assistant", "uuid": "s-a1", "requestId": "req-S1", "sessionId": sid,
          "timestamp": "2026-06-30T02:00:07.000Z",
          "message": {"role": "assistant", "model": "claude-opus-4-8", "id": "msg-S1",
                      "content": [{"type": "tool_use", "id": "toolu-s1", "name": "Bash",
                                   "input": {"command": "echo agent"}}]}}],
    )
    con = store.connect()
    try:
        store.ingest_all(con)
    finally:
        con.close()
    return sid


def test_prompt_for_command_picks_turn_before_the_command(tmp_path, monkeypatch):
    """The prompt for a command is the newest user turn at or before its
    timestamp: the first Bash call reports the first ask, the second reports the
    later one (not the first)."""
    sid = _build_prompt_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        first = store.prompt_for_command(con, sid, ts="2026-06-30T02:00:01.000Z")
        second = store.prompt_for_command(con, sid, ts="2026-06-30T02:00:05.000Z")
    finally:
        con.close()
    assert first["text"] == "first ask\nsecond line"
    # ts::VARCHAR renders a TIMESTAMPTZ in the machine's local tz, so the UTC
    # 02:00 fixture lands on 2026-06-29 or -30: assert the tz-stable year-month.
    assert first["ts"].startswith("2026-06")
    assert second["text"] == "second ask"


def test_prompt_for_command_skips_harness_generated_turns(tmp_path, monkeypatch):
    """A reminder-only turn, an interrupt marker, a task-notification and a
    local-command stdout are all harness plumbing, not prompts: a lookup landing
    on them steps back to the last thing the user actually asked."""
    sid = _build_prompt_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        # ts sits after all four auto-turns but before the second ask
        p = store.prompt_for_command(con, sid, ts="2026-06-30T02:00:03.500Z")
    finally:
        con.close()
    assert p["text"] == "first ask\nsecond line"


def test_prompt_for_command_keeps_user_invoked_commands(tmp_path, monkeypatch):
    """Running a slash command or a `!` bash line IS the user's turn, so those
    are shown rather than skipped like the harness's own turns."""
    sid = "sess-prompt-cmd"
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", [
        {"type": "user", "uuid": "c-u1", "sessionId": sid,
         "timestamp": "2026-06-30T02:00:00.000Z",
         "message": {"role": "user", "content": "an older ask"}},
        {"type": "user", "uuid": "c-u2", "sessionId": sid,
         "timestamp": "2026-06-30T02:00:01.000Z",
         "message": {"role": "user",
                     "content": "<command-name>/resume</command-name>"}},
    ])
    con = store.connect()
    try:
        store.ingest_all(con)
    finally:
        con.close()
    con = store.connect(read_only=True)
    try:
        p = store.prompt_for_command(con, sid, ts="2026-06-30T02:00:02.000Z")
    finally:
        con.close()
    assert p["text"] == "<command-name>/resume</command-name>"


def test_prompt_for_command_separates_agent_threads(tmp_path, monkeypatch):
    """A subagent's command reports that subagent's own turn (its task prompt),
    and the main thread never reports a subagent turn."""
    sid = _build_prompt_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        agent = store.prompt_for_command(
            con, sid, ts="2026-06-30T02:00:07.000Z", agent_id="ag1")
        main = store.prompt_for_command(con, sid, ts="2026-06-30T02:00:07.000Z")
    finally:
        con.close()
    assert agent["text"] == "task prompt for the subagent"
    assert main["text"] == "second ask"   # the subagent turn is not visible here


def test_prompt_for_command_no_match_returns_none(tmp_path, monkeypatch):
    """No session, an unknown session, an unknown agent thread, or a timestamp
    before the first turn all yield None (the TUI then shows its dim hint)."""
    sid = _build_prompt_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        assert store.prompt_for_command(con, "") is None
        assert store.prompt_for_command(con, "no-such-session") is None
        assert store.prompt_for_command(con, sid, agent_id="no-such-agent") is None
        assert store.prompt_for_command(con, sid, ts="2020-01-01T00:00:00.000Z") is None
    finally:
        con.close()


def test_prompt_for_command_malformed_ts_falls_back_to_newest(tmp_path, monkeypatch):
    """A hook timestamp that will not cast degrades to the newest turn in the
    thread rather than comparing against NULL and matching nothing."""
    sid = _build_prompt_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        p = store.prompt_for_command(con, sid, ts="not-a-timestamp")
    finally:
        con.close()
    assert p["text"] == "second ask"


def test_clean_prompt_text_strips_reminders_but_keeps_commands():
    """Reminder blocks are harness context and go; a slash-command block is what
    the user actually did and stays."""
    assert store.clean_prompt_text(
        "hello <system-reminder>x</system-reminder>world") == "hello world"
    assert store.clean_prompt_text("<system-reminder>only</system-reminder>") == ""
    assert store.clean_prompt_text("") == ""
    assert store.clean_prompt_text(None) == ""
    cmd = "<command-name>/resume</command-name>"
    assert store.clean_prompt_text(cmd) == cmd


# --- Turn model: prompt_turns / turn_actions / turn_prompt_text ----------------
#
# Hermetic, same shape as the fixtures above: a synthetic main transcript plus a
# subagent transcript under a fake ~/.claude/projects in tmp_path, ingested once
# and then queried through a read-only reopen (the store's own contract for every
# reader). The main session holds four main-thread turns whose windows are known
# by construction:
#
#   02:00:00  "first ask"       -> Bash echo one (main) + Read/Grep (subagent ag1)
#   02:00:05  "mid-turn nudge"  -> Bash echo two, whose tool_result is an error
#   02:00:10  "third ask"       -> nothing at all (a real, zero-call turn)
#   02:00:20  <510-char text>   -> nothing, and long enough to truncate `preview`
#
# plus a second session with one turn, so session filtering has something to
# exclude.
#
# Both transcripts carry a top-level `cwd` like the real ones do, and the fixture
# picks a LAST COMPONENT WITH A DASH (`fake-proj`): the dir-name decoding behind
# `sessions.project` turns that into `/fake/proj`, so the two columns differ and
# a test can tell which one a caller used.

_LONG_TURN_TEXT = "long turn " + "x" * 500          # 510 chars: preview truncates

_TURN_CWD = "/home/u/Projects/fake-proj"            # the exact launch dir
_TURN_SUB_CWD = "/tmp/claude/fake-proj/scratchpad"  # where the subagent ran instead


def _tool_use_line(uuid, sid, ts, request_id, blocks, out_tokens):
    """One assistant transcript line carrying `blocks` tool_use dicts under a
    single requestId, so parallel calls share one api_requests row."""
    return {"type": "assistant", "uuid": uuid, "requestId": request_id,
            "sessionId": sid, "timestamp": ts,
            "message": {"role": "assistant", "model": "claude-opus-4-8",
                        "id": f"msg-{request_id}", "content": blocks,
                        "usage": {"input_tokens": 10, "output_tokens": out_tokens,
                                  "cache_read_input_tokens": 0,
                                  "cache_creation_input_tokens": 0,
                                  "service_tier": "standard"}}}


def _tool_result_line(uuid, sid, ts, tool_use_id, is_error=False):
    """The user line carrying one tool_use's result (joined in by turn_actions)."""
    return {"type": "user", "uuid": uuid, "sessionId": sid, "timestamp": ts,
            "message": {"role": "user",
                        "content": [{"type": "tool_result",
                                     "tool_use_id": tool_use_id,
                                     "content": "out", "is_error": is_error}]},
            "toolUseResult": {"stdout": "out", "stderr": "", "interrupted": False}}


def _with_cwd(events, cwd):
    """Stamp a top-level `cwd` on every line, the way a real transcript carries it."""
    return [{**e, "cwd": cwd} for e in events]


def _turn_transcript_events(sid):
    """The main thread of the turn fixture (see the section comment)."""
    return [
        _user_prompt("t-u1", sid, "first ask", "2026-06-30T02:00:00.000Z"),
        _tool_use_line("t-a1", sid, "2026-06-30T02:00:01.000Z", "req-T1",
                       [{"type": "tool_use", "id": "toolu-t1", "name": "Bash",
                         "input": {"command": "echo one"}}], 30),
        _tool_result_line("t-r1", sid, "2026-06-30T02:00:02.000Z", "toolu-t1"),
        # a mid-turn user message: documented to START A NEW GROUP, not to extend
        # the turn above it
        _user_prompt("t-u2", sid, "mid-turn nudge", "2026-06-30T02:00:05.000Z"),
        _tool_use_line("t-a2", sid, "2026-06-30T02:00:06.000Z", "req-T2",
                       [{"type": "tool_use", "id": "toolu-t2", "name": "Bash",
                         "input": {"command": "echo two"}}], 11),
        _tool_result_line("t-r2", sid, "2026-06-30T02:00:07.000Z", "toolu-t2",
                          is_error=True),
        _user_prompt("t-u3", sid, "third ask", "2026-06-30T02:00:10.000Z"),
        _user_prompt("t-u4", sid, _LONG_TURN_TEXT, "2026-06-30T02:00:20.000Z"),
    ]


def _build_turn_store(tmp_path, monkeypatch, sid="sess-turns-01",
                      sid2="sess-turns-02"):
    """Ingest the turn fixture: main thread, a subagent whose two parallel calls
    land inside the FIRST turn's window, and a second session with one turn."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl",
                 _with_cwd(_turn_transcript_events(sid), _TURN_CWD))
    _write_jsonl(
        projects / "-fake-proj" / sid / "subagents" / "agent-ag1.jsonl",
        # The subagent's own user turn is its task prompt: it must NOT become a
        # row of its own (only main-thread turns do), and its tool calls carry the
        # PARENT session_id, so they fall inside the parent turn's window. Its cwd
        # is its own (a scratchpad) and must not reach the parent's session row.
        _with_cwd(
            [_user_prompt("t-s-u1", sid, "task prompt for ag1",
                          "2026-06-30T02:00:02.500Z"),
             _tool_use_line("t-s-a1", sid, "2026-06-30T02:00:03.000Z", "req-S1",
                            [{"type": "tool_use", "id": "toolu-sub-a", "name": "Read",
                              "input": {"file_path": "/tmp/read-me.py"}},
                             {"type": "tool_use", "id": "toolu-sub-b", "name": "Grep",
                              "input": {"pattern": "def build_"}}], 7)],
            _TURN_SUB_CWD),
    )
    _write_jsonl(projects / "-fake-proj" / f"{sid2}.jsonl", _with_cwd([
        _user_prompt("o-u1", sid2, "other session ask", "2026-06-30T03:00:00.000Z"),
        _tool_use_line("o-a1", sid2, "2026-06-30T03:00:01.000Z", "req-O1",
                       [{"type": "tool_use", "id": "toolu-o1", "name": "Bash",
                         "input": {"command": "echo other"}}], 5),
        # Two tools with no `_COMMAND_FIELD` entry, so the store stores `text`
        # NULL for both and a caller has to label them from `tool_input` at
        # render time (`action_label`). The MCP one also has a 40-char routing
        # prefix that only its last `__` segment survives (`_short_tool_name`).
        _tool_use_line("o-a2", sid2, "2026-06-30T03:00:02.000Z", "req-O2",
                       [{"type": "tool_use", "id": "toolu-o2", "name": "Agent",
                         "input": {"description": "Review phase 6",
                                   "subagent_type": "review-opus-xhigh"}},
                        {"type": "tool_use", "id": "toolu-o3",
                         "name": "mcp__plugin_linear_linear__authenticate",
                         "input": {"reason": "no known label field"}}], 6),
    ], _TURN_CWD))
    con = store.connect()
    try:
        store.ingest_all(con)
    finally:
        con.close()   # release the write lock for the read-only reopen
    return sid, sid2


def _turn(rows, prefix):
    """The single turn row whose preview starts with `prefix`."""
    hits = [r for r in rows if r["preview"].startswith(prefix)]
    assert len(hits) == 1, f"expected 1 turn starting {prefix!r}, got {len(hits)}"
    return hits[0]


def test_prompt_turns_one_row_per_main_thread_prompt_newest_first(tmp_path, monkeypatch):
    """Every main-thread prompt becomes exactly one row, newest-first, and the
    subagent's own turn (its task prompt) is not one of them."""
    sid, sid2 = _build_turn_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        rows = store.prompt_turns(con)
    finally:
        con.close()
    assert [r["preview"][:17] for r in rows] == [
        "other session ask", "long turn xxxxxxx", "third ask",
        "mid-turn nudge", "first ask"]
    assert [r["ts"] for r in rows] == sorted((r["ts"] for r in rows), reverse=True)
    assert not any("task prompt for ag1" in r["preview"] for r in rows)
    assert {r["session_id"] for r in rows} == {sid, sid2}
    # Both path keys come back, and they DISAGREE: `project` is decoded from the
    # transcript dir name (every `-` becomes `/`, so `fake-proj` splits), `cwd` is
    # the exact launch dir the transcript recorded. The TUI shows `cwd`.
    assert _turn(rows, "first ask")["project"] == "/fake/proj"
    assert _turn(rows, "first ask")["cwd"] == _TURN_CWD


def test_prompt_turns_window_ends_at_the_next_prompt(tmp_path, monkeypatch):
    """A mid-turn user message starts a NEW group (documented semantics), so the
    first turn's window closes at its timestamp and its counts stop there. Nothing
    in this fixture sits ON a bound; the test below pins those."""
    _build_turn_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        rows = store.prompt_turns(con)
    finally:
        con.close()
    first, nudge = _turn(rows, "first ask"), _turn(rows, "mid-turn nudge")
    assert first["next_ts"] == nudge["ts"]          # bounded by the next prompt
    assert first["tools"] == 3                     # echo one + the 2 subagent calls
    assert nudge["tools"] == 1                     # echo two belongs to ITS turn
    assert nudge["bash"] == 1
    # the newest turn of each session keeps an open-ended window
    assert _turn(rows, "long turn")["next_ts"] is None
    assert _turn(rows, "other session ask")["next_ts"] is None


def test_prompt_turns_window_bounds_are_lower_inclusive_upper_exclusive(tmp_path, monkeypatch):
    """The window is [ts, next_ts): a call at exactly the prompt's timestamp counts
    in that turn, one at exactly the next prompt's timestamp counts in the NEXT
    turn. Its own fixture, so the shared one's documented arithmetic stays intact.
    Pinned on both sides (prompt_turns' counts and turn_actions' list), so flipping
    `>=` to `>` or `<` to `<=` fails."""
    sid = "sess-turn-bounds"
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", [
        _user_prompt("b-u1", sid, "first ask", "2026-06-30T02:00:00.000Z"),
        # exactly ON the lower bound: belongs to "first ask"
        _tool_use_line("b-lower", sid, "2026-06-30T02:00:00.000Z", "req-B1",
                       [{"type": "tool_use", "id": "toolu-b1", "name": "Bash",
                         "input": {"command": "echo on-lower-bound"}}], 3),
        _tool_use_line("b-mid", sid, "2026-06-30T02:00:01.000Z", "req-B2",
                       [{"type": "tool_use", "id": "toolu-b2", "name": "Bash",
                         "input": {"command": "echo inside"}}], 3),
        _user_prompt("b-u2", sid, "second ask", "2026-06-30T02:00:05.000Z"),
        # exactly ON the upper bound: belongs to "second ask", not to "first ask"
        _tool_use_line("b-upper", sid, "2026-06-30T02:00:05.000Z", "req-B3",
                       [{"type": "tool_use", "id": "toolu-b3", "name": "Bash",
                         "input": {"command": "echo on-upper-bound"}}], 3),
    ])
    con = store.connect()
    try:
        store.ingest_all(con)
    finally:
        con.close()
    con = store.connect(read_only=True)
    try:
        rows = store.prompt_turns(con)
        first, second = _turn(rows, "first ask"), _turn(rows, "second ask")
        first_acts = store.turn_actions(con, sid, first["ts"], first["next_ts"])
        second_acts = store.turn_actions(con, sid, second["ts"], second["next_ts"])
    finally:
        con.close()
    assert (first["tools"], second["tools"]) == (2, 1)
    assert [a["text"] for a in first_acts] == ["echo on-lower-bound", "echo inside"]
    assert [a["text"] for a in second_acts] == ["echo on-upper-bound"]


def test_prompt_turns_attributes_subagent_calls_to_the_parent_turn(tmp_path, monkeypatch):
    """A subagent's tool calls carry the parent session_id and a non-null
    agent_id, so they count under the parent turn that spawned it."""
    _build_turn_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        first = _turn(store.prompt_turns(con), "first ask")
    finally:
        con.close()
    assert (first["tools"], first["bash"]) == (3, 1)
    assert first["agents"] == 1                    # one distinct subagent
    assert first["subcalls"] == 2                  # both of its calls


def test_prompt_turns_token_split_main_vs_subagent(tmp_path, monkeypatch):
    """Token numbers are the window's requests: 30 output on the main thread plus
    7 on the subagent, with the subagent share called out separately."""
    _build_turn_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        rows = store.prompt_turns(con)
    finally:
        con.close()
    first = _turn(rows, "first ask")
    assert first["output_tokens"] == 37
    assert first["subagent_output_tokens"] == 7
    assert first["requests"] == 2
    assert first["last_ts"] and first["last_ts"].startswith("2026-06")
    nudge = _turn(rows, "mid-turn nudge")
    assert (nudge["output_tokens"], nudge["subagent_output_tokens"]) == (11, 0)


def test_prompt_turns_zero_call_turn_reports_zero_not_none(tmp_path, monkeypatch):
    """A turn that ran nothing (a question, a resumed session) is real and must
    report 0 for every count rather than NULL, so callers need no None handling."""
    _build_turn_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        third = _turn(store.prompt_turns(con), "third ask")
    finally:
        con.close()
    for key in ("tools", "bash", "agents", "subcalls",
                "output_tokens", "subagent_output_tokens", "requests"):
        assert third[key] == 0, key
    assert third["last_ts"] is None                # no request, so no last activity


def test_prompt_turns_excludes_every_noise_prefix(tmp_path, monkeypatch):
    """Each harness-generated prefix in TURN_NOISE_PREFIXES is plumbing, not a
    prompt, so none of them becomes a row - only the one real turn does."""
    sid = "sess-turn-noise"
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    events = [_user_prompt("n-real", sid, "a real ask", "2026-06-30T02:00:00.000Z")]
    for i, prefix in enumerate(store.TURN_NOISE_PREFIXES, start=1):
        events.append(_user_prompt(f"n-{i}", sid, prefix + " trailing detail",
                                   f"2026-06-30T02:10:{i:02d}.000Z"))
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", events)
    con = store.connect()
    try:
        store.ingest_all(con)
    finally:
        con.close()
    con = store.connect(read_only=True)
    try:
        rows = store.prompt_turns(con)
    finally:
        con.close()
    assert [r["preview"] for r in rows] == ["a real ask"]


def test_prompt_turns_collapses_turns_sharing_one_timestamp(tmp_path, monkeypatch):
    """Two kept turns can share one (session_id, ts) - an image and its caption,
    a <command-name> block and a bare /compact. Left alone they would produce a
    phantom zero-width window, so they collapse to one row keeping the longest
    cleaned text."""
    sid = "sess-turn-samets"
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    ts = "2026-06-30T02:00:00.000Z"
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", [
        _user_prompt("s-short", sid, "/compact", ts),
        _user_prompt("s-long", sid, "<command-name>/compact</command-name>", ts),
    ])
    con = store.connect()
    try:
        store.ingest_all(con)
    finally:
        con.close()
    con = store.connect(read_only=True)
    try:
        rows = store.prompt_turns(con)
        # the surviving event_id must be the row the text came from, or the detail
        # view would open a different (shorter) turn than the list showed
        full = store.turn_prompt_text(con, rows[0]["event_id"])
    finally:
        con.close()
    assert len(rows) == 1
    assert rows[0]["preview"] == "<command-name>/compact</command-name>"
    assert rows[0]["next_ts"] is None              # no phantom zero-width window
    assert full == rows[0]["preview"]
    assert store.slash_label(rows[0]["preview"]) == "/compact"


def test_prompt_turns_collapse_tie_break_keeps_one_row_whole(tmp_path, monkeypatch):
    """Two kept turns at one (session_id, ts) whose cleaned texts are the SAME
    length: `(length(clean), event_id)` breaks the tie on event_id, and BOTH
    aggregates order by it, so the surviving preview and event_id still come from
    one row (the detail view opens the turn the list showed)."""
    sid = "sess-turn-tie"
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    ts = "2026-06-30T02:00:00.000Z"
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", [
        _user_prompt("tie-a", sid, "same length text A", ts),
        _user_prompt("tie-b", sid, "same length text B", ts),
    ])
    con = store.connect()
    try:
        store.ingest_all(con)
    finally:
        con.close()
    con = store.connect(read_only=True)
    try:
        rows = store.prompt_turns(con)
        full = store.turn_prompt_text(con, rows[0]["event_id"])
    finally:
        con.close()
    assert len(rows) == 1
    assert rows[0]["event_id"] == "tie-b"          # the higher event_id wins
    assert rows[0]["preview"] == full == "same length text B"


def test_prompt_turns_clean_agrees_with_clean_prompt_text(tmp_path, monkeypatch):
    """Regression: DuckDB's `trim()` strips only spaces, so the SQL clean must name
    the charset Python's `.strip()` removes (PROMPT_TRIM_CHARS). Without it a
    reminder-suffixed turn kept its trailing newline (a `preview` whose first line
    the detail view disagreed with, blank when the newline led), a noise turn behind
    a reminder survived because the leading `\\n` defeated the `^` anchor, and a
    reminder-only turn became a phantom row with preview='\\n'."""
    sid = "sess-turn-trim"
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    reminder = "<system-reminder>ctx</system-reminder>"
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", [
        _user_prompt("tr-real", sid, f"fix the bug\n{reminder}",
                     "2026-06-30T02:00:00.000Z"),
        _user_prompt("tr-noise", sid,
                     f"{reminder}\n<task-notification>agent done</task-notification>",
                     "2026-06-30T02:00:01.000Z"),
        _user_prompt("tr-empty", sid, reminder + "\n", "2026-06-30T02:00:02.000Z"),
    ])
    con = store.connect()
    try:
        store.ingest_all(con)
    finally:
        con.close()
    con = store.connect(read_only=True)
    try:
        rows = store.prompt_turns(con)
        full = store.turn_prompt_text(con, rows[0]["event_id"])
    finally:
        con.close()
    # only the real turn survives: the other two are noise and nothing at all
    assert [r["event_id"] for r in rows] == ["tr-real"]
    assert rows[0]["preview"] == full == "fix the bug"
    assert rows[0]["full_len"] == len(full) == 11


def test_prompt_turns_session_filter_and_limit(tmp_path, monkeypatch):
    """`session_id=` scopes the list to one session, `limit` caps it to the newest
    rows, and an unknown session yields an empty list."""
    sid, sid2 = _build_turn_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        mine = store.prompt_turns(con, session_id=sid)
        other = store.prompt_turns(con, session_id=sid2)
        capped = store.prompt_turns(con, limit=2)
        missing = store.prompt_turns(con, session_id="no-such-session")
    finally:
        con.close()
    assert len(mine) == 4 and {r["session_id"] for r in mine} == {sid}
    assert [r["preview"] for r in other] == ["other session ask"]
    assert [r["preview"][:17] for r in capped] == ["other session ask",
                                                   "long turn xxxxxxx"]
    assert missing == []


def test_prompt_turns_raw_scan_bounds_the_cleaned_set(tmp_path, monkeypatch):
    """raw_scan caps how many raw user turns are cleaned (74ms vs 1.96s on the
    real store): the newest ones survive, older turns are simply not fetched."""
    _build_turn_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        rows = store.prompt_turns(con, raw_scan=1)
    finally:
        con.close()
    assert [r["preview"] for r in rows] == ["other session ask"]


def test_prompt_turns_preview_truncates_and_full_text_loads_on_demand(tmp_path, monkeypatch):
    """`preview` is capped at PROMPT_PREVIEW_CHARS with `full_len` reporting the
    real size, and turn_prompt_text returns the whole cleaned text for that turn."""
    _build_turn_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        row = _turn(store.prompt_turns(con), "long turn")
        full = store.turn_prompt_text(con, row["event_id"])
        unknown = store.turn_prompt_text(con, "no-such-event")
        empty = store.turn_prompt_text(con, "")
    finally:
        con.close()
    assert len(row["preview"]) == store.PROMPT_PREVIEW_CHARS
    assert row["full_len"] == len(_LONG_TURN_TEXT)
    assert full == _LONG_TURN_TEXT
    assert unknown is None and empty is None


def test_turn_actions_oldest_first_with_agent_and_error(tmp_path, monkeypatch):
    """The detail list is every tool call in the window, oldest-first, with the
    subagent's agent_id and the joined tool_result's is_error."""
    sid, _sid2 = _build_turn_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        rows = store.prompt_turns(con)
        first, nudge = _turn(rows, "first ask"), _turn(rows, "mid-turn nudge")
        acts = store.turn_actions(con, sid, first["ts"], first["next_ts"])
        nudge_acts = store.turn_actions(con, sid, nudge["ts"], nudge["next_ts"])
    finally:
        con.close()
    assert [(a["tool_name"], a["agent_id"]) for a in acts] == [
        ("Bash", None), ("Read", "ag1"), ("Grep", "ag1")]
    assert [a["ts"] for a in acts] == sorted(a["ts"] for a in acts)
    assert acts[0]["text"] == "echo one"
    assert acts[1]["text"] == "/tmp/read-me.py"        # Read's repr field
    assert "def build_" in acts[2]["tool_input"]       # JSON comes back as text
    assert acts[0]["is_error"] is False
    # the next turn's window holds only its own call, whose result failed
    assert [(a["text"], a["is_error"]) for a in nudge_acts] == [("echo two", True)]


def test_turn_actions_open_ended_window_and_no_match(tmp_path, monkeypatch):
    """next_ts None is the open-ended window of a session's newest turn (so it
    sees every later call), and a missing session or unparseable bound returns []
    rather than raising. A falsy `ts` is refused too: with no lower bound the
    result would be the session's whole history rather than one turn."""
    sid, _sid2 = _build_turn_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        first_ts = _turn(store.prompt_turns(con), "first ask")["ts"]
        open_ended = store.turn_actions(con, sid, first_ts)
        no_session = store.turn_actions(con, "", first_ts)
        unknown = store.turn_actions(con, "no-such-session", first_ts)
        bad_ts = store.turn_actions(con, sid, "not-a-timestamp")
        no_ts = store.turn_actions(con, sid, None)
        empty_ts = store.turn_actions(con, sid, "", "2026-06-30T02:00:05.000Z")
    finally:
        con.close()
    assert [a["text"] for a in open_ended] == [
        "echo one", "/tmp/read-me.py", "def build_", "echo two"]
    assert no_session == [] and unknown == [] and bad_ts == []
    assert no_ts == [] and empty_ts == []


def test_turn_actions_lists_a_call_once_when_its_result_repeats(tmp_path, monkeypatch):
    """A tool_use can carry more than one `tool_result` row (34 tool_use_ids do in
    a real store). The call is still listed once - a fanned-out join would show it
    twice, spend two `limit` slots on it and disagree with prompt_turns' `tools`,
    which counts events unjoined - and `is_error` reads as "any result failed"."""
    sid = "sess-turn-dup-result"
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", [
        _user_prompt("d-u1", sid, "run it", "2026-06-30T02:00:00.000Z"),
        _tool_use_line("d-a1", sid, "2026-06-30T02:00:01.000Z", "req-D1",
                       [{"type": "tool_use", "id": "toolu-d1", "name": "Bash",
                         "input": {"command": "echo dup"}}], 4),
        _tool_result_line("d-r1", sid, "2026-06-30T02:00:02.000Z", "toolu-d1"),
        _tool_result_line("d-r2", sid, "2026-06-30T02:00:03.000Z", "toolu-d1",
                          is_error=True),
    ])
    con = store.connect()
    try:
        store.ingest_all(con)
    finally:
        con.close()
    con = store.connect(read_only=True)
    try:
        row = store.prompt_turns(con)[0]
        acts = store.turn_actions(con, sid, row["ts"], row["next_ts"])
    finally:
        con.close()
    assert row["tools"] == 1
    assert [(a["text"], a["is_error"]) for a in acts] == [("echo dup", True)]


def test_turn_actions_limit_caps_the_list(tmp_path, monkeypatch):
    """`limit` caps the detail list, keeping the oldest calls of the window."""
    sid, _sid2 = _build_turn_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        first_ts = _turn(store.prompt_turns(con), "first ask")["ts"]
        acts = store.turn_actions(con, sid, first_ts, limit=2)
    finally:
        con.close()
    assert [a["tool_name"] for a in acts] == ["Bash", "Read"]


def test_prompt_record_fields_match_the_query_keys(tmp_path, monkeypatch):
    """The TUI's frozen records map 1:1 onto these queries' result dicts.

    Pinned against the REAL queries rather than a copy of their keys: `from_row`
    DROPS a key the record does not declare (right, for an added column), so a
    renamed or removed one would otherwise leave the field at its default and
    render a plausible row of zeros with the suite still green. Equality both
    ways is the contract - an added key needs a field here, a removed one needs
    the field gone. This test lives in this file because it needs the duckdb
    fixtures; tests/test_claude_trail.py stays duckdb-free.
    """
    _sid, sid2 = _build_turn_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        rows = store.prompt_turns(con)
        # Checked here, before the lookups below: a renamed key would otherwise
        # raise a KeyError out of `rows[0]["ts"]` instead of showing the diff.
        assert set(rows[0]) == {f.name for f in fields(feed.PromptTurn)}
        # sid2's only turn is the newest in the store and holds every one of its
        # calls in an open-ended window, so the newest row answers for both key
        # sets.
        acts = store.turn_actions(con, sid2, rows[0]["ts"], rows[0]["next_ts"])
    finally:
        con.close()
    assert acts, "an empty action list would make the key check vacuous"
    assert set(acts[0]) == {f.name for f in fields(feed.TurnAction)}


def test_turn_page_renders_what_the_real_queries_return(tmp_path, monkeypatch):
    """The TUI's turn page against REAL query output, not hand-built records.

    Everything else about that page is tested without duckdb, which cannot catch
    the two things only the store can say: that `tools` counts the subagent's
    calls (so the Work row has to subtract) and that a subagent's calls come back
    interleaved in the parent turn's window under its own `agent_id` (so the
    gutter has runs to bracket). This fixture is the phase-1 measurement itself -
    one main-thread Bash plus a subagent's two parallel calls.
    """
    sid, _sid2 = _build_turn_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        rows = store.prompt_turns(con)
        first = _turn(rows, "first ask")
        acts = store.turn_actions(con, first["session_id"], first["ts"],
                                  first["next_ts"])
        text = store.turn_prompt_text(con, first["event_id"])
    finally:
        con.close()

    turn = feed.PromptTurn.from_row(first)
    actions = [feed.TurnAction.from_row(a) for a in acts]
    assert (turn.tools, turn.subcalls, turn.agents) == (3, 2, 1)
    assert feed._turn_work_line(turn).plain.startswith(
        "3 tool calls: 1 main, 2 across 1 subagent")

    table = feed.build_action_table(
        actions, {"ag1": {"description": "explore", "type": "Explore"}})
    # The main-thread call renders no gutter; the subagent's two are bracketed
    # as one run, labelled once.
    assert [c.plain for c in table.columns[1].cells] == ["", "┌ ag1 explore", "└ "]
    # Every call is labelled, including the two whose `text` the store derived
    # from a non-command field (a file_path and a pattern).
    assert sorted(c.plain for c in table.columns[3].cells) == [
        "/tmp/read-me.py", "def build_", "echo one"]
    assert text == "first ask"


def test_sql_clean_pipeline_agrees_with_the_python_one(tmp_path, monkeypatch):
    """`prompt_turns` cleans and classifies a turn in SQL; every other consumer
    (turn_prompt_text, prompt_for_command) does it in Python. The two must agree on
    the same text, so this runs the REAL SQL pipeline - PROMPT_CLEAN_SQL with its
    bound params, then regexp_matches on TURN_NOISE_PATTERN, in the shape
    prompt_turns uses - against clean_prompt_text + TURN_NOISE_RE.

    Every input is padded with whitespace DuckDB's `trim()` does not strip by
    default, which is where the two engines diverged. As a side effect it still
    proves _prefix_alternation's hand-rolled escaping parses identically in
    Python's `re` and DuckDB's RE2."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    reminder = "<system-reminder>ctx\nmore ctx</system-reminder>"
    texts = [
        "please refactor this, it is not noise",
        "\nfix the bug\t",
        f"fix the bug\n{reminder}",
        f"{reminder}\n",                      # nothing left: not a turn
        f" {reminder}\r\n<task-notification>done</task-notification>\v",
        "\n\t<bash-stdout>ok</bash-stdout>",
        "\r\n \t\v\f", "",
    ]
    texts += [f"\n{p} and some trailing detail\t" for p in store.TURN_NOISE_PREFIXES]
    con = store.connect()
    try:
        for text in texts:
            sql_clean, sql_noise = con.execute(
                f"""WITH raw AS (SELECT ? AS text),
                        kept AS (SELECT {store.PROMPT_CLEAN_SQL} AS clean FROM raw)
                    SELECT clean, regexp_matches(clean, ?) FROM kept""",
                [text, *store.PROMPT_CLEAN_PARAMS, store.TURN_NOISE_PATTERN],
            ).fetchone()
            py_clean = store.clean_prompt_text(text)
            assert sql_clean == py_clean, repr(text)
            assert sql_noise == bool(store.TURN_NOISE_RE.match(py_clean)), repr(text)
    finally:
        con.close()


def test_slash_label_renders_the_command_block_and_leaves_prose_alone():
    """A slash-command turn renders as `/name` (plus its args when it has any);
    anything that does not START with a command block comes back untouched."""
    assert store.slash_label("<command-name>/compact</command-name>") == "/compact"
    assert store.slash_label(
        "<command-name>/model</command-name>\n<command-message>model</command-message>"
        "\n<command-args>opus</command-args>") == "/model opus"
    assert store.slash_label(
        "<command-name>/clear</command-name><command-args></command-args>") == "/clear"
    assert store.slash_label("<command-name>compact</command-name>") == "/compact"
    assert store.slash_label("  <command-name>/compact</command-name>") == "/compact"
    plain = "lets plan a new feature"
    assert store.slash_label(plain) == plain
    assert store.slash_label("") == ""
    assert store.slash_label(None) is None
    # a block quoted mid-prose is a question ABOUT the command, not an invocation:
    # collapsing the turn to `/compact` would throw the question away
    prose = ("how does <command-name>/compact</command-name> behave when I ask "
             "this long question?")
    assert store.slash_label(prose) == prose
    # and the args must belong to the head block, not to a later one
    assert store.slash_label("<command-name>/clear</command-name>\n"
                             "then <command-args>opus</command-args>") == "/clear"


def test_slash_label_accepts_a_command_message_before_the_name():
    """A plugin/skill slash command emits `<command-message>` FIRST, so
    `<command-name>` is not at the head of the turn.

    Real payloads from the live store (3 of its newest 200 turns), which used to
    fall through to the raw text and render as XML in the TUI's Prompt cell.
    """
    assert store.slash_label(
        "<command-message>dev-orchestrate</command-message>\n"
        "<command-name>/dev-orchestrate</command-name>\n"
        "<command-args>--repo ~/Projects/personal/claude-trail</command-args>"
    ) == "/dev-orchestrate --repo ~/Projects/personal/claude-trail"
    assert store.slash_label(
        "<command-message>m2-standup</command-message>\n"
        "<command-name>/m2-standup</command-name>\n"
        "<command-args></command-args>") == "/m2-standup"
    # Still head-ANCHORED (what phase 2's review required): a leading run of
    # `<command-*>` elements is accepted, prose before them is not.
    prose = ("does <command-message>x</command-message>"
             "<command-name>/y</command-name> matter?")
    assert store.slash_label(prose) == prose


def test_prompt_for_command_skips_an_image_echo_sharing_the_timestamp(tmp_path, monkeypatch):
    """Regression: an `[Image: source: ...]` echo is emitted at the SAME timestamp
    as the text the user typed, so `ORDER BY ts DESC LIMIT 1` could return the
    echo as the prompt. The shared noise filter drops it either way."""
    sid = "sess-image-echo"
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    ts = "2026-06-30T02:00:00.000Z"
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", [
        _user_prompt("i-typed", sid, "look at this screenshot", ts),
        _user_prompt("i-echo", sid, "[Image: source: /tmp/shot.png]", ts),
    ])
    con = store.connect()
    try:
        store.ingest_all(con)
    finally:
        con.close()
    con = store.connect(read_only=True)
    try:
        p = store.prompt_for_command(con, sid, ts="2026-06-30T02:00:01.000Z")
    finally:
        con.close()
    assert p["text"] == "look at this screenshot"


# --- find_turn + the `prompts` CLI --------------------------------------------
#
# Over the same `_build_turn_store` fixture as the turn-model tests above, so the
# windows the CLI prints are the ones already pinned there. Rendered timestamps
# are local (`ts::VARCHAR`), so every assertion goes through a preview, an
# event_id or a UTC `Z` argument rather than a printed clock time.


def _prompts(*args):
    """Run `prompts` through cli_main (the dispatch the TUI hands off to) and
    return its exit code."""
    return store.cli_main(["prompts", *args])


def _prompt_cells(out):
    """The Prompt cell of each list row: the last column, which is the only one
    the printer leaves unpadded."""
    return [ln.split("  ")[-1].strip() for ln in out.splitlines()[1:]]


def _build_session_store(tmp_path, monkeypatch, sid, events):
    """Ingest ONE synthetic transcript and return its session id.

    `_build_turn_store`'s shape for the cases below that need timestamps or text
    of their own (turns inside one second, a noise-heavy history, control
    characters in a prompt) rather than the shared fixture's."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", events)
    con = store.connect()
    try:
        store.ingest_all(con)
    finally:
        con.close()   # release the write lock for the read-only reopen
    return sid


def _one_second_events(sid):
    """Three turns, two of them 800ms apart INSIDE one second: the shape a
    rendered `HH:MM:SS` cannot tell apart but a full-precision `ts` can."""
    return [
        _user_prompt("os-u1", sid, "before the pair", "2026-06-30T02:00:09.000Z"),
        _user_prompt("os-u2", sid, "earlier of the pair", "2026-06-30T02:00:10.100Z"),
        _user_prompt("os-u3", sid, "later of the pair", "2026-06-30T02:00:10.900Z"),
    ]


# The long turn's Prompt cell: its 400-char preview cut to fit the list's
# one-line cap, with the cut marked by the same `…` `short_path` uses for the
# Project cell of the same row.
_LONG_TURN_CELL = ("long turn " + "x" * 500)[:store.PROMPT_CLI_PREVIEW_CHARS - 1] + "…"


def test_find_turn_by_event_id_and_by_nearest_timestamp(tmp_path, monkeypatch):
    """One turn, resolved either way: an event_id lands on exactly that turn, and
    a timestamp lands on the newest turn AT OR BEFORE it (the `cat --at` rule, so
    a whole-second time copied out of the list still resolves)."""
    sid, _sid2 = _build_turn_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        listed = _turn(store.prompt_turns(con), "first ask")
        by_id = store.find_turn(con, event_id=listed["event_id"])
        # 2.5s into the first turn: no turn starts here, so it steps back to it
        mid = store.find_turn(con, session_id=sid, at="2026-06-30T02:00:02.500Z")
        # exactly on the next prompt: that turn, not the one before it
        on_bound = store.find_turn(con, session_id=sid, at="2026-06-30T02:00:05.000Z")
    finally:
        con.close()
    assert by_id == listed                      # same keys AND same aggregates
    assert mid["preview"] == "first ask"
    assert mid["next_ts"] == listed["next_ts"]  # the window, not just the prompt
    assert on_bound["preview"] == "mid-turn nudge"


def test_find_turn_at_matches_the_printed_second(tmp_path, monkeypatch):
    """A rendered timestamp is whole seconds while `events.ts` keeps
    milliseconds, so the `at` comparison truncates the turn's ts to the second.
    Without that, the exact string the list printed for a turn resolves to the
    PREVIOUS one, which is the one thing `--at` must never do."""
    sid = "sess-find-subsecond"
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", [
        _user_prompt("ss-u1", sid, "earlier ask", "2026-06-30T02:00:00.000Z"),
        _user_prompt("ss-u2", sid, "later ask", "2026-06-30T02:00:10.112Z"),
    ])
    con = store.connect()
    try:
        store.ingest_all(con)
    finally:
        con.close()
    con = store.connect(read_only=True)
    try:
        printed = store.find_turn(con, session_id=sid, at="2026-06-30T02:00:10.000Z")
        before = store.find_turn(con, session_id=sid, at="2026-06-30T02:00:09.000Z")
    finally:
        con.close()
    assert printed["preview"] == "later ask"    # the second it is printed as
    assert before["preview"] == "earlier ask"   # a whole second earlier: still before


def test_find_turn_at_tells_two_turns_inside_one_second_apart(tmp_path, monkeypatch):
    """Which rule applies is decided by the BOUND, not by the turn.

    Two turns 800ms apart print the same `HH:MM:SS`, so that whole-second string
    resolves to the NEWER of them - the row the list shows for that second -
    while each turn's own full-precision `ts` (what `prompts --json` prints and
    `_turn_time` invites pasting back) resolves to ITSELF. Truncating the turn's
    ts against an untruncated bound made the earlier turn's own timestamp answer
    with the LATER turn: measured at 9 of the newest 400 turns of a real store,
    every one of them a `/compact` pair ~100ms apart, each handed the wrong
    (open-ended) window."""
    sid = "sess-one-second"
    _build_session_store(tmp_path, monkeypatch, sid, _one_second_events(sid))
    con = store.connect(read_only=True)
    try:
        rows = store.prompt_turns(con, session_id=sid)
        earlier, later = _turn(rows, "earlier of"), _turn(rows, "later of")
        printed = store._turn_time(earlier["ts"])
        by_printed = store.find_turn(con, session_id=sid, at=printed)
        by_earlier_ts = store.find_turn(con, session_id=sid, at=earlier["ts"])
        by_later_ts = store.find_turn(con, session_id=sid, at=later["ts"])
    finally:
        con.close()
    assert printed == store._turn_time(later["ts"])      # one printed second...
    assert by_printed["event_id"] == later["event_id"]   # ...lands on the newer
    assert by_earlier_ts["event_id"] == earlier["event_id"]
    assert by_later_ts["event_id"] == later["event_id"]
    # and the window that came with it is the earlier turn's ~800ms one, not the
    # later turn's open-ended one
    assert by_earlier_ts["next_ts"] == later["ts"]


def test_find_turn_returns_none_for_what_is_not_a_turn(tmp_path, monkeypatch):
    """An unknown id, a subagent's own turn, a tool_use row, a time before the
    session's first prompt, and an `at` with no session all resolve to nothing."""
    sid, _sid2 = _build_turn_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        assert store.find_turn(con, event_id="no-such-event") is None
        assert store.find_turn(con, event_id="t-s-u1") is None      # subagent turn
        assert store.find_turn(con, event_id="toolu-t1") is None    # a tool call
        assert store.find_turn(con, session_id=sid,
                               at="2020-01-01T00:00:00.000Z") is None
        assert store.find_turn(con, at="2026-06-30T02:00:02.500Z") is None
        assert store.find_turn(con, session_id=sid) is None
    finally:
        con.close()


def test_find_turn_ignores_an_event_id_that_lost_the_collapse(tmp_path, monkeypatch):
    """Turns sharing one (session_id, ts) collapse to the longest text, so only
    the WINNING event_id is a turn of this model; the other one is not a row the
    list could ever have shown."""
    sid = "sess-find-samets"
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    ts = "2026-06-30T02:00:00.000Z"
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", [
        _user_prompt("f-short", sid, "/compact", ts),
        _user_prompt("f-long", sid, "<command-name>/compact</command-name>", ts),
    ])
    con = store.connect()
    try:
        store.ingest_all(con)
    finally:
        con.close()
    con = store.connect(read_only=True)
    try:
        winner = store.find_turn(con, event_id="f-long")
        loser = store.find_turn(con, event_id="f-short")
    finally:
        con.close()
    assert winner["preview"] == "<command-name>/compact</command-name>"
    assert loser is None


def test_prompt_turns_since_keeps_the_bound_inclusive(tmp_path, monkeypatch):
    """`since` is a lower bound on the turn's own timestamp: a turn exactly on it
    is in, the one just before it is out."""
    _build_turn_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        at_bound = store.prompt_turns(con, since="2026-06-30T02:00:05.000Z")
        just_after = store.prompt_turns(con, since="2026-06-30T02:00:05.001Z")
        unreadable = store.prompt_turns(con, since="not-a-timestamp")
    finally:
        con.close()
    assert [r["preview"][:14] for r in at_bound] == [
        "other session ", "long turn xxxx", "third ask", "mid-turn nudge"]
    assert "mid-turn nudge" not in [r["preview"] for r in just_after]
    # try_cast turns a value DuckDB cannot read into NULL, so it matches nothing;
    # cmd_prompts checks the string before it gets here.
    assert unreadable == []


def test_prompt_turns_since_fills_the_limit_from_the_bounded_set(tmp_path, monkeypatch):
    """The bound goes INSIDE the query, so `limit` is filled out of the turns that
    survive it. Filtering the list afterwards in Python would spend the limit on
    turns older than the bound and hand back fewer rows than were asked for."""
    _build_turn_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        rows = store.prompt_turns(con, limit=2, since="2026-06-30T02:00:05.000Z")
    finally:
        con.close()
    assert len(rows) == 2
    assert [r["preview"][:14] for r in rows] == ["other session ", "long turn xxxx"]


def test_cmd_prompts_lists_turns_newest_first(tmp_path, monkeypatch, capsys):
    """The list is one line per turn under a header, newest first, carrying the
    Prompts tab's numbers: calls with their Bash share, the subagent count, the
    window's output tokens with the subagent part, and the prompt's first line."""
    _build_turn_store(tmp_path, monkeypatch)
    assert _prompts() == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["time", "sess", "project", "calls", "agents",
                                "tokens", "prompt"]
    assert _prompt_cells("\n".join(lines)) == [
        "other session ask", _LONG_TURN_CELL, "third ask",
        "mid-turn nudge", "first ask"]
    first = next(ln for ln in lines if ln.endswith("first ask"))
    assert "3 (1 bash)" in first          # window total, INCLUDING the subagent's
    assert "37 (+7 sub)" in first         # output tokens, subagent share called out
    assert "sess-tur" in first            # the short session id
    assert "fake-proj" in first           # the session's exact cwd, not `/fake/proj`
    third = next(ln for ln in lines if ln.endswith("third ask"))
    assert " - " in third                 # a zero-call turn reads `-`, not `0`


def test_cmd_prompts_session_and_limit_scope_the_list(tmp_path, monkeypatch, capsys):
    """`--session` scopes to one session (by prefix, like `search`/`tokens`) and
    `--limit` caps the rows."""
    _sid, _sid2 = _build_turn_store(tmp_path, monkeypatch)
    assert _prompts("--session", "sess-turns-02") == 0
    assert _prompt_cells(capsys.readouterr().out) == ["other session ask"]
    assert _prompts("--session", "sess-turns-01", "--limit", "2") == 0
    assert _prompt_cells(capsys.readouterr().out) == [_LONG_TURN_CELL, "third ask"]
    # an unknown session is an empty list, not an error: the header still prints
    assert _prompts("--session", "sess-nope") == 0
    assert capsys.readouterr().out.splitlines() == [
        "time  sess  project  calls  agents  tokens  prompt"]


def test_cmd_prompts_ambiguous_session_prefix_is_refused(tmp_path, monkeypatch, capsys):
    """Both fixture sessions start `sess-turns-0`, and silently picking one would
    misreport whose turns these are."""
    _build_turn_store(tmp_path, monkeypatch)
    assert _prompts("--session", "sess-turns-0") == 2
    err = capsys.readouterr().err
    assert "matches 2 sessions" in err


def test_cmd_prompts_since_excludes_turns_before_the_bound(tmp_path, monkeypatch, capsys):
    """`--since` drops the turns older than the bound and keeps the one on it."""
    _build_turn_store(tmp_path, monkeypatch)
    assert _prompts("--session", "sess-turns-01",
                    "--since", "2026-06-30T02:00:05.000Z") == 0
    kept = _prompt_cells(capsys.readouterr().out)
    assert kept[-1] == "mid-turn nudge"      # exactly on the bound: in
    assert "first ask" not in kept           # five seconds before it: out


def test_cmd_prompts_at_takes_back_both_times_the_list_printed(tmp_path, monkeypatch, capsys):
    """The chain `_turn_time` advertises, end to end: copy a time out of the
    list, paste it into `--at`. The text list prints ONE whole second for two
    turns 800ms apart and that string opens the newer of them; `--json` prints
    each turn's full-precision `ts` and that one opens the turn it came from."""
    sid = "sess-one-second"
    _build_session_store(tmp_path, monkeypatch, sid, _one_second_events(sid))
    assert _prompts("--session", sid, "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    earlier, later = _turn(rows, "earlier of"), _turn(rows, "later of")
    assert _prompts("--session", sid) == 0
    printed = [ln.split("  ")[0] for ln in capsys.readouterr().out.splitlines()[1:]]
    assert printed[0] == printed[1]          # both turns, one printed second
    for at, expected in ((printed[0], later["event_id"]),
                         (earlier["ts"], earlier["event_id"]),
                         (later["ts"], later["event_id"])):
        assert _prompts("--session", sid, "--at", at) == 0, at
        assert f"event-id  {expected}" in capsys.readouterr().out, at


def test_cmd_prompts_limit_is_filled_past_the_dropped_noise_turns(tmp_path, monkeypatch,
                                                                  capsys):
    """`--limit N` means N rows, not "whatever N raw turns survive".

    The scan bounds RAW user turns and the noise filter drops them AFTER it, so
    on a history that is 3/4 harness noise a single pass comes back short (on the
    live store, a fixed 2x scan for `--limit 500` returned 466). The scan widens
    until the limit is filled, and stops once it covers every turn there is."""
    sid = "sess-noisy-scan"
    events = []
    for i in range(16):
        real = i % 4 == 3
        events.append(_user_prompt(
            f"ns-u{i}", sid,
            f"real ask {i}" if real else f"<task-notification>agent {i}</task-notification>",
            f"2026-06-30T02:{i:02d}:00.000Z"))
    _build_session_store(tmp_path, monkeypatch, sid, events)
    # The floor is the TUI's measured 400, which would swallow this whole fixture
    # in one pass; shrink it so the widening is what fills the limit.
    monkeypatch.setattr(store, "PROMPT_TURN_RAW_SCAN", 4)
    assert _prompts("--session", sid, "--limit", "4") == 0
    assert _prompt_cells(capsys.readouterr().out) == [
        "real ask 15", "real ask 11", "real ask 7", "real ask 3"]
    # more than the store holds: what exists, and no endless widening
    assert _prompts("--session", sid, "--limit", "50") == 0
    assert len(_prompt_cells(capsys.readouterr().out)) == 4


def test_cmd_prompts_strips_terminal_control_characters(tmp_path, monkeypatch, capsys):
    """Prompt text is the likeliest place in this store to find PASTED TERMINAL
    OUTPUT, and this printer writes to a real terminal: an ANSI escape or a BEL
    in a prompt (or in a command it ran) must print as text rather than be obeyed
    by the terminal reading it. `--json` keeps them, escaped as `\\uXXXX`."""
    sid = "sess-control-chars"
    _build_session_store(tmp_path, monkeypatch, sid, [
        _user_prompt("cc-u1", sid, "\x1b[31mRED\x1b[0m and a \x07bell\nsecond line",
                     "2026-06-30T02:00:00.000Z"),
        _tool_use_line("cc-a1", sid, "2026-06-30T02:00:01.000Z", "req-C1",
                       [{"type": "tool_use", "id": "toolu-c1", "name": "Bash",
                         "input": {"command": "echo \x1b[2Jclear"}}], 5),
    ])
    assert _prompts("--session", sid) == 0
    listed = capsys.readouterr().out
    assert _prompt_cells(listed) == ["[31mRED[0m and a bell"]
    assert _prompts("--session", sid, "--at", "2026-06-30T02:00:02.000Z") == 0
    dumped = capsys.readouterr().out
    assert "prompt    [31mRED[0m and a bell" in dumped
    assert dumped.rstrip().endswith("echo [2Jclear")
    for out in (listed, dumped):
        assert "\x1b" not in out and "\x07" not in out
    assert _prompts("--session", sid, "--json") == 0
    assert "\\u001b" in capsys.readouterr().out


def test_cmd_prompts_json_round_trips(tmp_path, monkeypatch, capsys):
    """`--json` is the scriptable form: a JSON array of the query's own dicts,
    with the full 400-char preview the text list shortens."""
    _build_turn_store(tmp_path, monkeypatch)
    assert _prompts("--json", "--limit", "2") == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["preview"][:14] for r in rows] == ["other session ", "long turn xxxx"]
    assert set(rows[0]) == set(store._TURN_ROW_KEYS)
    assert len(_turn(rows, "long turn")["preview"]) == store.PROMPT_PREVIEW_CHARS


def test_cmd_prompts_at_dumps_one_turns_tool_calls(tmp_path, monkeypatch, capsys):
    """`--at` + `--session` resolves one turn and prints its calls oldest-first:
    the main thread's, then the subagent's under its short id."""
    _build_turn_store(tmp_path, monkeypatch)
    assert _prompts("--session", "sess-turns-01",
                    "--at", "2026-06-30T02:00:02.500Z") == 0
    out = capsys.readouterr().out
    head, table = out.split("\n\n", 1)
    assert "session   sess-turns-01  /home/u/Projects/fake-proj" in head
    # tools is INCLUSIVE of subcalls, so the main-thread count is a subtraction
    assert "work      3 tool calls: 1 main, 2 across 1 subagent, 1 bash" in head
    assert "tokens    37 output, 7 by subagents, 2 requests" in head
    assert "prompt    first ask" in head
    rows = [ln.split() for ln in table.splitlines() if ln]
    assert rows[0] == ["time", "agent", "tool", "detail"]
    assert [(r[1], r[2]) for r in rows[1:]] == [
        ("main", "Bash"), ("ag1", "Read"), ("ag1", "Grep")]
    assert table.rstrip().endswith("def build_")


def test_cmd_prompts_event_id_labels_calls_the_store_left_blank(tmp_path, monkeypatch, capsys):
    """`--event-id` takes the id the list's `--json` prints, and the Agent/MCP
    calls it dumps have no stored `text`: their label comes from `tool_input`, and
    an MCP tool keeps only its own name."""
    _build_turn_store(tmp_path, monkeypatch)
    assert _prompts("--json", "--session", "sess-turns-02") == 0
    event_id = json.loads(capsys.readouterr().out)[0]["event_id"]
    assert _prompts("--event-id", event_id) == 0
    out = capsys.readouterr().out
    assert f"event-id  {event_id}" in out
    assert "work      3 tool calls: 1 bash" in out
    rows = [ln.split(None, 3) for ln in out.split("\n\n", 1)[1].splitlines() if ln]
    assert [(r[2], r[3]) for r in rows[1:]] == [
        ("Bash", "echo other"),
        ("Agent", "Review phase 6"),          # from tool_input.description
        ("authenticate", "-")]                # mcp__plugin_linear_linear__ dropped


def test_cmd_prompts_marks_a_failed_call(tmp_path, monkeypatch, capsys):
    """A call whose tool_result came back `is_error` is marked at the END of its
    line, so the Detail column keeps its alignment and the marker greps."""
    _build_turn_store(tmp_path, monkeypatch)
    assert _prompts("--session", "sess-turns-01",
                    "--at", "2026-06-30T02:00:05.000Z") == 0
    out = capsys.readouterr().out
    assert "prompt    mid-turn nudge" in out
    assert out.rstrip().endswith("echo two  [error]")


def test_cmd_prompts_json_single_turn_carries_its_actions(tmp_path, monkeypatch, capsys):
    """The single-turn `--json` is the turn's own dict plus its `actions` list."""
    _build_turn_store(tmp_path, monkeypatch)
    assert _prompts("--json", "--session", "sess-turns-01",
                    "--at", "2026-06-30T02:00:02.500Z") == 0
    turn = json.loads(capsys.readouterr().out)
    assert set(turn) == set(store._TURN_ROW_KEYS) | {"actions"}
    assert turn["preview"] == "first ask"
    assert [(a["tool_name"], a["agent_id"]) for a in turn["actions"]] == [
        ("Bash", None), ("Read", "ag1"), ("Grep", "ag1")]


def test_cmd_prompts_zero_call_turn_says_so(tmp_path, monkeypatch, capsys):
    """A turn that ran nothing is real (a question, a slash command, a resumed
    session), so the dump says that rather than printing an empty table."""
    _build_turn_store(tmp_path, monkeypatch)
    assert _prompts("--session", "sess-turns-01",
                    "--at", "2026-06-30T02:00:10.000Z") == 0
    out = capsys.readouterr().out
    assert "work      no tool calls" in out
    assert "tokens    no requests attributed to this window yet" in out
    assert out.rstrip().endswith("(no tool calls in this turn)")
    # and no column header standing over the nothing it would describe
    assert "detail" not in out


def test_cmd_prompts_unresolvable_turn_exits_1(tmp_path, monkeypatch, capsys):
    """A turn that is not there is exit 1 with one line, not an empty dump."""
    _build_turn_store(tmp_path, monkeypatch)
    assert _prompts("--session", "sess-turns-01",
                    "--at", "2020-01-01T00:00:00.000Z") == 1
    assert "no turn at or before" in capsys.readouterr().err
    assert _prompts("--event-id", "no-such-event") == 1
    assert "no turn with event-id" in capsys.readouterr().err


def test_cmd_prompts_missing_store_exits_1(tmp_path, monkeypatch, capsys):
    """Opening a store that is not there read-only would raise, so say where it
    would live and how to make one (`cmd_stats` guards the same way)."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "nope.duckdb")
    assert _prompts() == 1
    assert "no audit store at" in capsys.readouterr().err


def test_cmd_prompts_refuses_bad_arguments(tmp_path, monkeypatch, capsys):
    """Every argument error is exit 2 with a reason plus the usage line - and an
    unknown flag is an error, not something to ignore: `--sesion X` listing every
    session would look like a working command."""
    _build_turn_store(tmp_path, monkeypatch)
    for args, reason in [
        (("--sesion", "x"), "unknown argument: --sesion"),
        (("--session",), "--session needs a value"),
        (("--limit", "many"), "--limit wants a number"),
        (("--at", "2026-06-30T02:00:00.000Z"), "--at needs --session"),
        (("--at", "x", "--event-id", "y"), "both select one turn"),
        (("--session", "sess-turns-01", "--at", "nope"), "--at wants a timestamp"),
        (("--since", "nope"), "--since wants a timestamp"),
        # an event_id carries its own session, so honouring --session too would
        # mean dumping one session's turn under another's name
        (("--session", "sess-turns-01", "--event-id", "t-u1"),
         "--event-id names its own session"),
        # the list's bounds against the one-turn forms: dropped silently before
        (("--event-id", "t-u1", "--since", "2026-06-30T02:00:00.000Z"),
         "--since bound the LIST"),
        (("--session", "sess-turns-01", "--at", "2026-06-30T02:00:00.000Z",
          "--limit", "5"), "--limit bound the LIST"),
    ]:
        assert _prompts(*args) == 2, args
        assert reason in capsys.readouterr().err, args


def test_cmd_prompts_help_prints_usage(tmp_path, monkeypatch, capsys):
    """`--help` is the usage line on stdout and exit 0, not an error."""
    _build_turn_store(tmp_path, monkeypatch)
    assert _prompts("--help") == 0
    assert capsys.readouterr().out.startswith("usage: claude-trail prompts")


def test_cli_main_dispatches_prompts(monkeypatch, capsys):
    """`prompts` is wired into cli_main's table, and a busy store on that path
    degrades to the same one-line message the other read commands give."""
    seen = []
    monkeypatch.setitem(store.cli_main.__globals__, "cmd_prompts",
                        lambda argv: seen.append(argv) or 0)
    assert store.cli_main(["prompts", "--limit", "5"]) == 0
    assert seen == [["prompts", "--limit", "5"]]

    def busy(argv):
        raise Exception('IO Error: Could not set lock on file "x": Conflicting lock')
    monkeypatch.setitem(store.cli_main.__globals__, "cmd_prompts", busy)
    assert store.cli_main(["prompts"]) == 1
    assert "store busy" in capsys.readouterr().err


def test_session_token_totals(tmp_path, monkeypatch):
    """session_token_totals returns the exact per-session totals (one request:
    50 out / 100 in / 200 cache-read / 30 cache-create, no subagent spend)."""
    sid = _build_cli_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        t = store.session_token_totals(con, sid)
    finally:
        con.close()
    assert t == {"requests": 1, "output_tokens": 50, "input_tokens": 100,
                 "cache_read_tokens": 200, "cache_creation_tokens": 30,
                 "subagent_output_tokens": 0}


def test_session_token_totals_no_match_returns_none(tmp_path, monkeypatch):
    _build_cli_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        assert store.session_token_totals(con, "no-such-session") is None
        assert store.session_token_totals(con, "") is None
    finally:
        con.close()


def test_checkpoint_due_skips_when_nothing_stored(tmp_path, monkeypatch):
    """An idle sweep has nothing to flush, so it never pays the write-lock cost
    of a CHECKPOINT."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    due, why = store._checkpoint_due(False)
    assert due is False
    assert "nothing stored" in why


def test_checkpoint_due_flushes_when_no_stamp_yet(tmp_path, monkeypatch):
    """First run after a fresh store (no stamp): flush, so the WAL is not carried
    indefinitely."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    due, why = store._checkpoint_due(True)
    assert due is True
    assert "no flush recorded yet" in why


def test_checkpoint_due_throttles_on_a_fresh_stamp(tmp_path, monkeypatch):
    """A flush minutes ago means this run skips: at a 30s cadence the flush, not
    the sweep, is what holds the lock against readers."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    store._checkpoint_stamp().touch()
    due, why = store._checkpoint_due(True)
    assert due is False
    assert "min 300s" in why


def test_checkpoint_due_flushes_once_the_interval_passes(tmp_path, monkeypatch):
    """An old stamp lets the next storing run flush again."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    stamp = store._checkpoint_stamp()
    stamp.touch()
    old = time.time() - store.CHECKPOINT_MIN_INTERVAL - 60
    os.utime(stamp, (old, old))
    due, why = store._checkpoint_due(True)
    assert due is True
    assert "since last flush" in why


def test_checkpoint_due_force_overrides_every_skip(tmp_path, monkeypatch):
    """--compact (a rewrite follows) and --watch (its one flush is at shutdown)
    must land on a flushed store, even on an idle sweep with a fresh stamp."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    store._checkpoint_stamp().touch()
    assert store._checkpoint_due(False, force=True) == (True, "forced")


def test_ingest_skips_the_checkpoint_on_an_idle_rerun(tmp_path, monkeypatch, capsys):
    """End to end: the first ingest stores rows and flushes; an immediate re-run
    has nothing new, so it reports a skipped checkpoint instead of taking the
    multi-second lock again."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    sid = "sess-ckpt-0001"
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", _main_transcript_events(sid))

    assert store.cmd_ingest(["ingest"]) == 0
    first = capsys.readouterr().out
    assert "checkpoint: flushing WAL to disk" in first
    assert store.cmd_ingest(["ingest"]) == 0
    second = capsys.readouterr().out
    assert "checkpoint: skipped (nothing stored)" in second


def test_a_throttled_run_really_skips_the_flush(tmp_path, monkeypatch, capsys):
    """DuckDB checkpoints on close unless told not to, which once made the
    throttle a no-op. A skipped run must leave its WAL on disk with the rows
    still readable, and the next due run flushes that WAL even when it stores
    nothing new."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    for sid in ("sess-ckpt-0002", "sess-ckpt-0003"):
        _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", _main_transcript_events(sid))
        assert store.cmd_ingest(["ingest"]) == 0
    out = capsys.readouterr().out
    assert "checkpoint: flushing WAL to disk (no flush recorded yet)" in out
    assert "checkpoint: skipped (last flush" in out
    assert store._wal_path().exists(), "close() must not flush a skipped run"
    con = store.connect(read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM sessions").fetchone()[0] == 2
    finally:
        con.close()

    old = time.time() - store.CHECKPOINT_MIN_INTERVAL - 60
    os.utime(store._checkpoint_stamp(), (old, old))
    assert store.cmd_ingest(["ingest"]) == 0
    assert "checkpoint: flushing WAL to disk" in capsys.readouterr().out
    assert not store._wal_path().exists()


def test_is_lock_conflict_matches_duckdb_lock_errors_only():
    """The lock check keys off DuckDB's message; unrelated failures must still
    surface as themselves."""
    assert store.is_lock_conflict(Exception(
        'IO Error: Could not set lock on file "/x/trail.duckdb": Conflicting '
        'lock is held in /usr/bin/python3.14 (PID 264087) by user naka.'))
    assert store.is_lock_conflict(Exception("Conflicting lock is held"))
    assert not store.is_lock_conflict(Exception("Catalog Error: Table not found"))
    assert not store.is_lock_conflict(ValueError("bad input"))


def test_connect_writer_returns_none_after_retrying_a_lock_conflict(monkeypatch):
    """A reader holding the store makes the writer give up quietly (None), after
    retrying, instead of raising: the ingest is idempotent and runs on a timer."""
    calls = []

    def fake_connect(read_only=False):
        calls.append(read_only)
        raise Exception('IO Error: Could not set lock on file "x": Conflicting lock')

    monkeypatch.setattr(store, "connect", fake_connect)
    monkeypatch.setattr(store.time, "sleep", lambda s: None)
    assert store.connect_writer(attempts=3, delay=0) is None
    assert len(calls) == 3          # retried, not one-and-done


def test_connect_writer_reraises_other_errors(monkeypatch):
    def fake_connect(read_only=False):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(store, "connect", fake_connect)
    with pytest.raises(RuntimeError):
        store.connect_writer(attempts=2, delay=0)


def test_ingest_skips_cleanly_when_a_reader_holds_the_store(monkeypatch, capsys):
    """The whole run degrades to one log line and exit 0, so a routine collision
    with the TUI's store reads is not recorded as a unit failure."""
    monkeypatch.setattr(store, "connect_writer", lambda *a, **k: None)
    assert store.cmd_ingest(["ingest"]) == 0
    assert "skipped: store is held by a reader" in capsys.readouterr().out


def test_cli_main_reports_a_busy_store_instead_of_a_traceback(monkeypatch, capsys):
    """A read command that lands on the ingest's write lock explains itself and
    exits 1; anything else still propagates."""
    def busy(argv):
        raise Exception('IO Error: Could not set lock on file "x": Conflicting lock')
    monkeypatch.setitem(
        store.cli_main.__globals__, "cmd_search", busy)
    assert store.cli_main(["search", "anything"]) == 1
    assert "store busy" in capsys.readouterr().err


def test_cmd_stats_prints_counts_including_output_tokens(tmp_path, monkeypatch, capsys):
    """stats prints each labelled count block, including the api-requests /
    output-tokens line the spec pins."""
    _build_cli_store(tmp_path, monkeypatch)
    assert store.cmd_stats(["stats"]) == 0
    out = capsys.readouterr().out
    assert "== sessions ==" in out
    assert "== events ==" in out
    assert "== api requests / output tokens ==" in out
    assert "Bash" in out                # by-tool block
    assert "50" in out                  # sum(output_tokens) on the api-requests line


def test_cmd_stats_includes_database_section(tmp_path, monkeypatch, capsys):
    """stats leads with a `== database ==` block: connection string, duckdb
    version, and per-table row counts from db_info."""
    _build_cli_store(tmp_path, monkeypatch)
    assert store.cmd_stats(["stats"]) == 0
    out = capsys.readouterr().out
    assert "== database ==" in out
    assert "connection" in out
    assert "trail.duckdb" in out
    assert "duckdb" in out
    assert "rows events" in out


def test_db_info_reports_connection_version_and_counts(tmp_path, monkeypatch):
    """db_info returns (label, value) pairs including the connection path, a
    non-empty duckdb version, and per-table row counts for a populated store."""
    _build_cli_store(tmp_path, monkeypatch)
    con = store.connect(read_only=True)
    try:
        info = dict(store.db_info(con))
    finally:
        con.close()
    assert info["connection"].endswith("trail.duckdb")
    assert "source" in info
    assert info["duckdb"]                          # non-empty version string
    assert "rows events" in info
    assert int(info["rows events"].replace(",", "")) >= 1


def test_cmd_stats_missing_db_reports_location(tmp_path, monkeypatch, capsys):
    """On a not-yet-created store, stats reports where it would live and how to
    create it (return 0) instead of raising on the read-only open."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "does-not-exist.duckdb")
    assert store.cmd_stats(["stats"]) == 0
    out = capsys.readouterr().out
    assert "== database ==" in out
    assert "not created yet" in out


def test_cmd_ingest_logs_summary(tmp_path, monkeypatch, capsys):
    """cmd_ingest emits timestamped progress: a start line, the transcript and
    stored-delta summaries, per-phase timing, and the final store line. The Rich
    bar is disabled under a non-tty (capsys), so the summary lines carry detail."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    sid = "sess-ing-log-1"
    _write_jsonl(projects / "-fake" / f"{sid}.jsonl", _main_transcript_events(sid))
    assert store.cmd_ingest(["ingest"]) == 0
    out = capsys.readouterr().out
    assert "ingest start:" in out
    assert "transcripts:" in out
    assert "stored:" in out
    assert "raw_events" in out
    assert "store:" in out


def test_cmd_ingest_verbose_logs_per_transcript(tmp_path, monkeypatch, capsys):
    """-v adds one line per changed transcript (bar off under a non-tty)."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    sid = "sess-ing-log-2"
    _write_jsonl(projects / "-fake" / f"{sid}.jsonl", _main_transcript_events(sid))
    assert store.cmd_ingest(["ingest", "-v"]) == 0
    out = capsys.readouterr().out
    assert "transcript" in out
    assert sid in out


class _PandasSearchCounter:
    """A meta-path finder that only counts `pandas` lookups. A None entry in
    sys.modules stops an import before any finder runs, so a count of 0 means
    no import searched sys.path for pandas."""

    def __init__(self):
        self.hits = 0

    def find_spec(self, name, path=None, target=None):
        if name.partition(".")[0] == "pandas":
            self.hits += 1
        return None


def _entry_at_connect(monkeypatch, real=True):
    """Wrap connect_writer to record the sys.modules['pandas'] entry at the
    moment cmd_ingest opens the store (that is where duckdb gets imported).
    `real=False` stands in for a reader holding the store: no DuckDB runs."""
    seen = []
    real_writer = store.connect_writer

    def writer(*a, **k):
        seen.append(sys.modules.get("pandas", "absent"))
        return real_writer(*a, **k) if real else None

    monkeypatch.setattr(store, "connect_writer", writer)
    return seen


def test_cmd_ingest_blocks_a_missing_pandas_before_duckdb_probes_it(tmp_path, monkeypatch):
    """Without pandas, cmd_ingest sets sys.modules['pandas'] = None before it
    opens the store, so DuckDB's per-query `import pandas` never searches
    sys.path, and the ingest still round-trips."""
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: (
        None if name == "pandas" else real_find_spec(name, *a)))
    counter = _PandasSearchCounter()
    monkeypatch.setattr(sys, "meta_path", [counter, *sys.meta_path])
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    sid = "sess-pandas-01"
    _write_jsonl(projects / "-fake" / f"{sid}.jsonl", _main_transcript_events(sid))
    seen = _entry_at_connect(monkeypatch)

    assert store.cmd_ingest(["ingest"]) == 0
    assert seen == [None]               # in place before connect() imports duckdb
    assert counter.hits == 0            # every probe failed without a search
    with pytest.raises(ImportError):
        importlib.import_module("pandas")
    con = store.connect(read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM sessions WHERE session_id = ?",
                           [sid]).fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM events").fetchone()[0] > 0
    finally:
        con.close()


def test_cmd_ingest_leaves_an_importable_pandas_alone(tmp_path, monkeypatch):
    """With a pandas on sys.path, nothing is blocked. The store open is stubbed
    out (a reader holds it), because DuckDB keeps a pandas it imported for the
    rest of the process, and a fake one would leak into every later test."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "pandas.py").write_text('"""fake pandas: importable, never imported"""\n')
    monkeypatch.syspath_prepend(str(site))
    seen = _entry_at_connect(monkeypatch, real=False)

    assert store.cmd_ingest(["ingest"]) == 0
    assert seen == ["absent"]
    assert "pandas" not in sys.modules
    assert importlib.util.find_spec("pandas").origin == str(site / "pandas.py")


def test_cmd_reset_db_deletes_with_yes(tmp_path, monkeypatch, capsys):
    """reset-db --yes deletes the store file and reports it (return 0)."""
    _build_cli_store(tmp_path, monkeypatch)
    db = tmp_path / "trail.duckdb"
    assert db.exists()
    assert store.cmd_reset_db(["reset-db", "--yes"]) == 0
    assert not db.exists()
    assert "deleted" in capsys.readouterr().out


def test_cmd_reset_db_no_store_returns_zero(tmp_path, monkeypatch, capsys):
    """reset-db on an absent store is a no-op that returns 0."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "absent.duckdb")
    assert store.cmd_reset_db(["reset-db"]) == 0
    assert "nothing to reset" in capsys.readouterr().out


def test_cmd_reset_db_declined_keeps_file(tmp_path, monkeypatch, capsys):
    """Without --yes, a 'n' at the prompt aborts (return 1) and keeps the file."""
    _build_cli_store(tmp_path, monkeypatch)
    db = tmp_path / "trail.duckdb"
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    assert store.cmd_reset_db(["reset-db"]) == 1
    assert db.exists()
    assert "aborted" in capsys.readouterr().out


# cat/diff are no longer stubs: their real reconstruction behavior is exercised
# in the "Reconstruction CLI: cat / diff (Task 6)" section at the end of this file.


# --- File versions + content-addressed blobs (Task 5) -------------------------
#
# Fixtures below mirror the real `toolUseResult` shapes confirmed in Step 5.1
# (see the FILE VERSIONS comment block in claude_trail_store.py): a Write result
# carries the full post `content` and a `type` of "create"/"update"; an Edit
# result carries no `content` and reconstructs post by applying
# oldString->newString to `originalFile`. Both carry a top-level `filePath` and a
# `structuredPatch`, which is the detector the extractor keys on. Tests that need
# a DB monkeypatch STORE_PATH to a temp file; the pure ones do not.


def _edit_result_event(sid="sess-fv-0001", uuid="fv-edit-1", path="/tmp/demo.py"):
    """A user tool_result for an Edit: originalFile (pre) + oldString/newString +
    a real structuredPatch hunk, but NO `content` (post is reconstructed)."""
    pre = "line one\nline two\nline three\n"
    old = "line two\n"
    new = "line two CHANGED\nline two-b\n"
    return {
        "type": "user", "uuid": uuid, "sessionId": sid,
        "timestamp": "2026-06-30T04:00:01.000Z",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu-edit-1",
             "content": f"The file {path} has been updated."}]},
        "toolUseResult": {
            "filePath": path,
            "oldString": old,
            "newString": new,
            "replaceAll": False,
            "originalFile": pre,
            "structuredPatch": [
                {"oldStart": 2, "oldLines": 1, "newStart": 2, "newLines": 2,
                 "lines": ["-line two", "+line two CHANGED", "+line two-b"]}],
            "userModified": False,
        },
    }


def _write_create_event(sid="sess-fv-0001", uuid="fv-write-1",
                        path="/tmp/new.py", content="print('hello')\n"):
    """A user tool_result for a Write of a new file: type "create", full post
    `content`, originalFile null, structuredPatch []."""
    return {
        "type": "user", "uuid": uuid, "sessionId": sid,
        "timestamp": "2026-06-30T04:00:00.000Z",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu-write-1",
             "content": f"File created successfully at: {path}"}]},
        "toolUseResult": {
            "type": "create", "filePath": path, "content": content,
            "structuredPatch": [], "originalFile": None, "userModified": False,
        },
    }


def _blob_shas(con):
    """Every sha in the blob file, sorted, through the writer's own connection."""
    return store._blob_db(con).execute("SELECT sha FROM blobs ORDER BY 1").fetchall()


def test_put_blob_dedups_identical_content(tmp_path, monkeypatch):
    """Two identical contents content-address to one blob row (sha dedup); None
    stores nothing; a distinct content adds exactly one more."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    con = store.connect()
    try:
        a = store._put_blob(con, "same bytes")
        b = store._put_blob(con, "same bytes")
        assert a == b
        assert store.blob_count(con) == 1
        assert store._put_blob(con, None) is None      # None -> no row, no sha
        assert store.blob_count(con) == 1
        store._put_blob(con, "other bytes")
        assert store.blob_count(con) == 2
    finally:
        con.close()


def test_render_patch_difflib_fallback_when_no_structured_patch():
    """_render_patch prefers structuredPatch (as JSON); with none present it
    falls back to a difflib unified diff; with no post it returns None. Pure."""
    pre, post = "a\nb\nc\n", "a\nB\nc\n"
    patch = store._render_patch(None, pre, post, "/x/y.txt")
    assert patch is not None
    assert "--- /x/y.txt" in patch and "+++ /x/y.txt" in patch
    assert "-b" in patch and "+B" in patch
    assert store._render_patch([], pre, post, "/x/y.txt") == patch  # empty list == absent
    sp = [{"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1,
           "lines": ["-b", "+B"]}]
    assert json.loads(store._render_patch(sp, pre, post, "/x/y.txt")) == sp
    # no diff possible (create has pre=None; a missing post) -> None
    assert store._render_patch(None, None, "content", "/x") is None
    assert store._render_patch(None, "content", None, "/x") is None


def test_file_version_from_event_returns_none_for_non_edits(tmp_path, monkeypatch):
    """A Bash toolUseResult (no filePath/structuredPatch) and malformed events
    yield None and write no file_versions."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    con = store.connect()
    try:
        assert store.file_version_from_event(con, _user_tool_result()) is None  # Bash result
        assert store.file_version_from_event(con, {"type": "user"}) is None      # no toolUseResult
        assert store.file_version_from_event(con, "not a dict") is None
        assert con.execute("SELECT count(*) FROM file_versions").fetchone()[0] == 0
    finally:
        con.close()


def test_file_version_from_event_edit_row_and_blob_roundtrip(tmp_path, monkeypatch):
    """A synthetic Edit event yields a file_versions row with the right path and a
    non-null post_sha; the post blob round-trips to the reconstructed content and
    the patch comes from the structuredPatch (not difflib)."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    ev = _edit_result_event(path="/tmp/demo.py")
    tur = ev["toolUseResult"]
    expected_post = tur["originalFile"].replace(tur["oldString"], tur["newString"], 1)
    con = store.connect()
    try:
        fv = store.file_version_from_event(con, ev)
        assert fv is not None
        assert fv["path"] == "/tmp/demo.py"
        assert fv["pre_sha"] is not None and fv["post_sha"] is not None
        # the reconstructed post is stored and reads back byte-for-byte
        post = store.blob_texts(con, [fv["post_sha"]])[fv["post_sha"]]
        assert post == expected_post
        pre = store.blob_texts(con, [fv["pre_sha"]])[fv["pre_sha"]]
        assert pre == tur["originalFile"]
        # patch is the JSON-encoded structuredPatch, not a difflib diff
        assert json.loads(fv["patch"])[0]["newStart"] == 2
    finally:
        con.close()


def test_ingest_all_captures_file_versions_and_blobs(tmp_path, monkeypatch):
    """End-to-end: a transcript with a Write(create) and an Edit(update) yields
    non-zero file_versions and blobs after ingest_all, the edit path round-trips,
    and re-ingest is idempotent (offset-tracked, no duplicate file_versions)."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")

    sid = "sess-fv-e2e-0001"
    path = "/tmp/demo_e2e.py"
    events = [
        _write_create_event(sid, "fv-w-1", path, "v1\n"),
        _edit_result_event(sid, "fv-e-1", path),
        _user_prompt("u-fv-prompt", sid, "carry on", "2026-06-30T04:00:02.000Z"),
    ]
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", events)

    con = store.connect()
    try:
        def count(sql):
            return con.execute(sql).fetchone()[0]

        store.ingest_all(con)
        assert count("SELECT count(*) FROM file_versions") == 2      # write + edit
        assert store.blob_count(con) > 0               # real bytes stored
        # the edit path is present with a non-null post_sha that round-trips
        post_sha = con.execute(
            "SELECT post_sha FROM file_versions WHERE path=? AND pre_sha IS NOT NULL",
            [path]).fetchone()[0]
        assert post_sha is not None
        content = store.blob_texts(con, [post_sha])[post_sha]
        assert content == "line one\nline two CHANGED\nline two-b\nline three\n"

        # idempotent re-ingest: offset tracking means no line is reprocessed, so
        # the append-only file_versions table does not grow
        store.ingest_all(con)
        assert count("SELECT count(*) FROM file_versions") == 2
    finally:
        con.close()


def _edit_result_null_original_event(sid="sess-fv-0001", uuid="fv-edit-null-1",
                                     path="/tmp/nopre.py"):
    """The dominant real Edit shape: originalFile is null (77% of real Edits), so
    there is no PRE to reconstruct POST from. Only the path + structuredPatch (the
    diff) are recoverable; both content shas end up null."""
    return {
        "type": "user", "uuid": uuid, "sessionId": sid,
        "timestamp": "2026-06-30T04:00:03.000Z",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu-edit-null-1",
             "content": f"The file {path} has been updated."}]},
        "toolUseResult": {
            "filePath": path,
            "oldString": "line two\n",
            "newString": "line two CHANGED\n",
            "replaceAll": False,
            "originalFile": None,           # the majority shape: no PRE content
            "structuredPatch": [
                {"oldStart": 2, "oldLines": 1, "newStart": 2, "newLines": 1,
                 "lines": ["-line two", "+line two CHANGED"]}],
            "userModified": False,
        },
    }


def test_file_version_from_event_edit_null_original_file(tmp_path, monkeypatch):
    """An Edit with originalFile null (the dominant real shape) does not crash and
    still emits a row: correct path, a non-null patch from structuredPatch, but
    null pre_sha/post_sha (no PRE to content-address, nothing to reconstruct)."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    ev = _edit_result_null_original_event(path="/tmp/nopre.py")
    con = store.connect()
    try:
        fv = store.file_version_from_event(con, ev)
        assert fv is not None
        assert fv["path"] == "/tmp/nopre.py"
        assert fv["pre_sha"] is None and fv["post_sha"] is None
        assert fv["patch"] is not None                       # structuredPatch kept
        assert json.loads(fv["patch"])[0]["newStart"] == 2
        # nothing was content-addressed (no pre, no reconstructable post)
        assert store.blob_count(con) == 0
    finally:
        con.close()


def test_post_content_replace_all_vs_first():
    """_post_content honours replaceAll: True rewrites EVERY occurrence, the
    default (absent) and explicit False rewrite only the FIRST. This all-vs-first
    branch is the one Edit reconstruction subtlety. Pure, no DB."""
    pre = "x = FOO\ny = FOO\nz = FOO\n"
    tur_all = {"oldString": "FOO", "newString": "BAR", "replaceAll": True}
    assert store._post_content(tur_all, pre) == "x = BAR\ny = BAR\nz = BAR\n"
    tur_default = {"oldString": "FOO", "newString": "BAR"}  # replaceAll absent
    assert store._post_content(tur_default, pre) == "x = BAR\ny = FOO\nz = FOO\n"
    tur_false = {"oldString": "FOO", "newString": "BAR", "replaceAll": False}
    assert store._post_content(tur_false, pre) == "x = BAR\ny = FOO\nz = FOO\n"


def test_ingest_truncation_rescan_no_file_version_duplication(tmp_path, monkeypatch):
    """A truncated/rewritten transcript rescans from offset 0 (ingest_transcript:
    `if size < last: last = 0`), reprocessing every line. The event_id dedup in
    _insert_file_versions (anti-join, no unique constraint to ON CONFLICT on) must
    keep file_versions from growing on that rescan; without it every row would be
    duplicated. Hermetic: monkeypatched PROJECTS_DIR/STORE_PATH."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")

    sid = "sess-fv-rescan-0001"
    path = "/tmp/demo_rescan.py"
    events = [
        _write_create_event(sid, "fv-w-1", path, "v1\n"),
        _edit_result_event(sid, "fv-e-1", path),
    ]
    transcript = projects / "-fake-proj" / f"{sid}.jsonl"
    _write_jsonl(transcript, events)

    con = store.connect()
    try:
        def count(sql):
            return con.execute(sql).fetchone()[0]

        store.ingest_all(con)
        before = count("SELECT count(*) FROM file_versions")
        assert before == 2                                   # write + edit

        # Force the truncation-rescan branch: push the recorded offset past the
        # file size so `size < last` fires and the whole file is reprocessed.
        con.execute("UPDATE ingest_state SET last_offset = last_offset + 100000 "
                    "WHERE transcript_path = ?", [str(transcript)])
        assert store.ingest_all(con) == len(events)          # every line rescanned

        assert count("SELECT count(*) FROM file_versions") == before  # no dup
    finally:
        con.close()


# --- Reconstruction CLI: cat / diff (Task 6) ----------------------------------
#
# Hermetic: a synthetic store is built by ingest_all from a transcript holding
# Write(create)/Edit file_versions under a fake ~/.claude/projects layout in
# tmp_path. PROJECTS_DIR and STORE_PATH are monkeypatched so cmd_cat/cmd_diff,
# which open their OWN read-only connection, hit that store and never the live
# corpus or the real DB. The writer is closed before the commands run so the file
# is unlocked for the read-only reopen. stdout is captured via capsys. The Edit
# fixture (_edit_result_event) reconstructs post by applying oldString->newString
# to originalFile, so both the Write and the Edit have a non-null post_sha.

CATDIFF_PATH = "/tmp/catdiff_demo.py"
CATDIFF_WRITE_CONTENT = "print('hello')\n"                       # Write(create) post
CATDIFF_EDIT_POST = "line one\nline two CHANGED\nline two-b\nline three\n"  # Edit post


def _build_reconstruction_store(tmp_path, monkeypatch, sid, events):
    """Ingest `events` into a hermetic store and leave PROJECTS_DIR/STORE_PATH
    monkeypatched so cmd_cat/cmd_diff (own read-only connection) read it."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", events)
    con = store.connect()
    try:
        store.ingest_all(con)
    finally:
        con.close()  # unlock the file for the cmd_* read-only reopen


def _write_then_edit_events(sid):
    """A Write(create) of a path at 04:00:00 followed by an Edit of the SAME path
    at 04:00:01. Both yield a file_version with a non-null post_sha (the Write
    carries `content`; the Edit reconstructs post from originalFile), so the path
    has two reconstructable versions in chronological order."""
    return [
        _write_create_event(sid, "cd-w-1", CATDIFF_PATH, CATDIFF_WRITE_CONTENT),
        _edit_result_event(sid, "cd-e-1", CATDIFF_PATH),
    ]


def test_cmd_cat_prints_latest_reconstructed_content(tmp_path, monkeypatch, capsys):
    """cat <path> prints the LATEST reconstructed bytes: with a Write(create)
    then an Edit of the same path, the post-Edit content is returned."""
    sid = "sess-cat-0001"
    _build_reconstruction_store(tmp_path, monkeypatch, sid, _write_then_edit_events(sid))
    assert store.cmd_cat(["cat", CATDIFF_PATH]) == 0
    assert capsys.readouterr().out == CATDIFF_EDIT_POST


def test_cmd_cat_at_returns_earlier_content(tmp_path, monkeypatch, capsys):
    """cat --at <ts between the two versions> returns the EARLIER content: the
    `fv.ts <= ?` cutoff excludes the later Edit, leaving the Write's bytes."""
    sid = "sess-cat-0002"
    _build_reconstruction_store(tmp_path, monkeypatch, sid, _write_then_edit_events(sid))
    assert store.cmd_cat(["cat", CATDIFF_PATH, "--at", "2026-06-30T04:00:00.500Z"]) == 0
    assert capsys.readouterr().out == CATDIFF_WRITE_CONTENT


def test_cmd_cat_unknown_path_returns_1(tmp_path, monkeypatch, capsys):
    """cat on a path with no stored version prints the sentinel and returns 1."""
    sid = "sess-cat-0003"
    _build_reconstruction_store(tmp_path, monkeypatch, sid, _write_then_edit_events(sid))
    assert store.cmd_cat(["cat", "/tmp/no-such-file.py"]) == 1
    assert "no stored version for that path/time" in capsys.readouterr().out


def test_cmd_diff_lists_both_versions_oldest_first(tmp_path, monkeypatch, capsys):
    """diff <path> prints every stored version oldest-first, each with its patch:
    the Write(create) block (no diff -> "(no patch)") precedes the Edit block (its
    structuredPatch), and both carry the 8-char session prefix header."""
    sid = "sess-diff-0001"
    _build_reconstruction_store(tmp_path, monkeypatch, sid, _write_then_edit_events(sid))
    assert store.cmd_diff(["diff", CATDIFF_PATH]) == 0
    out = capsys.readouterr().out
    assert out.count(sid[:8]) == 2                       # one header per version
    assert "line two CHANGED" in out                     # the Edit's structuredPatch
    # oldest-first: the create (04:00:00, no patch) prints before the edit (04:00:01)
    assert out.index("(no patch)") < out.index("line two CHANGED")


def test_cmd_cat_patch_only_returns_1_but_diff_shows_patch(tmp_path, monkeypatch, capsys):
    """A path whose only version is patch-only (Edit with originalFile null ->
    null post_sha): cat has nothing to reconstruct (returns 1) while diff still
    lists the version with its structuredPatch."""
    sid = "sess-patchonly-0001"
    path = "/tmp/patchonly_demo.py"
    _build_reconstruction_store(
        tmp_path, monkeypatch, sid,
        [_edit_result_null_original_event(sid, "po-e-1", path)])

    assert store.cmd_cat(["cat", path]) == 1
    assert "no stored version for that path/time" in capsys.readouterr().out

    assert store.cmd_diff(["diff", path]) == 0
    out = capsys.readouterr().out
    assert sid[:8] in out                                # the version is listed
    assert "line two CHANGED" in out                     # its structuredPatch shown
    assert "(no patch)" not in out                       # it HAS a patch


# --- cat roll-forward: patch-only tail applied on top of newest bytes ----------
#
# file-history snapshots checkpoint the PRE-turn state, so a file last edited in
# a session's final turn had no snapshot of its final bytes and `cat` was one
# turn stale. cmd_cat now rolls trailing patch-only edits forward with
# _apply_structured_patch, strictly: a mismatching patch degrades to the old
# stale-but-consistent output plus a stderr warning, never a corrupted mix.


def test_apply_structured_patch_clean_context_and_multi_hunk():
    """Strict application: context lines verified, deletions consumed, additions
    inserted, and the untouched tail (incl. the trailing-newline '' element)
    re-joined verbatim."""
    base = "one\ntwo\nthree\nfour\n"
    single = json.dumps([{"oldStart": 2, "oldLines": 1, "newStart": 2, "newLines": 1,
                          "lines": ["-two", "+TWO"]}])
    assert store._apply_structured_patch(base, single) == "one\nTWO\nthree\nfour\n"
    multi = json.dumps([
        {"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1,
         "lines": ["-one", "+ONE"]},
        {"oldStart": 4, "oldLines": 1, "newStart": 4, "newLines": 2,
         "lines": [" four", "+five"]},
    ])
    assert store._apply_structured_patch(base, multi) == "ONE\ntwo\nthree\nfour\nfive\n"


def test_apply_structured_patch_insertion_only_hunk():
    """oldLines == 0 inserts AFTER old line oldStart (the unified-diff
    zero-length-range convention), touching nothing else."""
    base = "a\nb\n"
    ins = json.dumps([{"oldStart": 1, "oldLines": 0, "newStart": 2, "newLines": 1,
                       "lines": ["+a2"]}])
    assert store._apply_structured_patch(base, ins) == "a\na2\nb\n"


def test_apply_structured_patch_refuses_mismatch_and_garbage():
    """Any context/deletion mismatch, out-of-range hunk, non-JSON payload (the
    difflib fallback text), or non-hunk shape returns None: never half-applied."""
    base = "one\ntwo\n"
    wrong_ctx = json.dumps([{"oldStart": 1, "oldLines": 1, "newStart": 1,
                             "newLines": 1, "lines": ["-NOT THERE", "+x"]}])
    assert store._apply_structured_patch(base, wrong_ctx) is None
    assert store._apply_structured_patch(
        base, "--- a\n+++ b\n@@ -1 +1 @@\n-one\n+x\n") is None
    past_eof = json.dumps([{"oldStart": 99, "oldLines": 1, "newStart": 1,
                            "newLines": 1, "lines": ["-one", "+x"]}])
    assert store._apply_structured_patch(base, past_eof) is None
    assert store._apply_structured_patch(base, json.dumps({"not": "a list"})) is None


def _patch_only_edit_event(sid, uuid, path, ts, hunks):
    """A user Edit tool_result with originalFile null and no oldString/newString:
    post is NOT reconstructable (null post_sha, patch-only row) but the
    structuredPatch is stored -- the shape of the ~61% of transcript edits that
    carry no full content."""
    return {
        "type": "user", "uuid": uuid, "sessionId": sid, "timestamp": ts,
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"toolu-{uuid}",
             "content": f"The file {path} has been updated."}]},
        "toolUseResult": {
            "filePath": path, "originalFile": None,
            "structuredPatch": hunks, "userModified": False,
        },
    }


def test_cmd_cat_rolls_patch_only_tail_forward(tmp_path, monkeypatch, capsys):
    """A Write(create) followed by TWO patch-only Edits: cat applies both patches
    on top of the Write's bytes oldest-first (the second patch's context only
    matches the FIRST patch's output, proving the chain order), no warning."""
    sid = "sess-cat-tail-01"
    events = [
        _write_create_event(sid, "ct-w", CATDIFF_PATH, "alpha\nbeta\n"),
        _patch_only_edit_event(
            sid, "ct-p1", CATDIFF_PATH, "2026-06-30T04:00:02.000Z",
            [{"oldStart": 2, "oldLines": 1, "newStart": 2, "newLines": 1,
              "lines": ["-beta", "+BETA"]}]),
        _patch_only_edit_event(
            sid, "ct-p2", CATDIFF_PATH, "2026-06-30T04:00:03.000Z",
            [{"oldStart": 1, "oldLines": 2, "newStart": 1, "newLines": 3,
              "lines": [" alpha", " BETA", "+gamma"]}]),
    ]
    _build_reconstruction_store(tmp_path, monkeypatch, sid, events)
    assert store.cmd_cat(["cat", CATDIFF_PATH]) == 0
    captured = capsys.readouterr()
    assert captured.out == "alpha\nBETA\ngamma\n"
    assert captured.err == ""


def test_cmd_cat_tail_mismatch_falls_back_stale_with_warning(tmp_path, monkeypatch, capsys):
    """A trailing patch whose context does not match (file changed outside
    Claude) stops the roll-forward: the base bytes print unchanged, a staleness
    warning lands on stderr, and the exit code stays 0."""
    sid = "sess-cat-tail-02"
    events = [
        _write_create_event(sid, "cm-w", CATDIFF_PATH, "alpha\nbeta\n"),
        _patch_only_edit_event(
            sid, "cm-p1", CATDIFF_PATH, "2026-06-30T04:00:02.000Z",
            [{"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1,
              "lines": ["-SOMETHING ELSE", "+x"]}]),
    ]
    _build_reconstruction_store(tmp_path, monkeypatch, sid, events)
    assert store.cmd_cat(["cat", CATDIFF_PATH]) == 0
    captured = capsys.readouterr()
    assert captured.out == "alpha\nbeta\n"               # stale but consistent
    assert "stale by 1 edit" in captured.err


def test_cmd_cat_at_excludes_later_patch_tail(tmp_path, monkeypatch, capsys):
    """--at between the Write and a later patch-only Edit rolls nothing forward:
    the cutoff windows the tail exactly like the base row."""
    sid = "sess-cat-tail-03"
    events = [
        _write_create_event(sid, "cw-w", CATDIFF_PATH, "alpha\nbeta\n"),
        _patch_only_edit_event(
            sid, "cw-p1", CATDIFF_PATH, "2026-06-30T04:00:02.000Z",
            [{"oldStart": 2, "oldLines": 1, "newStart": 2, "newLines": 1,
              "lines": ["-beta", "+BETA"]}]),
    ]
    _build_reconstruction_store(tmp_path, monkeypatch, sid, events)
    assert store.cmd_cat(["cat", CATDIFF_PATH, "--at", "2026-06-30T04:00:01.000Z"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "alpha\nbeta\n"
    assert captured.err == ""


def test_cmd_diff_without_pytz(tmp_path, monkeypatch, capsys):
    """diff must not require the optional `pytz` package.

    DuckDB's Python client needs `pytz` to materialize a raw TIMESTAMPTZ into a
    datetime; diff dodges that by strftime-ing ts to a string for its header.
    Simulate a clean install without pytz by mapping the module to None (so any
    `import pytz` raises ModuleNotFoundError), then run diff over a path with a
    stored edit: it must return 0 and print without raising. Regression for the
    raw-ts footgun that only passed while the dev env had pytz (mirrors the
    per-session tokens guard test)."""
    monkeypatch.setitem(sys.modules, "pytz", None)  # any `import pytz` now raises
    sid = "sess-diff-nopytz-0001"
    _build_reconstruction_store(tmp_path, monkeypatch, sid, _write_then_edit_events(sid))
    assert store.cmd_diff(["diff", CATDIFF_PATH]) == 0
    out = capsys.readouterr().out
    assert sid[:8] in out                                # header rendered
    assert "line two CHANGED" in out                     # the Edit's structuredPatch


# --- File-history backfill (Task 8) -------------------------------------------
#
# Hermetic: PROJECTS_DIR, FILE_HISTORY_DIR, and STORE_PATH are all monkeypatched to
# tmp_path (the autouse _isolate_file_history fixture already stubs FILE_HISTORY_DIR;
# these tests override it with a populated dir). Fixtures mirror the real shapes
# confirmed in Step 8.1: a file-history-snapshot transcript line carries NO top-level
# sessionId and NO top-level timestamp (the session_id comes from the transcript
# filename, the timestamp from trackedFileBackups.backupTime), and the on-disk file
# is <pathhash>@vN where <pathhash> = sha256(abs_path)[:16].


def _pathhash(p):
    return store._sha(p)[:16]


def _fh_snapshot_event(message_id, backups, ts="2026-07-01T10:00:00.000Z"):
    """A file-history-snapshot transcript line as they REALLY appear (Step 8.1): no
    top-level sessionId and no top-level timestamp. `backups` maps a file key ->
    {backupFileName '<pathhash>@vN' | None, version, backupTime}."""
    return {"type": "file-history-snapshot", "messageId": message_id,
            "snapshot": {"messageId": message_id, "trackedFileBackups": backups,
                         "timestamp": ts},
            "isSnapshotUpdate": False}


def _read_tool_use(uuid, sid, abspath, ts):
    """An assistant Read tool_use line: snake_case file_path in tool_input, no
    toolUseResult (so it yields NO file_versions row), used to seed the reverse
    path index without an edit."""
    return {"type": "assistant", "uuid": uuid, "requestId": "req-" + uuid,
            "sessionId": sid, "timestamp": ts,
            "message": {"role": "assistant", "model": "claude-opus-4-8", "id": "msg-" + uuid,
                        "content": [{"type": "tool_use", "id": "toolu-" + uuid, "name": "Read",
                                     "input": {"file_path": abspath}}],
                        "usage": {"input_tokens": 1, "output_tokens": 1,
                                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}


def _setup_fh(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "FILE_HISTORY_DIR", tmp_path / "file-history")
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    return projects, tmp_path / "file-history"


def _write_fh_files(fh_dir, sid, files):
    """Write on-disk snapshot files {backupFileName: str-or-bytes} under <sid>/."""
    d = fh_dir / sid
    d.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        p = d / name
        p.write_bytes(content) if isinstance(content, bytes) else p.write_text(content, encoding="utf-8")


def test_file_history_core_lift(tmp_path, monkeypatch, capsys):
    """The point of the phase: a path that is patch-only in transcripts (an Edit
    with originalFile null) becomes cat-able to its snapshot content. After
    ingest_all the two snapshots are blobs, file_history has 2 rows, file_versions
    has source='file-history' rows with post_sha, and cat returns the v2 content
    (previously exited 1 = no stored version)."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    sid = "sess-fh-core-0001"
    path = "/home/naka/proj/core.py"
    ph = _pathhash(path)
    v1, v2 = "line1\nline2\n", "line1\nline2 CHANGED\nline3\n"
    _write_fh_files(fh_dir, sid, {f"{ph}@v1": v1, f"{ph}@v2": v2})
    events = [
        _fh_snapshot_event("m1", {path: {"backupFileName": f"{ph}@v1", "version": 1,
                                         "backupTime": "2026-07-01T10:00:01.000Z"}}),
        _fh_snapshot_event("m2", {path: {"backupFileName": f"{ph}@v2", "version": 2,
                                         "backupTime": "2026-07-01T10:00:02.000Z"}}),
        _edit_result_null_original_event(sid, "fh-core-edit", path),  # patch-only Edit, same path
    ]
    _write_jsonl(projects / "-proj" / f"{sid}.jsonl", events)

    con = store.connect()
    try:
        store.ingest_all(con)
        def count(sql):
            return con.execute(sql).fetchone()[0]
        assert count("SELECT count(*) FROM file_history") == 2
        assert store.blob_count(con) == 2                 # v1 + v2 preserved
        assert count("SELECT count(*) FROM file_versions "
                     "WHERE source='file-history' AND post_sha IS NOT NULL") == 2
        # the transcript Edit is still there, patch-only (null post_sha), source stamped
        assert con.execute("SELECT count(*) FROM file_versions WHERE source='transcript' "
                           "AND post_sha IS NULL AND path=?", [path]).fetchone()[0] == 1
    finally:
        con.close()

    capsys.readouterr()                                                # drop the ingest progress line
    assert store.cmd_cat(["cat", path]) == 0
    assert capsys.readouterr().out == v2                               # newest cat-able version


def test_file_history_b1_pre_sha_and_gap_chaining(tmp_path, monkeypatch):
    """B1: a group whose first present version is @v2 gets pre_sha NULL / no patch;
    a @v2 + @v4 group (interior gap) chains v4.pre_sha to v2's blob with a difflib
    patch. Never assumes v-1 exists."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    sid = "sess-fh-b1-0001"
    pa, pb = "/home/naka/proj/only_v2.py", "/home/naka/proj/gap.py"
    pha, phb = _pathhash(pa), _pathhash(pb)
    a2, b2, b4 = "only v2\n", "b line\n", "b line\nb line 4\n"
    _write_fh_files(fh_dir, sid, {f"{pha}@v2": a2, f"{phb}@v2": b2, f"{phb}@v4": b4})
    events = [
        _fh_snapshot_event("mA", {pa: {"backupFileName": f"{pha}@v2", "version": 2,
                                       "backupTime": "2026-07-01T11:00:02.000Z"}}),
        _fh_snapshot_event("mB2", {pb: {"backupFileName": f"{phb}@v2", "version": 2,
                                        "backupTime": "2026-07-01T11:00:02.000Z"}}),
        _fh_snapshot_event("mB4", {pb: {"backupFileName": f"{phb}@v4", "version": 4,
                                        "backupTime": "2026-07-01T11:00:04.000Z"}}),
    ]
    _write_jsonl(projects / "-proj" / f"{sid}.jsonl", events)
    con = store.connect()
    try:
        store.ingest_all(con)
        a = con.execute("SELECT pre_sha, patch FROM file_versions "
                        "WHERE source='file-history' AND path=?", [pa]).fetchall()
        assert len(a) == 1 and a[0][0] is None and a[0][1] is None      # @v2-only: no predecessor
        b = con.execute("SELECT pre_sha, post_sha, patch FROM file_versions "
                        "WHERE source='file-history' AND path=? ORDER BY ts", [pb]).fetchall()
        assert len(b) == 2
        (v2_pre, v2_post, v2_patch), (v4_pre, v4_post, v4_patch) = b
        assert v2_pre is None and v2_patch is None                      # v2 first present in group
        assert v4_pre == v2_post                                        # B1 across the gap
        assert v4_patch is not None and "b line 4" in v4_patch          # difflib patch present
    finally:
        con.close()


def test_file_history_b2_both_source_values(tmp_path, monkeypatch):
    """B2: after a mixed ingest a transcript row is source='transcript' and a
    file-history row is source='file-history' (the phase's own coverage stat relies
    on the transcript rows NOT landing source=NULL)."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    sid = "sess-fh-b2-0001"
    fh_path, edit_path = "/home/naka/proj/snap.py", "/tmp/b2_edit.py"
    ph = _pathhash(fh_path)
    _write_fh_files(fh_dir, sid, {f"{ph}@v1": "snap v1\n"})
    events = [
        _fh_snapshot_event("mS", {fh_path: {"backupFileName": f"{ph}@v1", "version": 1,
                                            "backupTime": "2026-07-01T12:00:01.000Z"}}),
        _edit_result_event(sid, "b2-edit", edit_path),                  # transcript Edit (real pre/post)
    ]
    _write_jsonl(projects / "-proj" / f"{sid}.jsonl", events)
    con = store.connect()
    try:
        store.ingest_all(con)
        assert con.execute("SELECT source FROM file_versions WHERE path=?",
                           [edit_path]).fetchone()[0] == "transcript"
        assert con.execute("SELECT source FROM file_versions WHERE path=?",
                           [fh_path]).fetchone()[0] == "file-history"
    finally:
        con.close()


def test_file_history_m3_resolves_via_read_tool_input(tmp_path, monkeypatch):
    """M3: a snapshot referenced by a RELATIVE key, whose path appears ONLY in a Read
    tool_use.tool_input (snake_case file_path, no file_versions row), still resolves
    via the pathhash reverse-index."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    sid = "sess-fh-m3-0001"
    abspath = "/home/naka/proj/read_only.py"
    ph = _pathhash(abspath)
    _write_fh_files(fh_dir, sid, {f"{ph}@v1": "readonly v1\n"})
    events = [
        _read_tool_use("m3read", sid, abspath, "2026-07-01T13:00:00.000Z"),
        _fh_snapshot_event("mR", {"read_only.py": {"backupFileName": f"{ph}@v1", "version": 1,
                                                   "backupTime": "2026-07-01T13:00:01.000Z"}}),
    ]
    _write_jsonl(projects / "-proj" / f"{sid}.jsonl", events)
    con = store.connect()
    try:
        store.ingest_all(con)
        assert con.execute("SELECT count(*) FROM file_versions WHERE source='transcript' "
                           "AND path=?", [abspath]).fetchone()[0] == 0     # Read is not an edit
        assert con.execute("SELECT path FROM file_history WHERE pathhash=?",
                           [ph]).fetchone()[0] == abspath                  # resolved anyway
        assert con.execute("SELECT count(*) FROM file_versions WHERE source='file-history' "
                           "AND path=?", [abspath]).fetchone()[0] == 1
    finally:
        con.close()


def test_file_history_deferred_resolution(tmp_path, monkeypatch):
    """Deferred resolution: an unresolved snapshot is still preserved (blob stored,
    file_history.path NULL, no file_versions row); a later sweep that reveals the
    path fills it and emits the file-history file_versions row."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    sid = "sess-fh-defer-0001"
    abspath = "/home/naka/proj/deferred.py"
    ph = _pathhash(abspath)
    _write_fh_files(fh_dir, sid, {f"{ph}@v1": "deferred v1\n"})
    transcript = projects / "-proj" / f"{sid}.jsonl"
    _write_jsonl(transcript, [                                            # relative key, path unknown
        _fh_snapshot_event("mD", {"deferred.py": {"backupFileName": f"{ph}@v1", "version": 1,
                                                  "backupTime": "2026-07-01T14:00:01.000Z"}})])
    con = store.connect()
    try:
        store.ingest_all(con)
        assert store.blob_count(con) == 1              # preserved
        assert con.execute("SELECT path FROM file_history WHERE pathhash=?", [ph]).fetchone()[0] is None
        assert con.execute("SELECT count(*) FROM file_versions "
                           "WHERE source='file-history'").fetchone()[0] == 0

        with open(transcript, "a", encoding="utf-8") as f:               # reveal the path (a Read)
            f.write(json.dumps(_read_tool_use("mDr", sid, abspath, "2026-07-01T14:05:00.000Z")) + "\n")
        store.ingest_all(con)
        assert con.execute("SELECT path FROM file_history WHERE pathhash=?",
                           [ph]).fetchone()[0] == abspath
        assert con.execute("SELECT count(*) FROM file_versions WHERE source='file-history' "
                           "AND path=?", [abspath]).fetchone()[0] == 1
    finally:
        con.close()


def test_file_history_idempotent(tmp_path, monkeypatch):
    """Re-running leaves file_history, file_versions, and blobs counts all unchanged
    (immutable-PK skip + anti-join dedup), including a direct ingest_file_history
    call that does not touch ingest_state."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    sid = "sess-fh-idem-0001"
    path = "/home/naka/proj/idem.py"
    ph = _pathhash(path)
    _write_fh_files(fh_dir, sid, {f"{ph}@v1": "v1\n", f"{ph}@v2": "v1\nv2\n"})
    events = [
        _fh_snapshot_event("mi1", {path: {"backupFileName": f"{ph}@v1", "version": 1,
                                          "backupTime": "2026-07-01T15:00:01.000Z"}}),
        _fh_snapshot_event("mi2", {path: {"backupFileName": f"{ph}@v2", "version": 2,
                                          "backupTime": "2026-07-01T15:00:02.000Z"}}),
    ]
    _write_jsonl(projects / "-proj" / f"{sid}.jsonl", events)
    con = store.connect()
    try:
        store.ingest_all(con)
        def snap():
            return (con.execute("SELECT count(*) FROM file_history").fetchone()[0],
                    con.execute("SELECT count(*) FROM file_versions").fetchone()[0],
                    store.blob_count(con))
        first = snap()
        assert first[0] == 2 and first[2] == 2
        store.ingest_all(con)
        assert snap() == first                                           # full re-sweep: unchanged
        store.ingest_file_history(con)                                   # direct pass: unchanged
        assert snap() == first
    finally:
        con.close()


def test_file_history_schema1_migration(tmp_path, monkeypatch):
    """A schema-1 (0.6.0) DB upgrades in place on reopen: the source column is added
    and backfills existing rows with 'transcript', file_history exists, and the
    stored schema version becomes the current one, '3' (the ON CONFLICT DO UPDATE
    fix; a 0.6.0 store with no DuckDB `blobs` table has nothing to move)."""
    import duckdb
    db = tmp_path / "trail.duckdb"
    monkeypatch.setattr(store, "STORE_PATH", db)
    raw = duckdb.connect(str(db))
    raw.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT)")
    raw.execute("INSERT INTO schema_meta VALUES ('version', '1')")
    raw.execute("CREATE TABLE file_versions(event_id TEXT, session_id TEXT, ts TIMESTAMPTZ, "
                "path TEXT, pre_sha TEXT, post_sha TEXT, patch TEXT)")   # no `source` column
    raw.execute("INSERT INTO file_versions VALUES ('e1','s1', NULL, '/p/a.py', NULL, 'abc', NULL)")
    raw.close()

    con = store.connect()                                               # runs the migration
    try:
        assert con.execute("SELECT source FROM file_versions WHERE event_id='e1'").fetchone()[0] == "transcript"
        assert con.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0] == "3"
        assert con.execute("SELECT count(*) FROM information_schema.tables "
                           "WHERE table_name='file_history'").fetchone()[0] == 1
    finally:
        con.close()


def test_file_history_cross_session_cat_newest_ts(tmp_path, monkeypatch, capsys):
    """The same path snapshotted under two session dirs: cat picks the newest
    backupTime across them."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    path = "/home/naka/proj/shared.py"
    ph = _pathhash(path)
    sid1, sid2 = "sess-fh-x-0001", "sess-fh-x-0002"
    old, new = "old session content\n", "new session content\n"
    _write_fh_files(fh_dir, sid1, {f"{ph}@v1": old})
    _write_fh_files(fh_dir, sid2, {f"{ph}@v1": new})
    _write_jsonl(projects / "-proj" / f"{sid1}.jsonl",
                 [_fh_snapshot_event("mx1", {path: {"backupFileName": f"{ph}@v1", "version": 1,
                                                    "backupTime": "2026-07-01T09:00:00.000Z"}})])
    _write_jsonl(projects / "-proj" / f"{sid2}.jsonl",
                 [_fh_snapshot_event("mx2", {path: {"backupFileName": f"{ph}@v1", "version": 1,
                                                    "backupTime": "2026-07-02T09:00:00.000Z"}})])
    con = store.connect()
    try:
        store.ingest_all(con)
        assert con.execute("SELECT count(*) FROM file_history WHERE pathhash=?",
                           [ph]).fetchone()[0] == 2                      # distinct (session, ...) PKs
    finally:
        con.close()
    capsys.readouterr()
    assert store.cmd_cat(["cat", path]) == 0
    assert capsys.readouterr().out == new                               # newer backupTime wins


def test_file_history_empty_snapshot_and_null_backupfilename(tmp_path, monkeypatch):
    """An empty snapshot is a real blob (sha of "") with a resolved row; a
    trackedFileBackups entry whose backupFileName is null is skipped entirely."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    sid = "sess-fh-empty-0001"
    empty_path, null_path = "/home/naka/proj/empty.py", "/home/naka/proj/not_backed.py"
    phe = _pathhash(empty_path)
    _write_fh_files(fh_dir, sid, {f"{phe}@v1": ""})
    events = [_fh_snapshot_event("me", {
        empty_path: {"backupFileName": f"{phe}@v1", "version": 1, "backupTime": "2026-07-01T16:00:01.000Z"},
        null_path:  {"backupFileName": None, "version": 1, "backupTime": "2026-07-01T16:00:02.000Z"},
    })]
    _write_jsonl(projects / "-proj" / f"{sid}.jsonl", events)
    con = store.connect()
    try:
        store.ingest_all(con)
        empty_sha = store._sha("")
        assert store.blob_texts(con, [empty_sha]) == {empty_sha: ""}
        assert con.execute("SELECT sha FROM file_history WHERE pathhash=?", [phe]).fetchone()[0] == empty_sha
        assert con.execute("SELECT count(*) FROM file_history WHERE pathhash=?",
                           [_pathhash(null_path)]).fetchone()[0] == 0    # null backupFileName skipped
        assert con.execute("SELECT count(*) FROM file_history").fetchone()[0] == 1
    finally:
        con.close()


def test_file_history_binary_snapshot_skipped(tmp_path, monkeypatch):
    """A non-UTF-8 snapshot (0 today) is recorded as a file_history row with sha NULL
    and note='binary-skipped', with no blob and no file_versions row (lossy
    errors='replace' would betray the preservation framing)."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    sid = "sess-fh-bin-0001"
    path = "/home/naka/proj/blob.bin"
    ph = _pathhash(path)
    _write_fh_files(fh_dir, sid, {f"{ph}@v1": b"\xff\xfe\x00\x01binary"})
    events = [_fh_snapshot_event("mbin", {path: {"backupFileName": f"{ph}@v1", "version": 1,
                                                 "backupTime": "2026-07-01T17:00:01.000Z"}})]
    _write_jsonl(projects / "-proj" / f"{sid}.jsonl", events)
    con = store.connect()
    try:
        store.ingest_all(con)
        assert con.execute("SELECT sha, note FROM file_history WHERE pathhash=?",
                           [ph]).fetchone() == (None, "binary-skipped")
        assert store.blob_count(con) == 0
        assert con.execute("SELECT count(*) FROM file_versions "
                           "WHERE source='file-history'").fetchone()[0] == 0
    finally:
        con.close()


def test_file_history_diff_default_hides_all_sources_shows(tmp_path, monkeypatch, capsys):
    """diff hides file-history rows by default; --all-sources shows them, printing
    only rows whose patch is non-null (the headless first snapshot is skipped)."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    sid = "sess-fh-diff-0001"
    path = "/home/naka/proj/diffme.py"
    ph = _pathhash(path)
    v2, v3 = "a\nb\n", "a\nb\nc ADDED\n"
    _write_fh_files(fh_dir, sid, {f"{ph}@v2": v2, f"{ph}@v3": v3})
    events = [
        _fh_snapshot_event("md2", {path: {"backupFileName": f"{ph}@v2", "version": 2,
                                          "backupTime": "2026-07-01T18:00:02.000Z"}}),
        _fh_snapshot_event("md3", {path: {"backupFileName": f"{ph}@v3", "version": 3,
                                          "backupTime": "2026-07-01T18:00:03.000Z"}}),
    ]
    _write_jsonl(projects / "-proj" / f"{sid}.jsonl", events)
    con = store.connect()
    try:
        store.ingest_all(con)
    finally:
        con.close()

    capsys.readouterr()
    assert store.cmd_diff(["diff", path]) == 0                          # default: file-history hidden
    default_out = capsys.readouterr().out
    assert "c ADDED" not in default_out and sid[:8] not in default_out

    assert store.cmd_diff(["diff", path, "--all-sources"]) == 0
    all_out = capsys.readouterr().out
    assert "c ADDED" in all_out                                         # v3's difflib patch shows
    assert all_out.count(sid[:8]) == 1                                  # v2 (null patch) skipped


def test_file_history_bad_backup_time_does_not_poison(tmp_path, monkeypatch):
    """Major 2 regression: a non-string or unparseable trackedFileBackups.backupTime
    must not abort ingest_all. The bad entry lives permanently in raw_events and is
    re-read every sweep, so letting the TIMESTAMPTZ ConversionException fire at the
    bind would wedge every future ingest with no self-heal. Instead the snapshot is
    still preserved (blob + file_history row, timestamp fell back to on-disk mtime)
    and a second sweep completes too."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    sid = "sess-fh-badts-0001"
    p_dict, p_str = "/home/naka/proj/bad_dict.py", "/home/naka/proj/bad_str.py"
    ph_dict, ph_str = _pathhash(p_dict), _pathhash(p_str)
    _write_fh_files(fh_dir, sid, {f"{ph_dict}@v1": "dict ts\n", f"{ph_str}@v1": "str ts\n"})
    # Absolute keys so both paths self-resolve; backupTime is garbage in two shapes:
    # a non-string (dict) and a string that does not parse as ISO.
    events = [_fh_snapshot_event("mbad", {
        p_dict: {"backupFileName": f"{ph_dict}@v1", "version": 1, "backupTime": {"weird": "dict"}},
        p_str:  {"backupFileName": f"{ph_str}@v1", "version": 1, "backupTime": "not-a-timestamp"},
    })]
    _write_jsonl(projects / "-proj" / f"{sid}.jsonl", events)
    con = store.connect()
    try:
        store.ingest_all(con)                                            # must NOT raise
        assert con.execute("SELECT count(*) FROM file_history").fetchone()[0] == 2
        assert store.blob_count(con) == 2   # bytes preserved
        # snapshot_ts fell back to the on-disk mtime (non-null), and both rows emit a
        # file-history file_versions row (the ts flowed through epoch_ms cleanly).
        assert con.execute("SELECT count(*) FROM file_history "
                           "WHERE snapshot_ts IS NOT NULL").fetchone()[0] == 2
        assert con.execute("SELECT count(*) FROM file_versions WHERE source='file-history' "
                           "AND post_sha IS NOT NULL").fetchone()[0] == 2
        store.ingest_all(con)                                            # second sweep: no poison
        assert con.execute("SELECT count(*) FROM file_history").fetchone()[0] == 2
    finally:
        con.close()


def test_file_history_reader_guards_pre_migration(tmp_path, monkeypatch):
    """Minor 3 (the regression that would have caught Major 1): a schema-1 (0.6.0)
    store opened READ-ONLY, before any post-upgrade ingest, must not raise from
    cmd_stats or cmd_diff. file_versions has no `source` column and there is no
    file_history table (readers run no DDL, so neither appears until a writable
    ingest), so both readers must guard their new queries via information_schema; the
    diff crash was a BinderException on the unguarded `source IS DISTINCT FROM` clause."""
    import duckdb
    db = tmp_path / "trail.duckdb"
    monkeypatch.setattr(store, "STORE_PATH", db)
    raw = duckdb.connect(str(db))
    raw.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT)")
    raw.execute("INSERT INTO schema_meta VALUES ('version', '1')")
    raw.execute("CREATE TABLE sessions(session_id TEXT)")            # base tables cmd_stats reads
    raw.execute("CREATE TABLE events(tool_name TEXT)")
    raw.execute("CREATE TABLE blobs(sha TEXT, size INTEGER)")
    raw.execute("CREATE TABLE api_requests(output_tokens INTEGER)")
    raw.execute("CREATE TABLE file_versions(event_id TEXT, session_id TEXT, ts TIMESTAMPTZ, "
                "path TEXT, pre_sha TEXT, post_sha TEXT, patch TEXT)")   # no `source`, no file_history
    raw.execute("INSERT INTO file_versions VALUES ('e1','s1', NULL, '/p/a.py', NULL, 'abc', NULL)")
    raw.close()

    assert store.cmd_stats(["stats"]) == 0                              # information_schema guards hold
    assert store.cmd_diff(["diff", "/p/a.py"]) == 0                     # was BinderException on `source`


def test_file_history_binary_skipped_path_promotes(tmp_path, monkeypatch):
    """Minor 1: a binary-skipped snapshot (sha NULL) whose path is not yet known must
    still have its path backfilled on a later sweep. The promotion trigger counts
    every path-less row, not only those carrying a blob, so a binary row is not stuck
    'unresolved' forever once its path becomes known (it emits no file_versions row
    either way, but stats must stop reporting it unresolved)."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    sid = "sess-fh-binprom-0001"
    abspath = "/home/naka/proj/binprom.bin"
    ph = _pathhash(abspath)
    _write_fh_files(fh_dir, sid, {f"{ph}@v1": b"\xff\xfe\x00bin"})       # non-UTF-8: binary-skipped
    transcript = projects / "-proj" / f"{sid}.jsonl"
    _write_jsonl(transcript, [                                           # relative key: path unknown
        _fh_snapshot_event("mbp", {"binprom.bin": {"backupFileName": f"{ph}@v1", "version": 1,
                                                   "backupTime": "2026-07-01T19:00:01.000Z"}})])
    con = store.connect()
    try:
        store.ingest_all(con)
        assert con.execute("SELECT sha, path FROM file_history WHERE pathhash=?",
                           [ph]).fetchone() == (None, None)             # binary-skipped, path unknown

        with open(transcript, "a", encoding="utf-8") as f:              # reveal the path (a Read)
            f.write(json.dumps(_read_tool_use("mbpr", sid, abspath, "2026-07-01T19:05:00.000Z")) + "\n")
        store.ingest_all(con)
        assert con.execute("SELECT path FROM file_history WHERE pathhash=?",
                           [ph]).fetchone()[0] == abspath               # promoted despite sha NULL
        assert con.execute("SELECT count(*) FROM file_versions "
                           "WHERE source='file-history'").fetchone()[0] == 0   # no emit for a null-sha row
    finally:
        con.close()


def test_file_history_promotes_after_dir_pruned(tmp_path, monkeypatch):
    """Minor 2: once a snapshot's bytes are preserved, deferred path resolution keeps
    working even after ~/.claude/file-history is pruned (the exact loss scenario this
    phase insures against). The disk-reading passes are gated on the dir, but
    promotion + emit run regardless, so a later sweep that learns the path still
    resolves the stored row and emits its file_versions row from the preserved blob."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    sid = "sess-fh-pruned-0001"
    abspath = "/home/naka/proj/pruned.py"
    ph = _pathhash(abspath)
    _write_fh_files(fh_dir, sid, {f"{ph}@v1": "pruned v1\n"})
    transcript = projects / "-proj" / f"{sid}.jsonl"
    _write_jsonl(transcript, [                                          # relative key: path unknown
        _fh_snapshot_event("mpr", {"pruned.py": {"backupFileName": f"{ph}@v1", "version": 1,
                                                 "backupTime": "2026-07-01T20:00:01.000Z"}})])
    con = store.connect()
    try:
        store.ingest_all(con)
        assert store.blob_count(con) == 1             # preserved
        assert con.execute("SELECT path FROM file_history WHERE pathhash=?",
                           [ph]).fetchone()[0] is None

        # Prune file-history (point at a nonexistent dir) and reveal the path.
        monkeypatch.setattr(store, "FILE_HISTORY_DIR", tmp_path / "pruned-away")
        with open(transcript, "a", encoding="utf-8") as f:
            f.write(json.dumps(_read_tool_use("mprr", sid, abspath, "2026-07-01T20:05:00.000Z")) + "\n")
        store.ingest_all(con)
        assert con.execute("SELECT path FROM file_history WHERE pathhash=?",
                           [ph]).fetchone()[0] == abspath              # resolved despite the pruned dir
        assert con.execute("SELECT count(*) FROM file_versions WHERE source='file-history' "
                           "AND path=?", [abspath]).fetchone()[0] == 1  # emitted from the stored blob
    finally:
        con.close()


# --- Incremental file-history pass (ingest CPU plan, phase 3) ------------------
#
# Ordinary `ingest` runs parse only the snapshot/delta lines their own sweep
# stored; the full pass (every stored payload plus the orphan glob) runs on
# --compact, --file-history-only, ingest_all, and in any run once
# `<store>.fh-full-stamp` is FILE_HISTORY_FULL_INTERVAL old (or missing, as on
# a fresh store). These tests pin both halves and that they leave the same rows.


def _full_pass_ran_recently():
    """Record a fresh full pass, so the next ordinary runs stay incremental."""
    store._fh_full_stamp().touch()


def _age_full_pass_stamp():
    """Make the recorded full pass one interval (plus a minute) old."""
    old = time.time() - store.FILE_HISTORY_FULL_INTERVAL - 60
    os.utime(store._fh_full_stamp(), (old, old))


def _fh_delta_event(message_id, tracking_path, bfn, version, btime, real_parent_dir):
    """A file-history-delta transcript line as it really appears: one file per
    line, `trackingPath` absolute or relative to the launch dir, and the same
    meta shape as a trackedFileBackups entry under `backup`. No uuid/sessionId."""
    return {"type": "file-history-delta", "messageId": message_id,
            "snapshotMessageId": "snap-" + message_id, "trackingPath": tracking_path,
            "backup": {"backupFileName": bfn, "version": version, "backupTime": btime,
                       "realParentDir": real_parent_dir},
            "timestamp": btime}


def _fh_rows(con):
    """Everything the file-history pass writes, in a comparable order."""
    return (con.execute("SELECT session_id, pathhash, version, path, sha, "
                        "epoch_ms(snapshot_ts), note FROM file_history "
                        "ORDER BY 1, 2, 3").fetchall(),
            con.execute("SELECT event_id, session_id, epoch_ms(ts), path, pre_sha, "
                        "post_sha, patch FROM file_versions WHERE source='file-history' "
                        "ORDER BY 1").fetchall(),
            _blob_shas(con))


def _forbid(monkeypatch, *names):
    """Make the full-pass helpers fail loudly if an ordinary run reaches them."""
    for name in names:
        def boom(*a, _name=name, **k):
            raise AssertionError(f"{_name} ran on an incremental pass")
        monkeypatch.setattr(store, name, boom)


def test_incremental_pass_stores_and_resolves_a_new_snapshot_in_the_same_run(
        tmp_path, monkeypatch, capsys):
    """One ordinary run stores and resolves new snapshots from the lines it just
    read, through each cheap route (an absolute key, sessions.cwd plus a relative
    key, a delta line's realParentDir) and with no payload scan or reverse index.
    The delta row takes its backupTime, not the file's mtime."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    sid, cwd = "sess-fh-inc-0001", "/home/naka/proj"
    p_abs, p_rel, p_delta = f"{cwd}/abs.py", f"{cwd}/sub/rel.py", f"{cwd}/pkg/delta.py"
    h_abs, h_rel, h_delta = _pathhash(p_abs), _pathhash(p_rel), _pathhash(p_delta)
    _write_fh_files(fh_dir, sid, {f"{h_abs}@v1": "abs\n", f"{h_rel}@v1": "rel\n",
                                  f"{h_delta}@v1": "delta\n"})
    _write_jsonl(projects / "-home-naka-proj" / f"{sid}.jsonl", [
        _fh_snapshot_event("mi1", {
            p_abs: {"backupFileName": f"{h_abs}@v1", "version": 1,
                    "backupTime": "2026-07-02T10:00:01.000Z"},
            "sub/rel.py": {"backupFileName": f"{h_rel}@v1", "version": 1,
                           "backupTime": "2026-07-02T10:00:02.000Z"}}),
        {**_user_prompt("u-inc-1", sid, "edit things", "2026-07-02T10:00:00.000Z"),
         "cwd": cwd},
        _fh_delta_event("mi2", "pkg/delta.py", f"{h_delta}@v1", 1,
                        "2026-07-02T10:00:03.000Z", f"{cwd}/pkg"),
    ])
    _full_pass_ran_recently()
    _forbid(monkeypatch, "_snapshot_records", "_known_paths")

    assert store.cmd_ingest(["ingest"]) == 0
    out = capsys.readouterr().out
    assert "file-history: 3 new snapshots, 3 resolved" in out
    assert "(full pass)" not in out
    con = store.connect(read_only=True)
    try:
        assert dict(con.execute("SELECT pathhash, path FROM file_history").fetchall()) == {
            h_abs: p_abs, h_rel: p_rel, h_delta: p_delta}
        assert con.execute("SELECT count(*) FROM file_versions WHERE source='file-history' "
                           "AND post_sha IS NOT NULL").fetchone()[0] == 3
        assert con.execute("SELECT epoch_ms(snapshot_ts) FROM file_history WHERE pathhash=?",
                           [h_delta]).fetchone()[0] == 1782986403000   # its backupTime
    finally:
        con.close()


def test_idle_ingest_reads_no_snapshot_payload(tmp_path, monkeypatch, capsys):
    """The point of the phase: once a snapshot is stored, an ordinary run that
    finds no new transcript lines reads no snapshot payload, builds no reverse
    index and asks file_history nothing beyond the stored-rows count. The first
    run on a fresh store has no full pass recorded, so it takes one and records
    it; that stamp is what keeps the idle run incremental."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    sid, path = "sess-fh-idle-0001", "/home/naka/proj/idle.py"
    ph = _pathhash(path)
    _write_fh_files(fh_dir, sid, {f"{ph}@v1": "idle\n"})
    _write_jsonl(projects / "-proj" / f"{sid}.jsonl", [
        _fh_snapshot_event("mid", {path: {"backupFileName": f"{ph}@v1", "version": 1,
                                          "backupTime": "2026-07-02T11:00:01.000Z"}})])
    assert not store._fh_full_stamp().exists()
    assert store.cmd_ingest(["ingest"]) == 0                       # stores the snapshot
    assert "(full pass)" in capsys.readouterr().out
    assert store._fh_full_stamp().exists()

    _forbid(monkeypatch, "_snapshot_records", "_known_paths")
    counting = _CountingCon(store.connect())
    monkeypatch.setattr(store, "connect_writer", lambda *a, **k: counting)
    assert store.cmd_ingest(["ingest"]) == 0
    assert "(full pass)" not in capsys.readouterr().out
    fh_sql = [s for s in counting.sql if "file-history" in s or "file_history" in s]
    assert fh_sql and all(s == "SELECT count(*) FROM file_history" for s in fh_sql)


def test_incremental_pass_resolves_from_an_earlier_row_of_the_same_file(
        tmp_path, monkeypatch):
    """A later version whose own line cannot name its path (a relative key in a
    session with no cwd) resolves from an earlier row of the same pathhash, and
    its synthetic file_versions row chains pre_sha over the version gap to the
    blob a previous run stored."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    sid, path = "sess-fh-chain-0001", "/home/naka/proj/chain.py"
    ph = _pathhash(path)
    v2, v4 = "chain v2\n", "chain v2\nchain v4\n"
    _write_fh_files(fh_dir, sid, {f"{ph}@v2": v2, f"{ph}@v4": v4})
    transcript = projects / "-proj" / f"{sid}.jsonl"
    _write_jsonl(transcript, [
        _fh_snapshot_event("mc2", {path: {"backupFileName": f"{ph}@v2", "version": 2,
                                          "backupTime": "2026-07-02T12:00:02.000Z"}})])
    _full_pass_ran_recently()            # else the orphan glob would store v4 already
    assert store.cmd_ingest(["ingest"]) == 0

    _forbid(monkeypatch, "_snapshot_records", "_known_paths")
    with open(transcript, "a", encoding="utf-8") as f:
        f.write(json.dumps(_fh_snapshot_event("mc4", {"chain.py": {
            "backupFileName": f"{ph}@v4", "version": 4,
            "backupTime": "2026-07-02T12:00:04.000Z"}})) + "\n")
    assert store.cmd_ingest(["ingest"]) == 0
    con = store.connect(read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM file_history WHERE path=?",
                           [path]).fetchone()[0] == 2
        (pre2, post2, _), (pre4, post4, patch4) = con.execute(
            "SELECT pre_sha, post_sha, patch FROM file_versions "
            "WHERE source='file-history' ORDER BY ts").fetchall()
        assert pre2 is None and pre4 == post2 == store._sha(v2)
        assert post4 == store._sha(v4) and "chain v4" in patch4
    finally:
        con.close()


@pytest.mark.parametrize("flag", ["--compact", "--file-history-only"])
def test_full_pass_picks_up_an_orphan_and_a_crashed_runs_lines(
        tmp_path, monkeypatch, capsys, flag):
    """The full pass is the safety net. A snapshot and a delta line stored by a
    run that died before its parse (simulated: swept without collecting) are
    never seen by an ordinary run again, since their offset has advanced, and an
    on-disk orphan has no line at all. --compact and --file-history-only pick up
    all three; the delta row still gets its backupTime, because its payload is
    kept for exactly this."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    sid, orphan_sid = "sess-fh-crash-0001", "sess-fh-orphan-0001"
    p_snap, p_delta = "/home/naka/proj/crash.py", "/home/naka/proj/crash_delta.py"
    h_snap, h_delta, h_orphan = (_pathhash(p_snap), _pathhash(p_delta),
                                 _pathhash("/home/naka/proj/orphan.py"))
    _write_fh_files(fh_dir, sid, {f"{h_snap}@v1": "crash\n", f"{h_delta}@v1": "delta\n"})
    _write_fh_files(fh_dir, orphan_sid, {f"{h_orphan}@v1": "orphan\n"})
    _write_jsonl(projects / "-proj" / f"{sid}.jsonl", [
        _fh_snapshot_event("mcr", {p_snap: {"backupFileName": f"{h_snap}@v1", "version": 1,
                                            "backupTime": "2026-07-02T13:00:01.000Z"}}),
        _fh_delta_event("mcd", p_delta, f"{h_delta}@v1", 1,
                        "2026-07-02T13:00:02.000Z", "/home/naka/proj")])
    con = store.connect()
    try:
        store._sweep_transcripts(con)                 # the crashed run: lines in, no parse
    finally:
        con.close()

    _full_pass_ran_recently()                         # --compact / --file-history-only force it
    assert store.cmd_ingest(["ingest"]) == 0          # ordinary run: sees none of it
    assert "file-history: 0 new snapshots, 0 resolved" in capsys.readouterr().out
    assert store.cmd_ingest(["ingest", flag]) == 0
    assert "file-history: 3 new snapshots, 2 resolved (full pass)" in capsys.readouterr().out
    con = store.connect(read_only=True)
    try:
        assert dict(con.execute("SELECT pathhash, path FROM file_history").fetchall()) == {
            h_snap: p_snap, h_delta: p_delta, h_orphan: None}  # orphan path never seen
        assert con.execute("SELECT epoch_ms(snapshot_ts) FROM file_history WHERE pathhash=?",
                           [h_delta]).fetchone()[0] == 1782997202000
        assert store.blob_count(con) == 3
    finally:
        con.close()
    before = _fh_rows(store.connect(read_only=True))
    assert store.cmd_ingest(["ingest", flag]) == 0    # idempotent
    con = store.connect(read_only=True)
    try:
        assert _fh_rows(con) == before
    finally:
        con.close()


def test_incremental_pass_defers_an_unresolvable_row_to_the_full_pass(
        tmp_path, monkeypatch):
    """A new row no cheap route resolves pays for one reverse-index scan on the
    run that inserts it, then stays path-less without costing later ordinary
    runs anything, even one whose new snapshot line names the file again (Claude
    Code repeats every tracked file in each snapshot, so the `scan_for` gate is
    what keeps that run from scanning): a path revealed by a later event is found
    by the next full pass, which also emits the row."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    sid, abspath = "sess-fh-defer2-0001", "/home/naka/proj/later.py"
    ph = _pathhash(abspath)
    _write_fh_files(fh_dir, sid, {f"{ph}@v1": "later v1\n"})
    transcript = projects / "-proj" / f"{sid}.jsonl"
    _write_jsonl(transcript, [                                     # relative key, no cwd
        _fh_snapshot_event("mL", {"later.py": {"backupFileName": f"{ph}@v1", "version": 1,
                                               "backupTime": "2026-07-02T14:00:01.000Z"}})])
    scans = []
    real_known = store._known_paths
    monkeypatch.setattr(store, "_known_paths", lambda con: scans.append(1) or real_known(con))
    _full_pass_ran_recently()

    def path_and_emits():
        con = store.connect(read_only=True)
        try:
            return (con.execute("SELECT path FROM file_history").fetchone()[0],
                    con.execute("SELECT count(*) FROM file_versions "
                                "WHERE source='file-history'").fetchone()[0])
        finally:
            con.close()

    assert store.cmd_ingest(["ingest"]) == 0
    assert path_and_emits() == (None, 0) and len(scans) == 1     # stored, scanned once
    with open(transcript, "a", encoding="utf-8") as f:            # reveal the path (a Read)
        f.write(json.dumps(_read_tool_use("mLr", sid, abspath, "2026-07-02T14:05:00.000Z")) + "\n")
        f.write(json.dumps(_fh_snapshot_event("mL2", {"later.py": {   # the file, tracked again
            "backupFileName": f"{ph}@v1", "version": 1,
            "backupTime": "2026-07-02T14:00:01.000Z"}})) + "\n")
    assert store.cmd_ingest(["ingest"]) == 0
    assert path_and_emits() == (None, 0) and len(scans) == 1     # ordinary run: no rescan
    assert store.cmd_ingest(["ingest", "--file-history-only"]) == 0
    assert path_and_emits() == (abspath, 1)


def test_incremental_and_full_pass_leave_the_same_rows(tmp_path, monkeypatch):
    """Correctness first: two ordinary runs over a transcript that grows in
    between (snapshot and delta lines, absolute and relative keys, a version gap,
    a repeated entry) leave exactly the file_history rows, synthetic
    file_versions rows and blobs that one full pass over the final transcript
    leaves in a fresh store."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    sid, cwd = "sess-fh-same-0001", "/home/naka/proj"
    pa, pb, pc = f"{cwd}/a.py", f"{cwd}/lib/b.py", f"{cwd}/lib/c.py"
    ha, hb, hc = _pathhash(pa), _pathhash(pb), _pathhash(pc)
    _write_fh_files(fh_dir, sid, {f"{ha}@v1": "a1\n", f"{ha}@v3": "a1\na3\n",
                                  f"{hb}@v1": "b1\n", f"{hc}@v2": "c2\n",
                                  f"{hc}@v5": "c2\nc5\n"})
    head = [
        {**_user_prompt("u-same-1", sid, "go", "2026-07-02T15:00:00.000Z"), "cwd": cwd},
        _fh_snapshot_event("ms1", {
            pa: {"backupFileName": f"{ha}@v1", "version": 1,
                 "backupTime": "2026-07-02T15:00:01.000Z"},
            "lib/b.py": {"backupFileName": f"{hb}@v1", "version": 1,
                         "backupTime": "2026-07-02T15:00:02.000Z"}}),
        _fh_delta_event("md1", "lib/c.py", f"{hc}@v2", 2, "2026-07-02T15:00:03.000Z",
                        f"{cwd}/lib"),
    ]
    tail = [
        _fh_snapshot_event("ms2", {
            pa: {"backupFileName": f"{ha}@v3", "version": 3,
                 "backupTime": "2026-07-02T15:00:04.000Z"},
            "lib/b.py": {"backupFileName": f"{hb}@v1", "version": 1,
                         "backupTime": "2026-07-02T15:00:02.000Z"}}),
        _fh_delta_event("md2", "lib/c.py", f"{hc}@v5", 5, "2026-07-02T15:00:05.000Z",
                        f"{cwd}/lib"),
    ]
    transcript = projects / "-home-naka-proj" / f"{sid}.jsonl"
    _write_jsonl(transcript, head)
    _full_pass_ran_recently()                       # both runs below stay incremental
    real_records = store._snapshot_records
    _forbid(monkeypatch, "_snapshot_records")
    assert store.cmd_ingest(["ingest"]) == 0
    with open(transcript, "a", encoding="utf-8") as f:
        f.write("".join(json.dumps(e) + "\n" for e in tail))
    assert store.cmd_ingest(["ingest"]) == 0
    monkeypatch.setattr(store, "_snapshot_records", real_records)   # ingest_all needs it
    con = store.connect(read_only=True)
    try:
        incremental = _fh_rows(con)
    finally:
        con.close()

    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "full.duckdb")
    con = store.connect()
    try:
        store.ingest_all(con)
        full = _fh_rows(con)
    finally:
        con.close()
    assert len(incremental[0]) == 5 and all(r[3] for r in incremental[0])   # all resolved
    assert len(incremental[1]) == 5
    assert incremental == full


def test_watch_runs_the_full_pass_first_then_incremental(tmp_path, monkeypatch):
    """A --watch process holds the store, so no --compact run can reach it. It
    follows the same stamp rule as a one-shot run, checked per sweep: on a fresh
    store its first sweep takes the full pass and later sweeps the incremental
    one, a restart within the interval stays incremental, and once
    FILE_HISTORY_FULL_INTERVAL has passed a sweep takes it again."""
    projects, _fh_dir = _setup_fh(tmp_path, monkeypatch)
    _write_jsonl(projects / "-proj" / "sess-fh-watch-0001.jsonl",
                 [_user_prompt("u-w-1", "sess-fh-watch-0001", "hi", "2026-07-02T16:00:00.000Z")])
    modes = []
    real = store.ingest_file_history
    monkeypatch.setattr(store, "ingest_file_history", lambda con, **k: (
        modes.append("full" if k.get("records") is None else "incremental"), real(con, **k))[1])

    class _Stop(Exception):
        pass

    def run(sweeps):
        modes.clear()
        sleeps = []

        def sleep(_s):
            sleeps.append(1)
            if len(sleeps) == sweeps:
                raise _Stop

        monkeypatch.setattr(store.time, "sleep", sleep)
        with pytest.raises(_Stop):
            store.cmd_ingest(["ingest", "--watch"])
        return list(modes)

    assert run(3) == ["full", "incremental", "incremental"]
    assert run(2) == ["incremental", "incremental"]
    monkeypatch.setattr(store, "FILE_HISTORY_FULL_INTERVAL", 0.0)
    assert run(2) == ["full", "full"]


def test_fh_full_due_follows_its_stamp(tmp_path, monkeypatch):
    """The full pass is due with no stamp (a fresh store), with one at least an
    interval old, and with one from the future (a clock moved back); a fresh
    stamp keeps it off unless the run forces it."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    assert store._fh_full_due() is True
    _full_pass_ran_recently()
    assert store._fh_full_due() is False
    assert store._fh_full_due(force=True) is True
    _age_full_pass_stamp()
    assert store._fh_full_due() is True
    future = time.time() + 600
    os.utime(store._fh_full_stamp(), (future, future))
    assert store._fh_full_due() is True


def test_ordinary_run_takes_the_full_pass_once_its_stamp_is_due(
        tmp_path, monkeypatch, capsys):
    """Setups that never pass --compact (plain cron, `ingest --detach`) still get
    the safety net. A line a crashed run stored and an orphan file wait only
    until the recorded full pass is FILE_HISTORY_FULL_INTERVAL old; then the next
    ordinary run takes the full pass and records it, and the runs after it are
    incremental again."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    sid, orphan_sid = "sess-fh-due-0001", "sess-fh-due-orphan"
    path = "/home/naka/proj/due.py"
    ph, h_orphan = _pathhash(path), _pathhash("/home/naka/proj/due_orphan.py")
    _write_fh_files(fh_dir, sid, {f"{ph}@v1": "due\n"})
    _write_fh_files(fh_dir, orphan_sid, {f"{h_orphan}@v1": "orphan\n"})
    _write_jsonl(projects / "-proj" / f"{sid}.jsonl", [
        _fh_snapshot_event("mdu", {path: {"backupFileName": f"{ph}@v1", "version": 1,
                                          "backupTime": "2026-07-02T17:00:01.000Z"}})])
    con = store.connect()
    try:
        store._sweep_transcripts(con)                 # the crashed run: line in, no parse
    finally:
        con.close()
    _full_pass_ran_recently()

    assert store.cmd_ingest(["ingest"]) == 0          # stamp fresh: incremental, sees none
    out = capsys.readouterr().out
    assert "file-history: 0 new snapshots, 0 resolved" in out and "(full pass)" not in out
    _age_full_pass_stamp()
    aged = store._fh_full_stamp().stat().st_mtime
    assert store.cmd_ingest(["ingest"]) == 0          # no flag, the stamp is due
    assert ("file-history: 2 new snapshots, 1 resolved (full pass)"
            in capsys.readouterr().out)
    assert store._fh_full_stamp().stat().st_mtime > aged
    assert store.cmd_ingest(["ingest"]) == 0
    assert "(full pass)" not in capsys.readouterr().out
    con = store.connect(read_only=True)
    try:
        assert dict(con.execute("SELECT pathhash, path FROM file_history").fetchall()) == {
            ph: path, h_orphan: None}
    finally:
        con.close()


def test_full_pass_stamp_moves_only_when_the_pass_finishes(tmp_path, monkeypatch):
    """The stamp records a full pass that finished. A run whose full pass raises
    and a run skipped on a reader's lock both leave it due, so the next run
    takes the pass instead of waiting another interval."""
    projects, _fh_dir = _setup_fh(tmp_path, monkeypatch)
    _write_jsonl(projects / "-proj" / "sess-fh-stamp-0001.jsonl",
                 [_user_prompt("u-st-1", "sess-fh-stamp-0001", "hi", "2026-07-02T18:00:00.000Z")])
    real = store.ingest_file_history

    def broken(con, **k):
        if k.get("records") is None:
            raise RuntimeError("full pass failed")
        return real(con, **k)

    monkeypatch.setattr(store, "ingest_file_history", broken)
    with pytest.raises(RuntimeError, match="full pass failed"):
        store.cmd_ingest(["ingest"])
    assert not store._fh_full_stamp().exists()

    monkeypatch.setattr(store, "ingest_file_history", real)
    monkeypatch.setattr(store, "connect_writer", lambda *a, **k: None)   # a reader holds it
    assert store.cmd_ingest(["ingest"]) == 0
    assert not store._fh_full_stamp().exists()


@pytest.mark.parametrize("late", ["crashed-run", "orphan", "out-of-order"])
def test_a_late_lower_version_rechains_its_emitted_successor(
        tmp_path, monkeypatch, capsys, late):
    """v2 is emitted while v1 is missing, so its row has pre_sha NULL and no
    patch. When v1 arrives later (a line a crashed run stored, found by the full
    pass; an orphan file the full pass globs; a line naming v1 after v2, read by
    an ordinary run) the emit walk re-chains v2 to it, so the store ends with the
    rows one full pass over the final state leaves in a fresh store. The
    re-emitted row is not counted as new, and a re-run changes nothing."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    sid, path = "sess-fh-late-0001", "/home/naka/proj/late.py"
    ph = _pathhash(path)
    v1, v2 = "one\n", "one\ntwo\n"
    _write_fh_files(fh_dir, sid, {f"{ph}@v1": v1, f"{ph}@v2": v2})
    transcript = projects / "-proj" / f"{sid}.jsonl"

    def line(mid, ver, btime):
        return _fh_snapshot_event(mid, {path: {"backupFileName": f"{ph}@v{ver}",
                                               "version": ver, "backupTime": btime}})

    def append(ev):
        with open(transcript, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev) + "\n")

    def v2_rows():
        con = store.connect(read_only=True)
        try:
            return con.execute("SELECT pre_sha, patch FROM file_versions WHERE event_id=?",
                               [f"fh:{sid}:{ph}@v2"]).fetchall()
        finally:
            con.close()

    def all_rows():
        con = store.connect(read_only=True)
        try:
            return _fh_rows(con)
        finally:
            con.close()

    l1 = line("mla1", 1, "2026-07-02T19:00:01.000Z")
    l2 = line("mla2", 2, "2026-07-02T19:05:01.000Z")
    _full_pass_ran_recently()
    if late == "crashed-run":
        _write_jsonl(transcript, [l1])
        con = store.connect()
        try:
            store._sweep_transcripts(con)             # the crashed run: v1's line in, no parse
        finally:
            con.close()
        append(l2)
    else:
        _write_jsonl(transcript, [l2])
    assert store.cmd_ingest(["ingest"]) == 0          # ordinary run: emits v2 alone
    assert v2_rows() == [(None, None)]
    capsys.readouterr()

    if late == "out-of-order":
        append(l1)
        assert store.cmd_ingest(["ingest"]) == 0      # the incremental pass repairs it
    else:
        assert store.cmd_ingest(["ingest", "--file-history-only"]) == 0
    out = capsys.readouterr().out
    assert "file-history: 1 new snapshots, 1 resolved" in out
    assert ("(full pass)" in out) == (late != "out-of-order")
    [(pre, patch)] = v2_rows()
    assert pre == store._sha(v1) and "+two" in patch
    got = all_rows()
    assert store.cmd_ingest(["ingest", "--file-history-only"]) == 0
    assert all_rows() == got                          # idempotent

    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "fresh.duckdb")
    con = store.connect()
    try:
        store.ingest_all(con)
        assert _fh_rows(con) == got
    finally:
        con.close()


def test_resolution_checks_every_candidate_against_its_pathhash(tmp_path, monkeypatch):
    """Route 2 (realParentDir plus the key's file name) is checked like every
    other route. Claude Code's realParentDir is realpath(dirname), while the
    backup name hashes the logical path, so under a symlinked project the
    route-2 candidate is a different string: it must be rejected, and the row
    resolves to the logical path through sessions.cwd. In a session with no cwd,
    a realParentDir that does match is what resolves a relative key, with no
    reverse-index scan."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    s_link, s_nocwd = "sess-fh-link-0001", "sess-fh-nocwd-0001"
    cwd = "/home/naka/proj"                           # a symlink to /mnt/real/proj
    p_link, p_nocwd = f"{cwd}/sub/linked.py", "/home/naka/other/pkg/m.py"
    h_link, h_nocwd = _pathhash(p_link), _pathhash(p_nocwd)
    _write_fh_files(fh_dir, s_link, {f"{h_link}@v1": "linked\n"})
    _write_fh_files(fh_dir, s_nocwd, {f"{h_nocwd}@v1": "nocwd\n"})
    _write_jsonl(projects / "-home-naka-proj" / f"{s_link}.jsonl", [
        {**_user_prompt("u-link-1", s_link, "go", "2026-07-02T20:00:00.000Z"), "cwd": cwd},
        _fh_delta_event("mlk", "sub/linked.py", f"{h_link}@v1", 1,
                        "2026-07-02T20:00:01.000Z", "/mnt/real/proj/sub")])
    _write_jsonl(projects / "-home-naka-other" / f"{s_nocwd}.jsonl", [   # no cwd line at all
        _fh_delta_event("mnc", "pkg/m.py", f"{h_nocwd}@v1", 1,
                        "2026-07-02T20:00:02.000Z", "/home/naka/other/pkg")])
    _full_pass_ran_recently()
    _forbid(monkeypatch, "_snapshot_records", "_known_paths")
    assert store.cmd_ingest(["ingest"]) == 0
    con = store.connect(read_only=True)
    try:
        assert dict(con.execute("SELECT pathhash, path FROM file_history").fetchall()) == {
            h_link: p_link, h_nocwd: p_nocwd}
        assert con.execute("SELECT count(*) FROM file_versions "
                           "WHERE source='file-history'").fetchone()[0] == 2
    finally:
        con.close()


def test_ordinary_run_emits_a_row_it_resolves_for_an_earlier_run(
        tmp_path, monkeypatch, capsys):
    """A row an earlier run stored path-less is emitted by the ordinary run that
    resolves it, not left for the next full pass: here a later delta line for
    the same version carries the realParentDir the snapshot line lacked."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    sid, path = "sess-fh-reveal-0001", "/home/naka/proj/reveal.py"
    ph = _pathhash(path)
    _write_fh_files(fh_dir, sid, {f"{ph}@v1": "reveal\n"})
    transcript = projects / "-proj" / f"{sid}.jsonl"
    _write_jsonl(transcript, [                                     # relative key, no cwd
        _fh_snapshot_event("mrv", {"reveal.py": {"backupFileName": f"{ph}@v1", "version": 1,
                                                 "backupTime": "2026-07-02T21:00:01.000Z"}})])

    def path_and_emits():
        con = store.connect(read_only=True)
        try:
            return (con.execute("SELECT path FROM file_history").fetchone()[0],
                    con.execute("SELECT count(*) FROM file_versions "
                                "WHERE source='file-history'").fetchone()[0])
        finally:
            con.close()

    _full_pass_ran_recently()
    assert store.cmd_ingest(["ingest"]) == 0
    assert path_and_emits() == (None, 0)
    _forbid(monkeypatch, "_snapshot_records", "_known_paths")
    with open(transcript, "a", encoding="utf-8") as f:
        f.write(json.dumps(_fh_delta_event("mrv2", "reveal.py", f"{ph}@v1", 1,
                                           "2026-07-02T21:00:01.000Z",
                                           "/home/naka/proj")) + "\n")
    capsys.readouterr()
    assert store.cmd_ingest(["ingest"]) == 0
    assert "file-history: 0 new snapshots, 1 resolved" in capsys.readouterr().out
    assert path_and_emits() == (path, 1)


# --- Blob file: content in <store>.blobs.sqlite (ingest CPU plan, phase 4) ------
#
# Schema 3 keeps file contents in a SQLite file next to the DuckDB store, so a
# new blob no longer makes the next CHECKPOINT rewrite every blob. These tests
# pin the parts that cannot be repaired later: the write order (no DuckDB row
# names an uncommitted blob), the schema 2 -> 3 move (resumable, verified before
# the drop, readers byte-identical before and after), and readers that meet a
# not-yet-migrated store or a missing blob file.

BLOBMIG_FH_PATH = "/home/naka/proj/mig.py"
BLOBMIG_EDIT_PATH = "/tmp/demo_mig.py"


def _build_blob_store(tmp_path, monkeypatch):
    """A closed store whose blobs come from both sources: a Write of CATDIFF_PATH
    plus a patch-only Edit of it (cat rolls that tail forward), an Edit of
    BLOBMIG_EDIT_PATH (pre + post blobs), and two file-history snapshots of
    BLOBMIG_FH_PATH (the emit walk diffs v1 -> v2 from their blobs)."""
    projects, fh_dir = _setup_fh(tmp_path, monkeypatch)
    sid = "sess-blob-0001"
    ph = _pathhash(BLOBMIG_FH_PATH)
    _write_fh_files(fh_dir, sid, {f"{ph}@v1": "mig v1\n", f"{ph}@v2": "mig v1\nmig v2\n"})
    _write_jsonl(projects / "-proj" / f"{sid}.jsonl", [
        _write_create_event(sid, "bm-w", CATDIFF_PATH, "alpha\nbeta\n"),
        _patch_only_edit_event(sid, "bm-p", CATDIFF_PATH, "2026-06-30T04:00:02.000Z",
                               [{"oldStart": 2, "oldLines": 1, "newStart": 2, "newLines": 1,
                                 "lines": ["-beta", "+BETA"]}]),
        _edit_result_event(sid, "bm-e", BLOBMIG_EDIT_PATH),
        _fh_snapshot_event("bm1", {BLOBMIG_FH_PATH: {"backupFileName": f"{ph}@v1", "version": 1,
                                                     "backupTime": "2026-07-01T10:00:01.000Z"}}),
        _fh_snapshot_event("bm2", {BLOBMIG_FH_PATH: {"backupFileName": f"{ph}@v2", "version": 2,
                                                     "backupTime": "2026-07-01T10:00:02.000Z"}}),
    ])
    con = store.connect()
    try:
        store.ingest_all(con)
    finally:
        con.close()


def _reader_outputs(capsys):
    """(exit code, stdout, stderr) of every blob-reading CLI path, plus the
    stats lines that sum blob sizes, keyed by name."""
    capsys.readouterr()
    out = {}
    for name, argv in [
            ("cat", ["cat", CATDIFF_PATH]),
            ("cat --at", ["cat", CATDIFF_PATH, "--at", "2026-06-30T04:00:01.000Z"]),
            ("cat edit", ["cat", BLOBMIG_EDIT_PATH]),
            ("cat fh", ["cat", BLOBMIG_FH_PATH]),
            ("cat fh --at", ["cat", BLOBMIG_FH_PATH, "--at", "2026-07-01T10:00:01.500Z"]),
            ("diff", ["diff", CATDIFF_PATH]),
            ("diff fh", ["diff", BLOBMIG_FH_PATH, "--all-sources"])]:
        rc = (store.cmd_cat if argv[0] == "cat" else store.cmd_diff)(argv)
        got = capsys.readouterr()
        out[name] = (rc, got.out, got.err)
    assert store.cmd_stats(["stats"]) == 0
    lines = capsys.readouterr().out.splitlines()
    i = lines.index("== blobs / bytes ==")
    out["stats blobs"] = lines[i + 1]
    out["stats snapshot bytes"] = next(ln for ln in lines if "snapshot bytes" in ln)
    return out


def _blob_file_rows():
    """{sha: (size, text)} straight from the blob file, by a connection of its own."""
    db = sqlite3.connect(store._blob_path())
    try:
        return {sha: (size, zlib.decompress(c).decode("utf-8"))
                for sha, size, c in db.execute("SELECT sha, size, content FROM blobs")}
    finally:
        db.close()


def _demote_to_schema2():
    """Turn the closed schema 3 store into what a schema 2 release left: the blobs
    in a DuckDB table with the old DDL, no blob file, version '2'. Returns the
    rows as {sha: (size, text)}."""
    import duckdb
    rows = _blob_file_rows()
    bp = store._blob_path()
    for p in (bp, Path(str(bp) + "-wal"), Path(str(bp) + "-shm")):
        p.unlink(missing_ok=True)
    raw = duckdb.connect(str(store.STORE_PATH))
    try:
        raw.execute("CREATE TABLE blobs(sha TEXT PRIMARY KEY, size BIGINT, content TEXT)")
        raw.executemany("INSERT INTO blobs VALUES (?,?,?)",
                        [(sha, size, text) for sha, (size, text) in rows.items()])
        raw.execute("UPDATE schema_meta SET value='2' WHERE key='version'")
    finally:
        raw.close()
    return rows


def _duck_state():
    """(DuckDB still has `blobs`, its row count or None, schema version), read-only."""
    con = store.connect(read_only=True)
    try:
        has = store._duck_has_blobs(con)
        n = con.execute("SELECT count(*) FROM blobs").fetchone()[0] if has else None
        version = con.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0]
        return has, n, version
    finally:
        con.close()


def test_migration_from_schema2_moves_blobs_and_keeps_readers_byte_identical(
        tmp_path, monkeypatch, capsys):
    """The first writer connect on a schema 2 store copies every DuckDB blob into
    the blob file, drops the table, records '3' and compacts. cat (latest,
    --at, the roll-forward, file-history snapshots), diff and the stats sums are
    byte-identical on the original store, on the schema 2 store (whose reader
    reads the DuckDB table and creates no blob file), and after the move. A
    full re-ingest afterwards adds nothing."""
    _build_blob_store(tmp_path, monkeypatch)
    want = _reader_outputs(capsys)
    assert want["cat"] == (0, "alpha\nBETA\n", "")             # the fixture exercises the roll-forward
    assert want["cat fh"][1] == "mig v1\nmig v2\n"
    assert "+mig v2" in want["diff fh"][1]                     # diffed from the two blobs

    rows = _demote_to_schema2()
    assert len(rows) == 5
    assert _duck_state() == (True, 5, "2")
    assert _reader_outputs(capsys) == want
    assert not store._blob_path().exists()                     # a reader never creates it

    con = store.connect()                                      # migrates
    try:
        assert not store._duck_has_blobs(con)
        assert con.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0] == "3"
        free_blocks = con.execute("PRAGMA database_size").fetchone()[5]
    finally:
        con.close()
    log = capsys.readouterr().out
    assert "blobs: moving 5 rows" in log and "(5 copied this run), all verified" in log
    assert "blobs: compacted" in log
    assert free_blocks == 0                                    # the drop was compacted away
    assert _blob_file_rows() == rows
    assert _reader_outputs(capsys) == want

    con = store.connect()                                      # nothing left to move
    try:
        def counts():
            return (store.blob_count(con),
                    con.execute("SELECT count(*) FROM file_versions").fetchone()[0],
                    con.execute("SELECT count(*) FROM file_history").fetchone()[0])
        before = counts()
        con.execute("DELETE FROM ingest_state")                # re-read every transcript line
        store.ingest_all(con)
        assert counts() == before
    finally:
        con.close()
    assert "blobs: moving" not in capsys.readouterr().out
    assert _blob_file_rows() == rows
    assert _reader_outputs(capsys) == want


def test_migration_killed_mid_copy_resumes_and_readers_keep_the_duckdb_table(
        tmp_path, monkeypatch, capsys):
    """A run killed during the copy leaves the DuckDB table whole and version '2';
    the batches it committed stay in the blob file. Readers keep reading the
    DuckDB table (the partial file is not theirs yet), and the next writer
    connect copies only what is missing, verifies all of it and finishes."""
    _build_blob_store(tmp_path, monkeypatch)
    want = _reader_outputs(capsys)
    rows = _demote_to_schema2()
    monkeypatch.setattr(store, "_BLOB_MIGRATE_BATCH", 1)       # one commit per row
    real_pack = store._pack_blob
    packed = []

    def killed_after_two(text):
        if len(packed) == 2:
            raise KeyboardInterrupt("timeout 300 killed the run")
        packed.append(text)
        return real_pack(text)

    monkeypatch.setattr(store, "_pack_blob", killed_after_two)
    with pytest.raises(KeyboardInterrupt):
        store.connect()
    assert _duck_state() == (True, 5, "2")
    assert len(_blob_file_rows()) == 2                         # two committed batches survive
    assert _reader_outputs(capsys) == want

    monkeypatch.setattr(store, "_pack_blob", real_pack)
    store.connect().close()
    assert "(3 copied this run), all verified" in capsys.readouterr().out
    assert _duck_state() == (False, None, "3")
    assert _blob_file_rows() == rows
    assert _reader_outputs(capsys) == want


def test_migration_killed_before_the_drop_recopies_a_bad_row_then_drops(
        tmp_path, monkeypatch, capsys):
    """A run that dies after the copy but before the drop leaves everything in
    both places. If a copied row is then wrong (here: overwritten with other
    bytes), the next connect's verify finds it, copies it again from DuckDB and
    only then drops the table."""
    _build_blob_store(tmp_path, monkeypatch)
    want = _reader_outputs(capsys)
    rows = _demote_to_schema2()
    real_verify = store._blob_copy_mismatches

    def killed(*a, **k):
        raise KeyboardInterrupt("killed between copy and verify")

    monkeypatch.setattr(store, "_blob_copy_mismatches", killed)
    with pytest.raises(KeyboardInterrupt):
        store.connect()
    assert _duck_state() == (True, 5, "2")
    assert _blob_file_rows() == rows                           # fully copied, nothing dropped

    victim = sorted(rows)[0]
    db = sqlite3.connect(store._blob_path())
    db.execute("UPDATE blobs SET content=? WHERE sha=?", [zlib.compress(b"torn"), victim])
    db.commit()
    db.close()
    monkeypatch.setattr(store, "_blob_copy_mismatches", real_verify)
    capsys.readouterr()
    store.connect().close()
    log = capsys.readouterr().out
    assert "blobs: 1 rows differ from DuckDB; copying them again" in log
    assert "(0 copied this run), all verified" in log
    assert _duck_state() == (False, None, "3")
    assert _blob_file_rows() == rows
    assert _reader_outputs(capsys) == want


def test_a_refused_move_keeps_ingest_on_schema2_and_retries_hourly(tmp_path, monkeypatch, capsys):
    """When a row still differs after the second copy, the move refuses: the
    DuckDB table and version stay, the failure is stamped, and connect() still
    returns a writer, so readers and ingest keep working. Until the stamp is
    BLOB_MOVE_RETRY_INTERVAL old a run does not copy or verify again, logs one
    error line, and captures as schema 2 did (the new blob goes to the DuckDB
    table, where cat finds it). The retry after that moves every row, the new
    one too, and clears the stamp."""
    _build_blob_store(tmp_path, monkeypatch)
    want = _reader_outputs(capsys)
    rows = _demote_to_schema2()
    real_verify = store._blob_copy_mismatches
    verified = []

    def refuse(con, db, shas=None):
        verified.append(shas)
        return {"f" * 64}

    monkeypatch.setattr(store, "_blob_copy_mismatches", refuse)
    store.connect().close()
    log = capsys.readouterr().out
    errors = [ln for ln in log.splitlines() if "error: blobs not moved" in ln]
    assert "blobs: moving 5 rows" in log and len(errors) == 1
    assert "(RuntimeError: 1 rows do not verify after a second copy)" in errors[0]
    assert "next attempt in 60 min" in errors[0]
    stamp = store._blob_move_stamp()
    assert stamp.read_text() == "RuntimeError: 1 rows do not verify after a second copy\n"
    assert _duck_state() == (True, 5, "2")
    assert _reader_outputs(capsys) == want

    verified.clear()
    new_path = "/tmp/written_while_refused.py"
    _write_jsonl(store.PROJECTS_DIR / "-proj" / "sess-blob-0002.jsonl",
                 [_write_create_event("sess-blob-0002", "bm-new", new_path, "new = 1\n")])
    assert store.cmd_ingest(["ingest"]) == 0
    log = capsys.readouterr().out
    assert verified == [] and "blobs: moving" not in log         # no attempt within the hour
    assert sum("error: blobs not moved" in ln for ln in log.splitlines()) == 1
    assert "+1 file_versions, +1 blobs" in log
    assert _duck_state() == (True, 6, "2")
    assert store.cmd_cat(["cat", new_path]) == 0
    assert capsys.readouterr().out == "new = 1\n"

    future = time.time() + 600
    os.utime(stamp, (future, future))                          # a clock moved back
    assert store._blob_move_due()
    old = time.time() - store.BLOB_MOVE_RETRY_INTERVAL - 1
    os.utime(stamp, (old, old))
    monkeypatch.setattr(store, "_blob_copy_mismatches", real_verify)
    store.connect().close()
    log = capsys.readouterr().out
    assert "(1 copied this run), all verified" in log and "error:" not in log
    assert not stamp.exists()
    assert _duck_state() == (False, None, "3")
    moved = _blob_file_rows()
    assert len(moved) == 6 and {sha: moved[sha] for sha in rows} == rows
    assert store.cmd_cat(["cat", new_path]) == 0
    assert capsys.readouterr().out == "new = 1\n"
    got = _reader_outputs(capsys)
    assert {k: v for k, v in got.items() if not k.startswith("stats")} == {
        k: v for k, v in want.items() if not k.startswith("stats")}


def test_readers_name_a_missing_blob_file_instead_of_raising(tmp_path, monkeypatch, capsys):
    """On a migrated store whose blob file is gone, cat exits 1 with a message
    naming the file, stats and db_info say so, and no reader recreates it. The
    next writer recreates it empty and says in the log that content is lost."""
    _build_blob_store(tmp_path, monkeypatch)
    bp = store._blob_path()
    for p in (bp, Path(str(bp) + "-wal"), Path(str(bp) + "-shm")):
        p.unlink(missing_ok=True)
    capsys.readouterr()

    assert store.cmd_cat(["cat", CATDIFF_PATH]) == 1
    got = capsys.readouterr()
    assert got.out == "" and got.err == f"error: blob file missing: {bp} (the store keeps file contents there)\n"
    assert store.cmd_stats(["stats"]) == 0
    out = capsys.readouterr().out
    assert f"blob file missing: {bp}" in out
    assert "snapshot bytes  ? (blob file missing)" in out
    con = store.connect(read_only=True)
    try:
        info = dict(store.db_info(con))
    finally:
        con.close()
    assert info["blob file"] == f"missing: {bp}"
    assert "rows blobs" not in info
    assert not bp.exists()

    store.connect().close()
    assert "warning: blob file" in capsys.readouterr().out
    assert bp.exists()
    assert store.cmd_cat(["cat", CATDIFF_PATH]) == 1
    got = capsys.readouterr()
    assert got.out == "no stored version for that path/time\n"
    assert "skipped 1 version(s) whose content the blob file does not hold" in got.err


def test_cat_skips_a_version_whose_blob_is_missing_and_rolls_the_older_one_forward(
        tmp_path, monkeypatch, capsys):
    """Two versions with bytes (a Write, then an Edit of it) and a patch-only
    edit after them. With the Edit's blob gone from the file, cat takes the
    Write's bytes, as the old JOIN on `blobs` did, rolls the Edit's patch and
    the patch-only one forward on top, and says it skipped one version."""
    sid = "sess-cat-skip-01"
    pre = "line one\nline two\nline three\n"                  # the Edit's originalFile
    _build_reconstruction_store(tmp_path, monkeypatch, sid, [
        _write_create_event(sid, "sk-w", CATDIFF_PATH, pre),
        _edit_result_event(sid, "sk-e", CATDIFF_PATH),
        _patch_only_edit_event(sid, "sk-p", CATDIFF_PATH, "2026-06-30T04:00:02.000Z",
                               [{"oldStart": 4, "oldLines": 1, "newStart": 4, "newLines": 1,
                                 "lines": ["-line three", "+line THREE"]}])])
    want = "line one\nline two CHANGED\nline two-b\nline THREE\n"
    capsys.readouterr()
    assert store.cmd_cat(["cat", CATDIFF_PATH]) == 0
    assert capsys.readouterr() == (want, "")

    db = sqlite3.connect(store._blob_path())
    gone = db.execute("DELETE FROM blobs WHERE sha=?", [store._blob_sha(CATDIFF_EDIT_POST)]).rowcount
    db.commit()
    db.close()
    assert gone == 1
    assert store.cmd_cat(["cat", CATDIFF_PATH]) == 0
    got = capsys.readouterr()
    assert got.out == want
    assert got.err == "warning: skipped 1 version(s) whose content the blob file does not hold\n"


def test_reader_reads_the_blob_file_while_a_writer_is_inserting(tmp_path, monkeypatch, capsys):
    """A writer in another process holds an open write transaction on the blob
    file (an insert not yet committed). A reader opens it read-only and reads
    the committed rows, not the pending one."""
    _build_blob_store(tmp_path, monkeypatch)
    want = _reader_outputs(capsys)["cat"]
    writer = textwrap.dedent("""
        import sqlite3, sys
        db = sqlite3.connect(sys.argv[1])
        db.execute("INSERT INTO blobs VALUES ('pending', 1, x'00')")
        print("in transaction" if db.in_transaction else "no transaction", flush=True)
        sys.stdin.readline()
        db.rollback()
        db.close()
    """)
    proc = subprocess.Popen([sys.executable, "-I", "-c", writer, str(store._blob_path())],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "in transaction"
        assert store.cmd_cat(["cat", CATDIFF_PATH]) == want[0]
        got = capsys.readouterr()
        assert (got.out, got.err) == want[1:]
        con = store.connect(read_only=True)
        try:
            assert store.blob_texts(con, ["pending"]) == {}
            assert store.blob_count(con) == 5
        finally:
            con.close()
    finally:
        proc.stdin.write("\n")
        proc.stdin.flush()
        assert proc.wait(timeout=10) == 0


class _OrderCheckingCon(_CountingCon):
    """At each DuckDB insert that writes sha references, asserts that every sha it
    is about to write is already COMMITTED to the blob file, read by a separate
    read-only connection (which cannot see an open transaction's rows)."""

    _STAGED = {"INSERT INTO file_versions": "SELECT pre_sha, post_sha FROM _fv_stage",
               "INSERT INTO file_history": "SELECT sha FROM _fh_stage"}

    def __init__(self, con):
        super().__init__(con)
        self.checked = set()

    def execute(self, sql, *args, **kwargs):
        for prefix, staged in self._STAGED.items():
            if sql.startswith(prefix):
                shas = {s for row in self._con.execute(staged).fetchall() for s in row if s}
                ro = sqlite3.connect(store._blob_path().resolve().as_uri() + "?mode=ro", uri=True)
                try:
                    committed = {sha for (sha,) in ro.execute("SELECT sha FROM blobs")}
                finally:
                    ro.close()
                assert shas <= committed, f"{prefix} names uncommitted blobs"
                self.checked |= shas
        return super().execute(sql, *args, **kwargs)


def _ingest_through_order_check(tmp_path, monkeypatch):
    """ingest_all of the _build_blob_store inputs through an _OrderCheckingCon."""
    real_connect = store.connect
    checking = []

    def connect_checked(read_only=False):
        con = real_connect(read_only)
        if read_only:
            return con
        wrapped = _OrderCheckingCon(con)
        store._BLOB_DBS[wrapped] = store._BLOB_DBS[con]   # the writer's blob file, seen through the wrapper
        checking.append((wrapped, con))
        return wrapped

    monkeypatch.setattr(store, "connect", connect_checked)
    _build_blob_store(tmp_path, monkeypatch)
    return checking[0][0]


def test_blobs_are_committed_before_any_duckdb_row_names_them(tmp_path, monkeypatch):
    """The write order that makes a crash harmless: by the time file_versions or
    file_history rows reach DuckDB, every blob they name is committed."""
    checking = _ingest_through_order_check(tmp_path, monkeypatch)
    assert len(checking.checked) == 5          # every blob of the fixture was checked


def test_order_check_catches_a_reference_to_an_uncommitted_blob(tmp_path, monkeypatch):
    """The control for the test above: with the commit skipped, the first
    insert of a sha reference trips the check."""
    monkeypatch.setattr(store, "_commit_blobs", lambda con: None)
    with pytest.raises(AssertionError, match="names uncommitted blobs"):
        _ingest_through_order_check(tmp_path, monkeypatch)


def test_cmd_reset_db_deletes_the_blob_file_and_its_wal_and_shm_last(
        tmp_path, monkeypatch, capsys):
    """reset-db removes the blob file with its -wal and -shm (as a killed writer
    leaves them), after the DuckDB files, so a reset that stops half way never
    leaves DuckDB naming blobs whose file is gone."""
    _build_cli_store(tmp_path, monkeypatch)
    bp = store._blob_path()
    side = [Path(str(bp) + "-wal"), Path(str(bp) + "-shm")]
    for p in side:
        p.write_bytes(b"")
    assert bp.exists()
    assert store.cmd_reset_db(["reset-db", "--yes"]) == 0
    assert not any(p.exists() for p in [store.STORE_PATH, bp, *side])
    deleted = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("deleted ")]
    assert deleted == [f"deleted {p}" for p in [store.STORE_PATH, bp, *side]]


def test_compact_db_leaves_the_blob_file_alone(tmp_path, monkeypatch, capsys):
    """The os.replace rewrite touches only the DuckDB file: the blob file keeps
    its bytes and mtime, and cat still prints the same content."""
    _build_blob_store(tmp_path, monkeypatch)
    want = _reader_outputs(capsys)
    bp = store._blob_path()
    before = (bp.read_bytes(), bp.stat().st_mtime_ns)
    store.compact_db()
    assert (bp.read_bytes(), bp.stat().st_mtime_ns) == before
    assert _reader_outputs(capsys) == want


def test_db_info_stats_and_store_size_count_the_blob_file(tmp_path, monkeypatch, capsys):
    """db_info names the blob file with its size and the blob rows; stats prints
    their count and bytes; the store size the ingest log reports includes it."""
    _build_blob_store(tmp_path, monkeypatch)
    rows = _blob_file_rows()
    nbytes = sum(size for size, _text in rows.values())
    bp = store._blob_path()
    con = store.connect(read_only=True)
    try:
        info = dict(store.db_info(con))
        assert store.blob_totals(con) == (5, nbytes)
    finally:
        con.close()
    assert info["blob file"] == f"{bp} ({store._fmt_bytes(bp.stat().st_size)})"
    assert info["rows blobs"] == f"5 ({store._fmt_bytes(nbytes)} uncompressed)"
    out = _reader_outputs(capsys)
    fh_bytes = len("mig v1\n") + len("mig v1\nmig v2\n")
    assert out["stats blobs"] == f"   5 {nbytes}"
    assert out["stats snapshot bytes"] == f"   snapshot bytes  {fh_bytes}"
    assert store._store_size() == store.STORE_PATH.stat().st_size + bp.stat().st_size


def test_ingest_log_counts_new_blobs_from_the_blob_file(tmp_path, monkeypatch, capsys):
    """The `stored:` line still reports new blobs, now counted in the blob file."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    sid = "sess-blob-log-0001"
    _write_jsonl(projects / "-proj" / f"{sid}.jsonl",
                 [_write_create_event(sid, "bl-w", "/tmp/log_demo.py", "x = 1\n")])
    assert store.cmd_ingest(["ingest"]) == 0
    assert "+1 file_versions, +1 blobs" in capsys.readouterr().out


def test_put_blob_needs_the_writer_and_keeps_a_lone_surrogate(tmp_path, monkeypatch):
    """A read-only connection cannot put a blob. The writer stores a lone
    surrogate (only a malformed JSON escape makes one) and reads it back intact;
    DuckDB used to raise on it."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    con = store.connect()
    try:
        sha = store._put_blob(con, "a\ud800b")
        assert store.blob_texts(con, [sha]) == {sha: "a\ud800b"}
    finally:
        con.close()
    ro = store.connect(read_only=True)
    try:
        with pytest.raises(RuntimeError, match="writer connection"):
            store._put_blob(ro, "text")
    finally:
        ro.close()


def test_blob_sha_covers_the_stored_bytes_and_cat_prints_a_lone_surrogate(
        tmp_path, monkeypatch, capsysbinary):
    """A blob's sha is taken over the bytes the blob file stores. For ordinary
    text that is the sha it always had (pinned: existing rows and the dedupe
    depend on it). A Write holding a lone surrogate and a Write of the same
    text with "?" in its place get two shas, and cat prints each file's own
    bytes; with the "replace" sha they shared one and cat raised."""
    assert store._blob_sha("alpha\nbeta\n") == (
        "e49c81e2d2f84e259d40e2fb8192f3bcd198b355184845d76d8f58807d0d78ee")
    assert store._blob_sha("café ✓\n") == (
        "7953dfd42d596ffc230a3ba67f3f1e856f53b477e64300f6eda9e4f94021e32e")
    for text in ("", "alpha\nbeta\n", "café ✓\n"):
        assert store._blob_sha(text) == store._sha(text)
    assert store._blob_sha("x\ud800y\n") == (
        "614ace69d9d66702b40ddc2fdb7d861eebf12f87c36797a5a75d643c5a7c70c8")
    assert store._blob_sha("x?y\n") == (
        "63e9304e35ad3e577a77e74da55fd34d38ebe553a6200b3c6b836e1a56df789e")

    sid = "sess-surrogate-01"
    _build_reconstruction_store(tmp_path, monkeypatch, sid, [   # json.dumps escapes it as \ud800
        _write_create_event(sid, "sur-w", "/tmp/sur.txt", "x\ud800y\n"),
        _write_create_event(sid, "q-w", "/tmp/q.txt", "x?y\n")])
    capsysbinary.readouterr()
    assert store.cmd_cat(["cat", "/tmp/sur.txt"]) == 0
    assert capsysbinary.readouterr() == (b"x\xed\xa0\x80y\n", b"")
    assert store.cmd_cat(["cat", "/tmp/q.txt"]) == 0
    assert capsysbinary.readouterr() == (b"x?y\n", b"")


# --- ingest --detach: singleton lock + background fork (Phase 1) ---------------


def test_acquire_ingest_lock_is_exclusive_and_reusable(tmp_path, monkeypatch):
    """The lock is keyed to STORE_PATH's dir: the first acquire returns a held fd,
    a second acquire while it is open is refused (None), and closing the first fd
    releases it so a later acquire succeeds again."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    first = store._acquire_ingest_lock()
    assert first  # truthy open fd (>= 3; 0/1/2 are the std streams)
    try:
        assert store._acquire_ingest_lock() is None   # held -> refused, no block
    finally:
        os.close(first)
    again = store._acquire_ingest_lock()               # released -> acquirable again
    assert again
    os.close(again)


def test_ingest_detach_skips_without_forking_when_lock_held(tmp_path, monkeypatch):
    """When the lock is already held, `ingest --detach` returns 0 immediately
    without forking and without ingesting (idempotent skip). Exercised WITHOUT a
    real fork: os.fork is patched to flag any call, and the store is never even
    created because the skip returns before connect()."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    forked = []
    monkeypatch.setattr(os, "fork", lambda: forked.append(True))

    held = store._acquire_ingest_lock()                # pre-hold the lock
    assert held
    try:
        assert store.cmd_ingest(["ingest", "--detach"]) == 0
    finally:
        os.close(held)
    assert forked == []                                # never forked
    assert not (tmp_path / "trail.duckdb").exists()    # nothing ingested/written


def test_ingest_detach_parent_branch_closes_fd_reaps_and_returns(tmp_path, monkeypatch):
    """Lock-acquired path, parent branch simulated: os.fork returns a positive pid
    (as it does in the real parent) so no child runs; the parent closes its lock
    fd, reaps the intermediate child via waitpid (no zombie for a long-lived
    caller), returns 0, and ingests nothing itself. Closing the fd here also
    releases the lock, which a follow-up acquire confirms."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    monkeypatch.setattr(os, "fork", lambda: 4242)      # pretend to be the parent
    reaped = []
    monkeypatch.setattr(os, "waitpid", lambda pid, opts: reaped.append(pid) or (pid, 0))
    closed = []
    real_close = os.close
    monkeypatch.setattr(os, "close", lambda fd: (closed.append(fd), real_close(fd))[1])

    assert store.cmd_ingest(["ingest", "--detach"]) == 0
    assert closed                                      # parent closed its lock fd
    assert reaped == [4242]                            # intermediate child reaped
    assert not (tmp_path / "trail.duckdb").exists()    # parent does not ingest
    # The lock is free again (all fds on the description closed).
    monkeypatch.setattr(os, "close", real_close)
    fd = store._acquire_ingest_lock()
    assert fd
    os.close(fd)


def test_detach_child_argv_strips_detach_and_watch_keeps_rest():
    """The forked child re-enters cmd_ingest with --detach and --watch removed
    (so it cannot recurse and never watches) while every other flag survives."""
    assert store._detach_child_argv(
        ["ingest", "--detach", "--watch", "--compact"]
    ) == ["ingest", "--compact"]
    assert store._detach_child_argv(
        ["ingest", "--detach", "--file-history-only", "-v"]
    ) == ["ingest", "--file-history-only", "-v"]


def test_ingest_detach_returns_cleanly_without_os_fork(tmp_path, monkeypatch):
    """On a platform without os.fork (non-POSIX), `ingest --detach` returns 0 (a
    logged no-op, NOT an error: a hook runs this every turn and a nonzero exit
    would surface as a hook error each time), and never reaches lock acquisition
    (no lock file is created under STORE_PATH's dir)."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    monkeypatch.delattr(os, "fork", raising=False)
    assert store.cmd_ingest(["ingest", "--detach"]) == 0
    assert not (tmp_path / "trail-ingest.lock").exists()  # _acquire_ingest_lock never reached


# --- Writer row group size (ingest CPU phase 4b) -------------------------------
#
# A CHECKPOINT rewrites the whole row group new rows land in. DuckDB keeps the
# row group size per attached database, not in the file, so every writer open
# attaches the store with ROW_GROUP_SIZE (`_open_writer`, and compact_db's copy).


def _insert_events(con, n, tag):
    """`n` synthetic events rows of about 300 bytes of text each."""
    con.execute(
        "INSERT INTO events (event_id, session_id, ts, type, text) "
        "SELECT ? || i, 'sess-rg', TIMESTAMPTZ '2026-10-20 00:00:00+00' + to_seconds(i), "
        "'user', repeat('x', 300) || i FROM range(?) r(i)", [tag, n])


def _row_groups(con, table="events"):
    """`table`'s row groups in order, each as (rows, where its stored segments
    sit). A CHECKPOINT writes a group it rewrites to new blocks, so an unchanged
    location set means the flush left that group alone."""
    groups = {}
    for rg, path, seg, count, block, offset in con.execute(
            "SELECT row_group_id, column_path, segment_id, count, block_id, block_offset "
            f"FROM pragma_storage_info('{table}')").fetchall():
        g = groups.setdefault(rg, [0, set()])
        if path == "[0]":
            g[0] += count
        g[1].add((path, seg, block, offset))
    return [(rows, frozenset(at)) for _, (rows, at) in sorted(groups.items())]


def _assert_writes_small_groups(con):
    """ROW_GROUP_SIZE + 1 new events rows end in groups of at most
    ROW_GROUP_SIZE on this writer, and a bigger group an earlier plain open
    left (`_demote_to_schema2` opens plainly) is not written again. A plain
    open would put the new rows in its last group, up to 122,880 rows."""
    n = store.ROW_GROUP_SIZE
    con.execute("CHECKPOINT")
    had = con.execute("SELECT count(*) FROM events").fetchone()[0]
    before = _row_groups(con)
    _insert_events(con, n + 1, f"rg{had}-")
    con.execute("CHECKPOINT")
    after = _row_groups(con)
    assert sum(rows for rows, _ in after) == had + n + 1
    written = [rows for rows, at in after if (rows, at) not in before]
    assert sum(written) >= n + 1 and max(written) <= n


def test_a_flush_after_one_row_rewrites_only_the_small_last_group(tmp_path, monkeypatch):
    """On a store a plain open filled (one big group, as every store was before
    this change), the writer puts one new row in a new group and leaves the big
    one where it is; the same flush through a plain open rewrites the big
    group. The next one-row flush rewrites only that small last group."""
    import duckdb
    n = store.ROW_GROUP_SIZE
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    store.connect().close()                                    # the schema
    old = duckdb.connect(str(store.STORE_PATH))                # a writer before the change
    try:
        _insert_events(old, 3 * n + 100, "old-")
        old.execute("CHECKPOINT")
        before = _row_groups(old)
    finally:
        old.close()
    assert [rows for rows, _ in before] == [3 * n + 100]
    plain = tmp_path / "plain.duckdb"
    plain.write_bytes(store.STORE_PATH.read_bytes())

    con = store.connect()
    try:
        _insert_events(con, 1, "one-")
        con.execute("CHECKPOINT")
        after = _row_groups(con)
    finally:
        con.close()
    assert [rows for rows, _ in after] == [3 * n + 100, 1]
    assert after[0] == before[0]                               # the big group stayed put

    raw = duckdb.connect(str(plain))                           # the same flush, opened plainly
    try:
        _insert_events(raw, 1, "one-")
        raw.execute("CHECKPOINT")
        rewritten = _row_groups(raw)
    finally:
        raw.close()
    assert [rows for rows, _ in rewritten] == [3 * n + 101]
    assert rewritten[0][1] != before[0][1]                     # all 3n+101 rows written again

    con = store.connect()                                      # the next flush, a new run
    try:
        _insert_events(con, 1, "two-")
        con.execute("CHECKPOINT")
        last = _row_groups(con)
    finally:
        con.close()
    assert [rows for rows, _ in last] == [3 * n + 100, 2]
    assert last[0] == before[0]                                # still not written again


def test_every_writer_open_writes_row_groups_of_row_group_size(tmp_path, monkeypatch, capsys):
    """connect_writer (connect), compact_db's copy, and the writer connect()
    reopens after a blob move, whether the move lands, is refused, or is not
    due: each keeps new row groups at ROW_GROUP_SIZE rows. The writer also sees
    the store as its only catalog and spills where a direct open would."""
    _build_blob_store(tmp_path, monkeypatch)
    con = store.connect_writer(attempts=1)
    try:
        assert con.execute("SELECT database_name FROM duckdb_databases() "
                           "WHERE NOT internal").fetchall() == [("trail",)]
        assert con.execute("SELECT current_setting('temp_directory')").fetchone()[0] == \
            str(store.STORE_PATH) + ".tmp"
        _assert_writes_small_groups(con)
    finally:
        con.close()

    store.compact_db()
    con = store.connect(read_only=True)                        # a reader needs nothing new
    try:
        sizes = [rows for rows, _ in _row_groups(con)]
    finally:
        con.close()
    assert max(sizes) == store.ROW_GROUP_SIZE and len(sizes) == 2   # not merged into one

    _demote_to_schema2()
    con = store.connect()                                      # moves, compacts, reopens
    try:
        assert not store._duck_has_blobs(con)
        _assert_writes_small_groups(con)
    finally:
        con.close()
    assert "blobs: compacted" in capsys.readouterr().out

    _demote_to_schema2()
    monkeypatch.setattr(store, "_blob_copy_mismatches", lambda con, db, shas=None: {"f" * 64})
    for attempt in ("refused", "not due"):
        con = store.connect()
        try:
            assert store._duck_has_blobs(con)
            _assert_writes_small_groups(con)
        finally:
            con.close()
        log = capsys.readouterr().out
        assert ("blobs: moving" in log) == (attempt == "refused")
        assert "error: blobs not moved" in log


def test_no_store_open_bypasses_the_row_group_size():
    """Every DuckDB open in the store module is read-only, an in-memory driver,
    or an ATTACH that is read-only or passes ROW_GROUP_SIZE. One plain writer
    open would let the last group of each table grow back to 122,880 rows,
    and every flush would rewrite it again."""
    lines = [ln.strip() for ln in Path(store.__file__).read_text().splitlines()]
    opens = [ln for ln in lines if "duckdb.connect(" in ln]
    assert opens and all("duckdb.connect()" in ln or "read_only=True" in ln for ln in opens), opens
    attaches = [ln for ln in lines if "ATTACH '" in ln]
    assert len(attaches) == 3, attaches                        # _open_writer, compact_db src + dst
    assert all("READ_ONLY" in ln or "ROW_GROUP_SIZE" in ln for ln in attaches), attaches


@contextlib.contextmanager
def _store_held_elsewhere(script, *args, env=None, timeout=30):
    """Run `script` in another Python process and yield the first line it
    prints, which it does once it holds the store; it lets go when its stdin
    closes. Bounded both ways: a child that dies or stays silent fails the test
    with its stderr instead of hanging it, and one that does not exit after
    stdin closes is killed."""
    with subprocess.Popen([sys.executable, "-c", script, *map(str, args)],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, env=env) as proc:
        try:
            ready, _, _ = select.select([proc.stdout], [], [], timeout)
            line = proc.stdout.readline() if ready else ""
            if not line:
                proc.kill()
                _, err = proc.communicate(timeout=timeout)
                pytest.fail(f"the holder never took the store:\n{err}")
            yield line.rstrip("\n")
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def test_connect_writer_skips_a_store_another_process_holds(tmp_path, monkeypatch):
    """The writer's ATTACH meets another process's lock with the same error as a
    direct open did, so connect_writer still skips (None) instead of raising."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    store.connect().close()
    reader = ("import duckdb, sys\n"
              "con = duckdb.connect(sys.argv[1], read_only=True)\n"
              "print('held', flush=True)\n"
              "sys.stdin.read()\n")
    with _store_held_elsewhere(reader, store.STORE_PATH) as line:
        assert line == "held"
        assert store.connect_writer(attempts=1, delay=0) is None
    store.connect_writer(attempts=1).close()                  # free again once it exits


def test_a_reader_meets_the_attached_writers_lock(tmp_path, monkeypatch, capsys):
    """The reverse direction: another process holds the store through the real
    writer path (`connect()`, so the ATTACH with ROW_GROUP_SIZE holds the lock).
    A read-only open here fails with an error `is_lock_conflict` matches, and a
    CLI read turns it into the `store busy` line instead of a traceback."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    writer = ("import sys\n"
              "sys.path.insert(0, sys.argv[1])\n"
              "import claude_trail_store as store\n"
              "con = store.connect()\n"
              "print(con.execute('SELECT current_database()').fetchone()[0], flush=True)\n"
              "sys.stdin.read()\n")
    env = {**os.environ, "CLAUDE_TRAIL_DB": str(store.STORE_PATH)}
    repo = Path(store.__file__).resolve().parent
    with _store_held_elsewhere(writer, repo, env=env) as catalog:
        assert catalog == store._WRITER_CATALOG                 # the attached writer holds it
        with pytest.raises(Exception) as exc:
            store.connect(read_only=True)
        assert store.is_lock_conflict(exc.value), exc.value
        assert store.cli_main(["stats"]) == 1
        assert "store busy" in capsys.readouterr().err
    store.connect(read_only=True).close()                     # free again once it exits
