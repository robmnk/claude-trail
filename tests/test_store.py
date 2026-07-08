import json
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


def test_decode_project_maps_encoded_dir_name(tmp_path, monkeypatch):
    """A leading '-' cwd-encoded dir name decodes to a slash path label."""
    monkeypatch.setattr(store, "PROJECTS_DIR", tmp_path)
    path = tmp_path / "-home-naka-Projects-personal" / "abc.jsonl"
    assert store._decode_project(path) == "/home/naka/Projects/personal"
