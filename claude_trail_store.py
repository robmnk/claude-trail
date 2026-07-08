"""DuckDB-backed audit trail store for Claude Code sessions.

Ingests transcripts (~/.claude/projects/**/*.jsonl) and file snapshots into a
single local DuckDB file; provides substring/regex search and file
reconstruction. Single-writer ingester, read-only readers.
"""
import hashlib
import json
import os
import time
from pathlib import Path

STORE_PATH = Path(os.environ.get("CLAUDE_TRAIL_DB", Path.home() / ".claude" / "trail.duckdb"))
SCHEMA_VERSION = "1"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT);  -- {'version':'1'}

-- Lossless source of truth: one row per transcript line, every event type, verbatim.
-- events / file_versions / sessions below are all derived projections of this.
CREATE TABLE IF NOT EXISTS raw_events(
  uuid            TEXT PRIMARY KEY,   -- transcript `uuid`, or sha256(line) when absent/malformed
  session_id      TEXT,
  ts              TIMESTAMPTZ,
  type            TEXT,               -- every event type, kept as-is (system, mode, attachment, ...)
  transcript_path TEXT,
  raw             TEXT                -- the complete original JSONL line, byte-for-byte
);                                    -- TEXT (not JSON) so malformed lines persist too; query via json_extract(raw,...) / raw::JSON
CREATE INDEX IF NOT EXISTS raw_session_ts ON raw_events(session_id, ts);
CREATE INDEX IF NOT EXISTS raw_type ON raw_events(type);

CREATE TABLE IF NOT EXISTS sessions(
  session_id  TEXT PRIMARY KEY,
  project     TEXT,            -- decoded from transcript dir / event cwd
  cwd         TEXT,
  git_branch  TEXT,
  title       TEXT,            -- from ai-title/custom-title events if present
  first_ts    TIMESTAMPTZ,
  last_ts     TIMESTAMPTZ,
  event_count BIGINT
);

CREATE TABLE IF NOT EXISTS events(
  event_id    TEXT PRIMARY KEY,   -- stable: tool_use id, or "<uuid>:<suffix>"
  session_id  TEXT,
  parent_uuid TEXT,
  ts          TIMESTAMPTZ,
  type        TEXT,               -- user | assistant | tool_use | tool_result
  role        TEXT,
  tool_name   TEXT,               -- Bash, Edit, ... (NULL for messages)
  tool_use_id TEXT,               -- links tool_use <-> tool_result
  request_id  TEXT,               -- API request that emitted this row (assistant lines only)
  agent_id    TEXT,               -- subagent id (subagents/agent-<id>.jsonl); NULL = main thread
  text        TEXT,               -- message text or Bash command or repr field
  tool_input  JSON,
  stdout      TEXT,
  stderr      TEXT,
  is_error    BOOLEAN
);
CREATE INDEX IF NOT EXISTS events_session_ts ON events(session_id, ts);
CREATE INDEX IF NOT EXISTS events_tooluse ON events(tool_use_id);

CREATE TABLE IF NOT EXISTS api_requests(   -- one row per API request (token accounting)
  request_id            TEXT PRIMARY KEY,  -- requestId; fallback message.id, else line uuid
  message_id            TEXT,
  session_id            TEXT,
  agent_id              TEXT,              -- NULL = main thread
  ts                    TIMESTAMPTZ,
  model                 TEXT,
  input_tokens          BIGINT,
  output_tokens         BIGINT,
  cache_read_tokens     BIGINT,
  cache_creation_tokens BIGINT,
  service_tier          TEXT
);
CREATE INDEX IF NOT EXISTS api_session_ts ON api_requests(session_id, ts);

CREATE TABLE IF NOT EXISTS file_versions(
  event_id   TEXT,
  session_id TEXT,
  ts         TIMESTAMPTZ,
  path       TEXT,
  pre_sha    TEXT,
  post_sha   TEXT,
  patch      TEXT               -- unified diff (from structuredPatch or difflib)
);
CREATE INDEX IF NOT EXISTS fv_path ON file_versions(path, ts);

CREATE TABLE IF NOT EXISTS blobs(
  sha     TEXT PRIMARY KEY,     -- sha256 hex of content
  size    BIGINT,
  content TEXT                  -- full file content; DuckDB compresses on disk
);

CREATE TABLE IF NOT EXISTS ingest_state(
  transcript_path TEXT PRIMARY KEY,
  last_offset     BIGINT,
  last_mtime      DOUBLE
);

CREATE VIEW IF NOT EXISTS actions AS  -- one row per tool call, result joined in
SELECT u.event_id, u.ts, u.session_id, u.agent_id, u.tool_name, u.text AS command,
       u.tool_input, r.stdout, r.stderr, r.is_error
FROM events u
LEFT JOIN events r ON r.tool_use_id = u.tool_use_id AND r.type='tool_result'
WHERE u.type='tool_use';

CREATE VIEW IF NOT EXISTS session_tokens AS  -- exact per-session totals
SELECT session_id, count(*) AS requests,
       sum(output_tokens)         AS output_tokens,
       sum(input_tokens)          AS input_tokens,
       sum(cache_read_tokens)     AS cache_read_tokens,
       sum(cache_creation_tokens) AS cache_creation_tokens,
       sum(CASE WHEN agent_id IS NOT NULL THEN output_tokens ELSE 0 END) AS subagent_output_tokens,
       min(ts) AS first_ts, max(ts) AS last_ts
FROM api_requests GROUP BY 1;

CREATE VIEW IF NOT EXISTS command_tokens AS  -- generation cost per tool call
SELECT e.event_id, e.ts, e.session_id, e.agent_id, e.tool_name, e.text AS command,
       r.model, r.output_tokens,
       count(*) OVER (PARTITION BY e.request_id)                    AS calls_in_request,
       r.output_tokens / count(*) OVER (PARTITION BY e.request_id)  AS output_tokens_share
FROM events e JOIN api_requests r ON r.request_id = e.request_id
WHERE e.type = 'tool_use';

CREATE VIEW IF NOT EXISTS request_context AS  -- context growth ~= read-back cost of prior results
SELECT session_id, agent_id, request_id, ts, model, output_tokens,
       input_tokens + cache_read_tokens + cache_creation_tokens AS context_tokens,
       input_tokens + cache_read_tokens + cache_creation_tokens
         - lag(input_tokens + cache_read_tokens + cache_creation_tokens)
           OVER (PARTITION BY session_id, agent_id ORDER BY ts, request_id) AS context_growth
FROM api_requests;
"""


def connect(read_only: bool = False):
    import duckdb  # lazy: keeps the TUI/hook free of the duckdb import
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(STORE_PATH), read_only=read_only)
    if not read_only:
        con.execute(SCHEMA_SQL)
        con.execute(
            "INSERT INTO schema_meta VALUES ('version', ?) ON CONFLICT DO NOTHING",
            [SCHEMA_VERSION],
        )
    return con


_COMMAND_FIELD = {  # representative `text` per tool when there is no command
    "Bash": "command",
    "Read": "file_path", "Edit": "file_path", "Write": "file_path",
    "NotebookEdit": "notebook_path", "Glob": "pattern", "Grep": "pattern",
}


def _repr_text(tool_name, tool_input):
    if not isinstance(tool_input, dict):
        return None
    field = _COMMAND_FIELD.get(tool_name)
    return tool_input.get(field) if field else None


def _row(event_id, session_id, parent, ts, etype, role, tool_name=None,
         tool_use_id=None, text=None, tool_input=None,
         stdout=None, stderr=None, is_error=None, request_id=None, agent_id=None):
    return {
        "event_id": event_id, "session_id": session_id, "parent_uuid": parent,
        "ts": ts, "type": etype, "role": role, "tool_name": tool_name,
        "tool_use_id": tool_use_id, "request_id": request_id, "agent_id": agent_id,
        "text": text,
        "tool_input": json.dumps(tool_input) if tool_input is not None else None,
        "stdout": stdout, "stderr": stderr, "is_error": is_error,
    }


def _result_fields(tur, block):
    """Pull stdout/stderr/is_error from toolUseResult (dict) and/or the
    tool_result content block, tolerating both Bash and file-tool shapes."""
    stdout = stderr = None
    is_error = bool(block.get("is_error")) if isinstance(block, dict) else None
    if isinstance(tur, dict):
        stdout = tur.get("stdout")
        stderr = tur.get("stderr")
        if tur.get("interrupted"):
            is_error = True
    if stdout is None and isinstance(block, dict):
        c = block.get("content")
        stdout = c if isinstance(c, str) else (json.dumps(c) if c is not None else None)
    return stdout, stderr, is_error


def _text_from_blocks(content):
    parts = [b.get("text", "") for b in content
             if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if p) or None


def parse_event(ev, agent_id=None):
    """Transcript event dict -> list of `events` row dicts. Never raises.

    agent_id comes from the transcript path (subagents/agent-<id>.jsonl), not
    the event itself, so the caller passes it in; every row is stamped with it.
    """
    if not isinstance(ev, dict):
        return []
    rows, etype = [], ev.get("type")
    sid = ev.get("sessionId", "")
    ts = ev.get("timestamp")
    uuid = ev.get("uuid") or ""
    parent = ev.get("parentUuid")
    msg = ev.get("message") or {}
    if etype == "assistant":
        rid = ev.get("requestId")
        for i, b in enumerate(msg.get("content") or []):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                rows.append(_row(f"{uuid}:t{i}", sid, parent, ts, "assistant",
                                 "assistant", text=b.get("text"), request_id=rid))
            elif b.get("type") == "tool_use":
                tid = b.get("id")
                rows.append(_row(tid or f"{uuid}:u{i}", sid, parent, ts, "tool_use",
                                 "assistant", tool_name=b.get("name"), tool_use_id=tid,
                                 text=_repr_text(b.get("name"), b.get("input")),
                                 tool_input=b.get("input"), request_id=rid))
    elif etype == "user":
        content = msg.get("content")
        tur = ev.get("toolUseResult")
        if isinstance(content, list):
            results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
            for b in results:
                tid = b.get("tool_use_id")
                out, err, ierr = _result_fields(tur, b)
                rows.append(_row(f"{uuid}:r:{tid}", sid, parent, ts, "tool_result",
                                 "tool", tool_use_id=tid, stdout=out, stderr=err, is_error=ierr))
            if not results:
                txt = _text_from_blocks(content)
                if txt:
                    rows.append(_row(uuid, sid, parent, ts, "user", "user", text=txt))
        elif isinstance(content, str) and content.strip():
            rows.append(_row(uuid, sid, parent, ts, "user", "user", text=content))
    for r in rows:
        r["agent_id"] = agent_id
    return rows


def parse_usage(ev, agent_id=None):
    """Assistant event -> one `api_requests` row dict, or None. Never raises.

    One API request is written as several JSONL lines (one per content block),
    each repeating the same requestId with identical usage; the insert dedupes
    on request_id. Synthetic messages (model == "<synthetic>") carry all-zero
    usage for a request that never hit the API, so they are skipped.
    """
    if not isinstance(ev, dict) or ev.get("type") != "assistant":
        return None
    msg = ev.get("message") or {}
    u = msg.get("usage")
    if not isinstance(u, dict) or msg.get("model") == "<synthetic>":
        return None
    return {
        "request_id": ev.get("requestId") or msg.get("id") or ev.get("uuid"),
        "message_id": msg.get("id"),
        "session_id": ev.get("sessionId", ""),
        "agent_id": agent_id,
        "ts": ev.get("timestamp"),
        "model": msg.get("model"),
        "input_tokens": u.get("input_tokens") or 0,
        "output_tokens": u.get("output_tokens") or 0,
        "cache_read_tokens": u.get("cache_read_input_tokens") or 0,
        "cache_creation_tokens": u.get("cache_creation_input_tokens") or 0,
        "service_tier": u.get("service_tier"),
    }


# --- Incremental ingester (Task 3) --------------------------------------------

PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _decode_project(transcript_path):
    # dir names encode cwd: "-home-naka-Projects-personal" -> project label.
    # Use the first component under PROJECTS_DIR: subagent transcripts live two
    # levels deeper (<project>/<sid>/subagents/*.jsonl), so parent.name is wrong there.
    try:
        name = transcript_path.relative_to(PROJECTS_DIR).parts[0]
    except ValueError:
        name = transcript_path.parent.name
    return name.replace("-", "/", 1).replace("-", "/") if name.startswith("-") else name


def _sha(text: str) -> str:            # also reused for blobs in Task 5
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _insert_raw(con, raw_rows):
    """Persist original transcript lines verbatim, idempotent on uuid."""
    raw_rows = list({r[0]: r for r in raw_rows}.values())  # ON CONFLICT skips existing rows, not dups within one insert
    con.execute("CREATE TEMP TABLE IF NOT EXISTS _raw_stage AS SELECT * FROM raw_events WHERE 0=1")
    con.execute("DELETE FROM _raw_stage")
    con.executemany(
        "INSERT INTO _raw_stage(uuid, session_id, ts, type, transcript_path, raw) VALUES (?,?,?,?,?,?)",
        raw_rows)
    con.execute("INSERT INTO raw_events SELECT * FROM _raw_stage ON CONFLICT (uuid) DO NOTHING")
    con.execute("DELETE FROM _raw_stage")


def ingest_transcript(con, path):
    """Read new lines from `path` since last_offset; insert events; advance offset.
    Returns number of events inserted."""
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    row = con.execute("SELECT last_offset FROM ingest_state WHERE transcript_path=?",
                      [str(path)]).fetchone()
    last = row[0] if row else 0
    if size < last:            # truncated/rewritten -> rescan
        last = 0
    if size == last:
        return 0
    with open(path, "rb") as f:
        f.seek(last)
        data = f.read(size - last)
    nl = data.rfind(b"\n")
    if nl < 0:
        return 0               # no complete line yet
    complete, new_off = data[:nl + 1], last + nl + 1
    # subagent transcripts: <project>/<sid>/subagents/agent-<id>.jsonl
    agent_id = path.stem.removeprefix("agent-") if path.parent.name == "subagents" else None
    raw_rows, rows, ureqs = [], [], []
    for line in complete.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            # persist the raw line anyway (lossless); key by content hash
            raw_rows.append((_sha(line), None, None, None, str(path), line))
            continue
        uid = ev.get("uuid") or _sha(line)
        raw_rows.append((uid, ev.get("sessionId", ""), ev.get("timestamp"),
                         ev.get("type"), str(path), line))
        rows.extend(parse_event(ev, agent_id))  # derived events; may be empty for a line
        u = parse_usage(ev, agent_id)           # one api_requests row per assistant line
        if u:
            ureqs.append(u)
    if raw_rows:
        _insert_raw(con, raw_rows)            # verbatim, always
        _upsert_session(con, path, raw_rows)
    if rows:
        _insert_events(con, rows)
    if ureqs:
        _insert_requests(con, ureqs)
    con.execute(
        "INSERT INTO ingest_state VALUES (?,?,?) "
        "ON CONFLICT (transcript_path) DO UPDATE SET last_offset=excluded.last_offset, last_mtime=excluded.last_mtime",
        [str(path), new_off, size])
    return len(raw_rows)                       # count of transcript lines ingested


_EVENT_COLS = ["event_id","session_id","parent_uuid","ts","type","role",
               "tool_name","tool_use_id","request_id","agent_id",
               "text","tool_input","stdout","stderr","is_error"]


def _insert_events(con, rows):
    con.execute("CREATE TEMP TABLE IF NOT EXISTS _stage AS SELECT * FROM events WHERE 0=1")
    con.execute("DELETE FROM _stage")
    # executemany into a staging temp table, then INSERT..ON CONFLICT (Appender if too slow)
    con.executemany(
        f"INSERT INTO _stage ({','.join(_EVENT_COLS)}) VALUES ({','.join(['?']*len(_EVENT_COLS))})",
        [[r[c] for c in _EVENT_COLS] for r in rows])
    con.execute("INSERT INTO events SELECT * FROM _stage ON CONFLICT (event_id) DO NOTHING")
    con.execute("DELETE FROM _stage")


_REQ_COLS = ["request_id","message_id","session_id","agent_id","ts","model",
             "input_tokens","output_tokens","cache_read_tokens",
             "cache_creation_tokens","service_tier"]


def _insert_requests(con, rows):
    # one request arrives as several content-block lines with identical usage:
    # collapse within the batch here; ON CONFLICT handles cross-batch repeats
    rows = list({r["request_id"]: r for r in rows}.values())
    con.execute("CREATE TEMP TABLE IF NOT EXISTS _req_stage AS SELECT * FROM api_requests WHERE 0=1")
    con.execute("DELETE FROM _req_stage")
    con.executemany(
        f"INSERT INTO _req_stage ({','.join(_REQ_COLS)}) VALUES ({','.join(['?']*len(_REQ_COLS))})",
        [[r[c] for c in _REQ_COLS] for r in rows])
    con.execute("INSERT INTO api_requests SELECT * FROM _req_stage ON CONFLICT (request_id) DO NOTHING")
    con.execute("DELETE FROM _req_stage")


def _upsert_session(con, path, raw_rows):
    # raw_rows are tuples: (uuid, session_id, ts, type, transcript_path, raw)
    sid = next((r[1] for r in raw_rows if r[1]), None)
    if not sid:
        return
    ts_vals = [r[2] for r in raw_rows if r[2]]
    con.execute(
        "INSERT INTO sessions(session_id, project, first_ts, last_ts, event_count) "
        "VALUES (?,?,?,?,?) ON CONFLICT (session_id) DO UPDATE SET "
        "last_ts=greatest(sessions.last_ts, excluded.last_ts), "
        "event_count=sessions.event_count + excluded.event_count",
        [sid, _decode_project(path), min(ts_vals, default=None),
         max(ts_vals, default=None), len(raw_rows)])  # event_count = transcript lines


def ingest_all(con):
    total = 0
    paths = sorted(PROJECTS_DIR.glob("*/*.jsonl")) + sorted(PROJECTS_DIR.glob("*/*/subagents/*.jsonl"))
    for path in paths:
        total += ingest_transcript(con, path)
    return total


def cmd_ingest(argv):
    watch = "--watch" in argv
    interval = 5.0
    con = connect()
    try:
        while True:
            n = ingest_all(con)
            print(f"ingested {n} events")
            if not watch:
                break
            time.sleep(interval)
    finally:
        con.close()
    return 0


def cli_main(argv):
    # Task 3 dispatch: only `ingest` exists yet. search/stats/tokens/cat/diff
    # land in later phases; this keeps `claude-trail ingest` runnable without
    # referencing functions that do not exist yet.
    cmd = argv[0]
    if cmd == "ingest":
        return cmd_ingest(argv)
    print(f"claude-trail: subcommand '{cmd}' is not available yet")
    return 2
