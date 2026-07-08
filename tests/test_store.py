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


def test_cmd_cat_and_cmd_diff_are_phase6_stubs(tmp_path, monkeypatch, capsys):
    """cat/diff are stubs until Phase 6: they note the deferral and return 0."""
    assert store.cmd_cat(["cat", "/some/path"]) == 0
    assert "phase 6" in capsys.readouterr().out
    assert store.cmd_diff(["diff", "/some/path"]) == 0
    assert "phase 6" in capsys.readouterr().out
