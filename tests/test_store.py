import json
import os
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import claude_trail_store as store

# The 13 objects the schema bootstrap must create: 9 tables + 4 views. Both show
# up in information_schema.tables (views included), so one membership check
# covers the whole schema.
EXPECTED_OBJECTS = {
    "actions",
    "api_requests",
    "blobs",
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
    """The file-history-snapshot payload IS retained (the backfill reads it back),
    even as the neighbouring user line's payload is dropped."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(store, "PROJECTS_DIR", projects)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    sid = "sess-snap-0001"
    events = [
        _user_prompt("u1", sid, "hi", "2026-07-01T10:00:00.000Z"),
        _fh_snapshot_event("m1", {"/tmp/a.txt": {
            "backupFileName": "h1@v1", "version": 1,
            "backupTime": "2026-07-01T10:00:00.000Z"}}),
    ]
    _write_jsonl(projects / "-fake-proj" / f"{sid}.jsonl", events)
    con = store.connect()
    try:
        store.ingest_all(con)
        c = lambda sql: con.execute(sql).fetchone()[0]
        assert c("SELECT raw FROM raw_events WHERE type='file-history-snapshot'") is not None
        assert c("SELECT count(*) FROM raw_events WHERE type<>'file-history-snapshot' AND raw IS NOT NULL") == 0
        assert c("SELECT count(*) FROM raw_events") == 2                      # both index rows kept
    finally:
        con.close()


def test_prune_raw_events_clears_payload_keeps_snapshots_and_malformed(tmp_path, monkeypatch):
    """prune_raw_events nulls only the non-snapshot payloads: file-history-snapshot
    (read back) and malformed (type NULL) rows keep theirs, and it is idempotent."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    con = store.connect()
    try:
        con.executemany(
            "INSERT INTO raw_events(uuid,session_id,ts,type,transcript_path,raw) VALUES (?,?,?,?,?,?)",
            [("u1", "s", None, "user", "/p", '{"big":"payload"}'),
             ("snap", "s", None, "file-history-snapshot", "/p", '{"snapshot":1}'),
             ("bad", "s", None, None, "/p", "not-json")])          # malformed: type NULL
        cleared, nbytes = store.prune_raw_events(con)
        assert cleared == 1 and nbytes > 0
        val = lambda u: con.execute("SELECT raw FROM raw_events WHERE uuid=?", [u]).fetchone()[0]
        assert val("u1") is None            # non-snapshot payload cleared
        assert val("snap") is not None      # snapshot kept
        assert val("bad") is not None       # malformed kept
        assert con.execute("SELECT count(*) FROM raw_events").fetchone()[0] == 3  # index intact
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

_LONG_TURN_TEXT = "long turn " + "x" * 500          # 510 chars: preview truncates


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
                 _turn_transcript_events(sid))
    _write_jsonl(
        projects / "-fake-proj" / sid / "subagents" / "agent-ag1.jsonl",
        # The subagent's own user turn is its task prompt: it must NOT become a
        # row of its own (only main-thread turns do), and its tool calls carry the
        # PARENT session_id, so they fall inside the parent turn's window.
        [_user_prompt("t-s-u1", sid, "task prompt for ag1",
                      "2026-06-30T02:00:02.500Z"),
         _tool_use_line("t-s-a1", sid, "2026-06-30T02:00:03.000Z", "req-S1",
                        [{"type": "tool_use", "id": "toolu-sub-a", "name": "Read",
                          "input": {"file_path": "/tmp/read-me.py"}},
                         {"type": "tool_use", "id": "toolu-sub-b", "name": "Grep",
                          "input": {"pattern": "def build_"}}], 7)],
    )
    _write_jsonl(projects / "-fake-proj" / f"{sid2}.jsonl", [
        _user_prompt("o-u1", sid2, "other session ask", "2026-06-30T03:00:00.000Z"),
        _tool_use_line("o-a1", sid2, "2026-06-30T03:00:01.000Z", "req-O1",
                       [{"type": "tool_use", "id": "toolu-o1", "name": "Bash",
                         "input": {"command": "echo other"}}], 5),
    ])
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
    # sessions.project is decoded from the transcript dir; cwd is not populated by
    # the ingester today, so the key is present and None (the TUI falls back).
    assert _turn(rows, "first ask")["project"] == "/fake/proj"
    assert _turn(rows, "first ask")["cwd"] is None


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


def test_put_blob_dedups_identical_content(tmp_path, monkeypatch):
    """Two identical contents content-address to one blob row (sha dedup); None
    stores nothing; a distinct content adds exactly one more."""
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "trail.duckdb")
    con = store.connect()
    try:
        a = store._put_blob(con, "same bytes")
        b = store._put_blob(con, "same bytes")
        assert a == b
        assert con.execute("SELECT count(*) FROM blobs").fetchone()[0] == 1
        assert store._put_blob(con, None) is None      # None -> no row, no sha
        assert con.execute("SELECT count(*) FROM blobs").fetchone()[0] == 1
        store._put_blob(con, "other bytes")
        assert con.execute("SELECT count(*) FROM blobs").fetchone()[0] == 2
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
        post = con.execute("SELECT content FROM blobs WHERE sha=?",
                           [fv["post_sha"]]).fetchone()[0]
        assert post == expected_post
        pre = con.execute("SELECT content FROM blobs WHERE sha=?",
                          [fv["pre_sha"]]).fetchone()[0]
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
        assert count("SELECT count(*) FROM blobs") > 0               # real bytes stored
        # the edit path is present with a non-null post_sha that round-trips
        post_sha = con.execute(
            "SELECT post_sha FROM file_versions WHERE path=? AND pre_sha IS NOT NULL",
            [path]).fetchone()[0]
        assert post_sha is not None
        content = con.execute("SELECT content FROM blobs WHERE sha=?",
                             [post_sha]).fetchone()[0]
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
        assert con.execute("SELECT count(*) FROM blobs").fetchone()[0] == 0
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
        assert count("SELECT count(*) FROM blobs") == 2                 # v1 + v2 preserved
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
        assert con.execute("SELECT count(*) FROM blobs").fetchone()[0] == 1              # preserved
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
                    con.execute("SELECT count(*) FROM blobs").fetchone()[0])
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
    stored schema version becomes '2' (the ON CONFLICT DO UPDATE fix)."""
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
        assert con.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0] == "2"
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
        assert con.execute("SELECT count(*) FROM blobs WHERE sha=?", [empty_sha]).fetchone()[0] == 1
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
        assert con.execute("SELECT count(*) FROM blobs").fetchone()[0] == 0
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
        assert con.execute("SELECT count(*) FROM blobs").fetchone()[0] == 2   # bytes preserved
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
        assert con.execute("SELECT count(*) FROM blobs").fetchone()[0] == 1             # preserved
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
