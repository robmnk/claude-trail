"""DuckDB-backed audit trail store for Claude Code sessions.

Ingests transcripts (~/.claude/projects/**/*.jsonl) and file snapshots into a
single local DuckDB file; provides substring/regex search and file
reconstruction. Single-writer ingester, read-only readers.
"""
import difflib
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

STORE_PATH = Path(os.environ.get("CLAUDE_TRAIL_DB", Path.home() / ".claude" / "trail.duckdb"))
SCHEMA_VERSION = "2"   # 1 -> 2: file_versions.source column + file_history table (Phase 8)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT);  -- {'version':'2'}

-- Lossless source of truth: one row per transcript line, every event type, verbatim.
-- events / file_versions / sessions below are all derived projections of this.
CREATE TABLE IF NOT EXISTS raw_events(
  uuid            TEXT PRIMARY KEY,   -- transcript `uuid`, or sha256(line) when absent/malformed
  session_id      TEXT,
  ts              TIMESTAMPTZ,
  type            TEXT,               -- every event type, kept as-is (system, mode, attachment, ...)
  transcript_path TEXT,
  raw             TEXT                -- verbatim JSONL line, kept only for file-history-snapshot + malformed lines; NULL otherwise (see ingest_transcript / prune_raw_events)
);                                    -- TEXT (not JSON) so malformed lines persist too; query via json_extract(raw,...) / raw::JSON
CREATE INDEX IF NOT EXISTS raw_session_ts ON raw_events(session_id, ts);
CREATE INDEX IF NOT EXISTS raw_type ON raw_events(type);

CREATE TABLE IF NOT EXISTS sessions(
  session_id  TEXT PRIMARY KEY,
  project     TEXT,            -- LOSSY label decoded from the transcript DIR NAME only (every `-` becomes `/`, so `claude-trail` -> `claude/trail`); never an event cwd. See _decode_project
  cwd         TEXT,            -- the session's exact LAUNCH dir, verbatim from the first transcript line carrying one; never follows a later `cd`. NULL = transcript gone from disk, or present but carrying no cwd in its head. See _upsert_session / backfill_session_cwd
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
  patch      TEXT,              -- structuredPatch hunks as JSON, else a difflib
                                --   unified diff (only when pre+post both known)
  source     TEXT DEFAULT 'transcript'  -- provenance: 'transcript' | 'file-history' (B2; must be stamped, see _FV_COLS)
);
CREATE INDEX IF NOT EXISTS fv_path ON file_versions(path, ts);

CREATE TABLE IF NOT EXISTS blobs(
  sha     TEXT PRIMARY KEY,     -- sha256 hex of content
  size    BIGINT,
  content TEXT                  -- full file content; DuckDB compresses on disk
);

-- One row per ~/.claude/file-history/<session_id>/<pathhash>@vN snapshot (Phase 8).
-- The bytes are the store's one irreversible-loss input (unlike every raw_events
-- projection, file-history is cache-like and may be pruned), so they are preserved
-- as blobs here even when `path` is not yet resolvable; resolved rows also surface
-- through cat via synthetic source='file-history' file_versions rows.
CREATE TABLE IF NOT EXISTS file_history(
  session_id  TEXT,
  pathhash    TEXT,            -- sha256(abs_path)[:16], the on-disk dir key
  version     INTEGER,         -- N from @vN / trackedFileBackups.version
  path        TEXT,            -- resolved absolute path, or NULL if not yet known
  sha         TEXT,            -- blob sha of the snapshot content (NULL if binary-skipped)
  snapshot_ts TIMESTAMPTZ,     -- trackedFileBackups.backupTime (ISO); mtime fallback for orphans
  note        TEXT,            -- e.g. 'binary-skipped'; NULL normally
  PRIMARY KEY (session_id, pathhash, version)   -- idempotent re-ingest
);
CREATE INDEX IF NOT EXISTS fh_pathhash ON file_history(pathhash);

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
        # Migrate a 0.6.0 (schema 1) DB in place. SCHEMA_SQL's CREATE ... IF NOT
        # EXISTS above adds file_history, but leaves a pre-existing file_versions
        # untouched, so the new `source` column is added by ADD COLUMN IF NOT
        # EXISTS (idempotent; backfills every existing row with the DEFAULT,
        # correctly labelling all pre-phase-8 rows 'transcript'). The version seed
        # must DO UPDATE, not DO NOTHING: an existing ('version','1') row would
        # otherwise pin the DB at '1' forever.
        con.execute("ALTER TABLE file_versions ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'transcript'")
        con.execute(
            "INSERT INTO schema_meta VALUES ('version', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [SCHEMA_VERSION],
        )
    return con


def is_lock_conflict(exc):
    """True when `exc` is DuckDB refusing the store because another process holds
    it. DuckDB's file lock is exclusive in BOTH directions: a writer cannot open
    a reader-held store any more than the reverse, so at a short ingest cadence
    an interactive TUI read and the timer's ingest collide routinely. It is a
    transient, expected condition, not a fault - callers skip and retry rather
    than crash. Matched on the message because duckdb raises a plain IOException
    for it."""
    msg = str(exc)
    return "Could not set lock" in msg or "Conflicting lock" in msg


def connect_writer(attempts=3, delay=2.0):
    """`connect()` for the single writer, tolerating a reader holding the store.

    Returns None instead of raising when every attempt hits a lock conflict: the
    ingest is idempotent and runs on a timer, so skipping this cycle costs at
    most one interval, while a traceback would look like a broken store in the
    log. Any other error still propagates."""
    for attempt in range(1, attempts + 1):
        try:
            return connect()
        except Exception as e:
            if not is_lock_conflict(e):
                raise
            if attempt == attempts:
                return None
            time.sleep(delay)
    return None


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
    # LOSSY, and unfixably so: the encoding spends `-` on both the separator and
    # any `-` in a directory's own name, so `claude-trail` comes back as
    # `claude/trail` - a path that does not exist. `sessions.cwd` (written from the
    # transcript's own `cwd`, see `_first_event_cwd`) is the exact answer; this
    # stays the fallback for rows that have no cwd rather than trying to guess
    # which dashes were separators.
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
        st = path.stat()
    except OSError:
        return 0
    size = st.st_size
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
    raw_rows, rows, ureqs, fvs = [], [], [], []
    cwd = None   # the session's launch dir, from the first main-thread line carrying one
    full_read = last == 0   # this chunk starts at byte 0, so its first cwd IS the file's
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
        typ = ev.get("type")
        # Keep the lightweight index row for every line, but store the verbatim
        # `raw` payload only for what the store still reads back: file-history
        # -snapshot lines (via _snapshot_records). Every other line's payload is
        # a redundant third copy (the transcript on disk + the `events`
        # projection already hold it) and nothing reads it, so drop it to keep
        # the store lean. Malformed lines (except branch above) keep their raw.
        raw_val = line if typ == "file-history-snapshot" else None
        raw_rows.append((uid, ev.get("sessionId", ""), ev.get("timestamp"),
                         typ, str(path), raw_val))
        # Main transcripts only: a subagent runs under the PARENT session id but
        # in its own cwd (measured: a scratchpad under /tmp for 2 of 5 sampled
        # subagent transcripts), so taking its cwd would mislabel the parent.
        if cwd is None and agent_id is None:
            cwd = _first_event_cwd(ev)
        rows.extend(parse_event(ev, agent_id))  # derived events; may be empty for a line
        u = parse_usage(ev, agent_id)           # one api_requests row per assistant line
        if u:
            ureqs.append(u)
        if ev.get("toolUseResult") is not None:  # edit/write results -> file version + blobs
            fv = file_version_from_event(con, ev)
            if fv:
                fvs.append(fv)
    if raw_rows:
        _insert_raw(con, raw_rows)            # index rows always; payload only for snapshots/malformed
        # `cwd` only on a full read. An incremental chunk starts mid-file, so its
        # first cwd is whatever the run had `cd`-ed to by then; writing that would
        # freeze a scratchpad into the column (coalesce never replaces a value,
        # and `backfill_session_cwd` skips a row that is no longer NULL). A row
        # left NULL here is the backfill's job, and its read is the file's HEAD.
        _upsert_session(con, path, raw_rows,      # sees every line (event_count stays exact)
                        cwd if full_read else None)
    if rows:
        _insert_events(con, rows)
    if ureqs:
        _insert_requests(con, ureqs)
    if fvs:
        _insert_file_versions(con, fvs)
    con.execute(
        "INSERT INTO ingest_state VALUES (?,?,?) "
        "ON CONFLICT (transcript_path) DO UPDATE SET last_offset=excluded.last_offset, last_mtime=excluded.last_mtime",
        [str(path), new_off, st.st_mtime])  # last_mtime: real mtime; offset check uses size, not this
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


def _first_event_cwd(ev):
    """The top-level `cwd` of one transcript line, or None when it carries none."""
    cwd = ev.get("cwd") if isinstance(ev, dict) else None
    return cwd if isinstance(cwd, str) and cwd else None


def _upsert_session(con, path, raw_rows, cwd=None):
    # raw_rows are tuples: (uuid, session_id, ts, type, transcript_path, raw)
    #
    # `cwd` is the FIRST cwd of a main transcript (the launch dir): callers pass it
    # only for a read that started at byte 0 (see `ingest_transcript`), so it can
    # never be a mid-file value. It is also written once:
    # `coalesce(sessions.cwd, excluded.cwd)` never replaces a value
    # already there. Latest-wins would be wrong AND unstable - Claude Code rewrites
    # the top-level cwd after a `cd`, and 12 of the newest 40 transcripts on this
    # machine carry more than one (up to 27 distinct values, including
    # `/tmp/.../scratchpad`), so the Project column would drift away from the
    # project mid-session and flip on every incremental sweep. First-wins also
    # makes the sweep idempotent: re-ingesting the same session cannot change it.
    sid = next((r[1] for r in raw_rows if r[1]), None)
    if not sid:
        return
    ts_vals = [r[2] for r in raw_rows if r[2]]
    con.execute(
        "INSERT INTO sessions(session_id, project, cwd, first_ts, last_ts, event_count) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT (session_id) DO UPDATE SET "
        "cwd=coalesce(sessions.cwd, excluded.cwd), "
        "last_ts=greatest(sessions.last_ts, excluded.last_ts), "
        "event_count=sessions.event_count + excluded.event_count",
        [sid, _decode_project(path), cwd, min(ts_vals, default=None),
         max(ts_vals, default=None), len(raw_rows)])  # event_count = transcript lines


def _now_hms():
    return datetime.now().strftime("%H:%M:%S")


def _log(msg):
    """Timestamped ingest progress line (stdout, flushed for live tailing)."""
    print(f"[{_now_hms()}] {msg}", flush=True)


def _rel_transcript(path):
    """Short label for a transcript in verbose logs: `<parent>/<name>`."""
    return "/".join(path.parts[-2:])


# Tables ingest mutates, for the per-sweep stored-delta summary (present only).
_INGEST_COUNT_TABLES = ["raw_events", "events", "api_requests",
                        "file_versions", "blobs", "file_history"]


def _table_counts(con):
    """Row counts for the ingest-touched tables that currently exist."""
    present = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='main'").fetchall()}
    return {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in _INGEST_COUNT_TABLES if t in present}


# A CHECKPOINT on a multi-GB store takes seconds (~3.5s at 1 GB) and runs while
# the write lock is held, which no reader can share: a read-only open fails
# outright against a held lock, so every second of it is a second the TUI's
# Tokens / prompt / database reads degrade to their "pending ingest" hint. At a
# 30s ingest cadence that window dominates. So flush on a schedule instead of
# after every sweep: the WAL is durable (an unflushed store replays it on the
# next open) and DuckDB auto-checkpoints on its own WAL-size threshold, so a
# skipped flush costs a slightly larger `.wal`, never data.
CHECKPOINT_MIN_INTERVAL = 300.0  # seconds between ingest-driven flushes


def _checkpoint_stamp():
    """Marker whose mtime is the last ingest-driven CHECKPOINT (per store)."""
    return Path(str(STORE_PATH) + ".checkpoint-stamp")


def _checkpoint_due(stored_anything, force=False):
    """`(should_flush, reason)` for the end-of-run CHECKPOINT.

    `force` covers the two runs that must land on a flushed store: `--compact`
    (a full rewrite follows) and `--watch` (its one flush is at shutdown). A
    sweep that stored nothing has nothing to flush, so it never pays the lock.
    """
    if force:
        return True, "forced"
    if not stored_anything:
        return False, "nothing stored"
    try:
        age = time.time() - _checkpoint_stamp().stat().st_mtime
    except OSError:
        return True, "no flush recorded yet"
    if age >= CHECKPOINT_MIN_INTERVAL:
        return True, f"{age:.0f}s since last flush"
    return False, f"last flush {age:.0f}s ago (min {CHECKPOINT_MIN_INTERVAL:.0f}s)"


def _store_size():
    """On-disk size of the store file plus its WAL (0 for parts not present)."""
    total = 0
    for p in (STORE_PATH, Path(str(STORE_PATH) + ".wal")):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def _main_transcript_paths():
    """Main session transcripts only: `<project>/<sid>.jsonl`, no subagents."""
    return sorted(PROJECTS_DIR.glob("*/*.jsonl"))


def _transcript_paths():
    """All transcript files: main `<sid>.jsonl` plus subagent `subagents/*.jsonl`."""
    return (_main_transcript_paths()
            + sorted(PROJECTS_DIR.glob("*/*/subagents/*.jsonl")))


# How far into a transcript the backfill looks for the session's launch cwd.
# Measured over the 127 transcripts on this machine: the first cwd-bearing line
# is line 3..22 (the header block), and the 10 files with no cwd at all are
# sub-300-byte stubs. 50 covers that with room to spare while bounding the read
# of a transcript that can be hundreds of MB to its head.
SESSION_CWD_SCAN_LINES = 50


def _head_cwd(path):
    """`(session_id, cwd)` from the first head line of `path` that carries a cwd.

    `(None, None)` when the head has none (a stub transcript, an unreadable file).
    The session id comes from the SAME line as the cwd, so the row that gets
    filled is identified by the data rather than by the file name.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= SESSION_CWD_SCAN_LINES:
                    break
                if '"cwd"' not in line:   # cheap reject before parsing a big line
                    continue
                try:
                    ev = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                cwd = _first_event_cwd(ev)
                if cwd:
                    return ev.get("sessionId"), cwd
    except OSError:
        pass
    return None, None


def backfill_session_cwd(con, paths=None):
    """Fill `sessions.cwd` for rows ingested before the ingester wrote it.

    IN-PLACE, not a `reset-db` + full re-ingest. The cwd is a few lines into each
    transcript, so filling it costs one bounded head read per stale session:
    measured on the real 1.5 GB store, 117 rows filled inside a 0.9s ingest run.
    A reset would also be LOSSY, not merely slow - the store keeps 1,703 sessions
    while only 127 main transcripts are still on disk, so rebuilding from scratch
    would throw away the other ~1,580 sessions' history to fill a column it could
    not fill for them either. `raw_events` cannot answer it (the verbatim payload
    is kept only for file-history-snapshot lines, see `ingest_transcript` /
    `prune_raw_events`), so the transcripts on disk are the only source either way.

    Idempotent, and cheap in its steady state: a value already there is never
    overwritten, and the sweep only ever re-reads the head of a transcript whose
    session row is STILL NULL. The `return 0` early exit is for a store where
    every row is filled, which the real store never reaches: 1,586 of its 1,703
    rows can never be filled (their transcripts left the disk) and stay NULL
    forever, keep rendering from `project`, and keep the loop running on every
    sweep. Measured there: 117 rows filled in 0.142s on the upgrade run, then
    2.8-3.0ms per sweep, re-reading the 2.2 KB of heads belonging to the handful
    of transcripts that carry no cwd at all. The candidate filter is the file name
    (a main transcript is named `<session-id>.jsonl`, true for all 117 resolvable
    transcripts here), which is what keeps a sweep that still has NULL rows from
    re-reading every transcript head; a name that disagrees with its own
    `sessionId` just leaves that row NULL. Returns the number of rows filled.
    """
    pending = {r[0] for r in con.execute(
        "SELECT session_id FROM sessions WHERE cwd IS NULL").fetchall()}
    if not pending:
        return 0
    filled = 0
    for path in (_main_transcript_paths() if paths is None else paths):
        # `paths` may be the full sweep list; a subagent's cwd is its own, and its
        # `agent-<id>` stem is never a session id, so both filters drop it.
        if path.parent.name == "subagents" or path.stem not in pending:
            continue
        sid, cwd = _head_cwd(path)
        sid = sid or path.stem
        if not cwd or sid not in pending:
            continue
        con.execute("UPDATE sessions SET cwd=? WHERE session_id=? AND cwd IS NULL",
                    [cwd, sid])
        pending.discard(sid)
        filled += 1
    return filled


def _sweep_transcripts(con, paths=None, verbose=False, tick=None):
    """Ingest every transcript under PROJECTS_DIR. Returns (files, changed, lines):
    transcript files seen, how many had new data, and lines ingested. Calls
    `tick()` once per file (drives the progress bar) and logs one line per changed
    transcript when `verbose`."""
    if paths is None:
        paths = _transcript_paths()
    changed = lines = 0
    for path in paths:
        n = ingest_transcript(con, path)
        if n:
            changed += 1
            lines += n
            if verbose:
                _log(f"  transcript {_rel_transcript(path)}: +{n} lines")
        if tick:
            tick()
    return len(paths), changed, lines


def ingest_all(con, verbose=False):
    """Sweep transcripts then backfill file-history (M6: file-history AFTER the
    transcript sweep, so a path first revealed this run resolves its snapshots
    this run; within the pass the promotion UPDATE runs after the index build,
    before emitting rows). Returns the transcript-line count (the historical
    contract); per-file lines are logged when `verbose`."""
    _files, _changed, lines = _sweep_transcripts(con, verbose=verbose)
    # Rows ingested before the ingester wrote `cwd` (and rows whose sweep read
    # started mid-file, which writes no cwd at all). Safe AFTER the sweep, and
    # better there: the sweep cannot have written a post-`cd` value for it to
    # lose to, and a session first seen this run is already filled, so it costs
    # nothing. Only rows still NULL pay a head read.
    backfill_session_cwd(con)
    ingest_file_history(con, verbose=verbose)
    return lines


# --- File versions + content-addressed blobs (Task 5) -------------------------
#
# Step 5.1 investigation, confirmed against the live corpus (735 edit/write
# `toolUseResult`s across 60 transcripts, zero shape mismatches) and the curated
# 233-row store (the file_versions counts below are measured from it). The plan's
# field-name guesses are ADAPTED to what the data actually holds:
#
#   Write  -> toolUseResult = {type: "create"|"update", filePath, content,
#             originalFile, structuredPatch, userModified}
#       * filePath      : absolute path (top level)          [the path key]
#       * content       : full POST content, always a str    [post source]
#       * originalFile  : full PRE content; null on "create", str on "update"
#       * structuredPatch: list of hunks; [] on "create"
#       * userModified=true: `content` is the user's edited final text (what
#         landed on disk), not Claude's proposal; stored as-is.
#
#   Edit   -> toolUseResult = {filePath, oldString, newString, replaceAll,
#             originalFile, structuredPatch, userModified}
#       * NO `content` and NO `type`; POST is not stored directly.
#       * originalFile  : full PRE content (str) WHEN PRESENT, but it is NULL on
#         the large majority of real edits (142/184 Edit results, 77%, in the
#         curated corpus; the plan wrongly assumed it is always a str). With no
#         PRE there is nothing to reconstruct POST from, so both content shas end
#         up null and only path + structuredPatch (the diff) are recorded.
#       * POST (only when originalFile is present) is reconstructed by applying
#         oldString->newString to originalFile (all occurrences when replaceAll,
#         else the first). See _post_content.
#       * structuredPatch: non-empty list of hunks.
#
#   structuredPatch hunk = {oldStart, oldLines, newStart, newLines,
#                           lines: [" ctx", "-removed", "+added", ...]}.
#
#   MultiEdit/NotebookEdit: NOT present in the corpus. _post_content
#   defensively supports a MultiEdit `edits` list (apply sequentially) if a
#   future version emits one; unknown shapes just yield post=None (non-fatal).
#
# CONTENT-COVERAGE LIMITATION (from the null-originalFile majority above):
# toolUseResult alone yields FULL POST CONTENT for only ~39% of rows (91/233
# have a non-null post_sha; 47/233 a non-null pre_sha). The other ~61% are
# patch-only: `cat` cannot rebuild their bytes, but `diff` (structuredPatch)
# still works. Closing that gap is the file-history backfill follow-up below.
#
# Detector: a result is a file edit iff it has a top-level `filePath` (str) AND
# a `structuredPatch` key. In the sample these co-occur perfectly (735 with
# both, 2539 non-edit results with neither). A PDF Read carries `type:"parts"`
# with `filePath` nested under `file`, NOT at top level, so it is correctly
# skipped. Bash results (stdout/stderr, no filePath) are skipped too.
#
# file-history (~/.claude/file-history/<session_id>/<pathhash>@vN): full-content
# snapshots, and the layout IS solvable (the plan's "unsolvable/redundant" claim
# is false). The dir name IS the transcript session_id (verified 76/76 match),
# <pathhash> = sha256(<absolute_path>)[:16] (forward-hash verified, 42/42 stems
# for a sampled session), and @vN is the per-file snapshot order. It is exactly
# the source that fills the ~61% of edits whose originalFile is null, so it is
# NOT redundant with toolUseResult. Ingesting it is deferred:
#   FOLLOW-UP file-history backfill (its own phase, conductor-scheduled): walk
#   <session_id>/<pathhash>@vN, map each stem back to its path via the known
#   sha256(path)[:16], and blob the snapshots to give the ~61% patch-only rows
#   real PRE/POST content. NOT implemented here; this phase relies on
#   toolUseResult alone (files Claude edited, at the ~39% content coverage above).


def _put_blob(con, text):
    """Content-address `text` into `blobs` (dedup by sha256); return its sha, or
    None for None input. Empty string is a real blob (sha of "")."""
    if text is None:
        return None
    sha = _sha(text)
    con.execute("INSERT INTO blobs VALUES (?,?,?) ON CONFLICT (sha) DO NOTHING",
                [sha, len(text.encode("utf-8", "replace")), text])
    return sha


def _post_content(tur, pre):
    """Full post-edit content for a file result. Write carries it directly as
    `content`; Edit does not, so reconstruct it by applying the recorded string
    replacement(s) to the pre-edit `originalFile`. Returns None when neither the
    content nor a usable pre+replacement is present. Never raises."""
    content = tur.get("content")
    if isinstance(content, str):
        return content                     # Write: post given verbatim
    if not isinstance(pre, str):
        return None                        # no pre to apply an edit onto
    edits = tur.get("edits")               # MultiEdit shape (defensive; unseen in corpus)
    if isinstance(edits, list) and edits:
        out = pre
        for e in edits:
            if not isinstance(e, dict):
                continue
            old, new = e.get("oldString"), e.get("newString")
            if not isinstance(old, str) or not isinstance(new, str):
                continue
            out = out.replace(old, new) if e.get("replaceAll") else out.replace(old, new, 1)
        return out
    old, new = tur.get("oldString"), tur.get("newString")
    if isinstance(old, str) and isinstance(new, str):
        return pre.replace(old, new) if tur.get("replaceAll") else pre.replace(old, new, 1)
    return None


def _render_patch(structured, pre, post, path):
    """Prefer the already-computed structuredPatch hunks (stored as JSON); else
    fall back to a difflib unified diff when both pre and post are present; else
    None (e.g. a create, which has no diff, only post content)."""
    if structured:
        return json.dumps(structured)
    if pre is not None and post is not None:
        return "".join(difflib.unified_diff(
            pre.splitlines(True), post.splitlines(True),
            fromfile=path, tofile=path))
    return None


def _apply_structured_patch(content, patch_json):
    """Apply stored structuredPatch hunks (the JSON `_render_patch` writes) to
    `content`, STRICTLY: every context (' ') and deletion ('-') line must match
    the file at the hunk's stated position. Returns the patched text, or None on
    ANY mismatch (difflib-text/unknown format, wrong context, overlapping or
    out-of-range hunks) -- never a half-applied file. Strictness is the safety
    property: `cat` uses this to roll patch-only edits forward, and a patch that
    no longer applies (file changed outside Claude, interleaved session) must
    degrade to stale-but-consistent output, not a silently corrupted mix.

    Line model: split('\\n'), so a trailing newline appears as a final '' element
    that hunks never consume (jsdiff's structuredPatch uses the same convention;
    the untouched tail is re-joined verbatim, preserving the final newline)."""
    try:
        hunks = json.loads(patch_json)
    except (TypeError, ValueError):
        return None                       # difflib fallback text or garbage
    if not isinstance(hunks, list) or not all(isinstance(h, dict) for h in hunks):
        return None
    lines = content.split("\n")
    out = []
    pos = 0                               # next unconsumed index into `lines`
    for h in hunks:
        try:
            old_start, old_lines = int(h["oldStart"]), int(h["oldLines"])
            hunk_lines = h["lines"]
        except (KeyError, TypeError, ValueError):
            return None
        if not isinstance(hunk_lines, list):
            return None
        # 1-based first old line -> 0-based index; a pure-insertion hunk
        # (oldLines == 0) inserts AFTER old line `oldStart`, i.e. at index
        # oldStart itself (unified-diff convention for zero-length ranges).
        start = old_start if old_lines == 0 else old_start - 1
        if start < pos or start > len(lines):
            return None                   # overlapping / out-of-order / past EOF
        out.extend(lines[pos:start])
        pos = start
        for hl in hunk_lines:
            if not isinstance(hl, str) or not hl:
                return None
            tag, text = hl[0], hl[1:]
            if tag in (" ", "-"):
                if pos >= len(lines) or lines[pos] != text:
                    return None           # context/deletion mismatch: refuse
                if tag == " ":
                    out.append(text)
                pos += 1
            elif tag == "+":
                out.append(text)
            elif tag == "\\":
                continue                  # "\ No newline at end of file" marker
            else:
                return None
    out.extend(lines[pos:])
    return "\n".join(out)


def file_version_from_event(con, ev):
    """For an Edit/Write/MultiEdit user tool_result event, return a `file_versions`
    row dict and store its pre/post blobs via _put_blob. Returns None for anything
    that is not a file-editing result (Bash, Read, no path, unknown shape). Uses
    the shapes confirmed in Step 5.1 above. Never raises."""
    if not isinstance(ev, dict):
        return None
    tur = ev.get("toolUseResult")
    if not isinstance(tur, dict):
        return None
    path = tur.get("filePath")
    if not isinstance(path, str) or not path or "structuredPatch" not in tur:
        return None                        # not an edit/write result
    pre = tur.get("originalFile")
    if not isinstance(pre, str):
        pre = None                         # null on create, or absent
    post = _post_content(tur, pre)
    return {
        "event_id": ev.get("uuid"),
        "session_id": ev.get("sessionId", ""),
        "ts": ev.get("timestamp"),
        "path": path,
        "pre_sha": _put_blob(con, pre),
        "post_sha": _put_blob(con, post),
        "patch": _render_patch(tur.get("structuredPatch"), pre, post, path),
        "source": "transcript",   # B2: stamped explicitly; the staging INSERT..SELECT
    }                             # bypasses the column DEFAULT, so a NULL would ship otherwise


_FV_COLS = ["event_id", "session_id", "ts", "path", "pre_sha", "post_sha", "patch", "source"]


def _insert_file_versions(con, rows):
    # Dedup on event_id (mirroring _insert_events / _insert_requests) so a
    # reprocessed line never duplicates a row. file_versions has NO unique
    # constraint, so ON CONFLICT is unavailable; dedup is an anti-join against the
    # target instead. Two paths reprocess a line and must not double-insert: the
    # deterministic truncation rescan (ingest_transcript: `if size < last: last =
    # 0`), and the crash window between this insert and the ingest_state offset
    # advance. Collapse the batch by event_id first (only the keyed rows; a
    # null/empty event_id, which essentially never occurs since a user tool_result
    # carries a uuid, is kept as-is and never matches the anti-join, so it still
    # inserts). Blobs dedup separately on sha.
    keyed, unkeyed = {}, []
    for r in rows:
        if r["event_id"]:
            keyed[r["event_id"]] = r      # collapse repeats sharing one event uuid
        else:
            unkeyed.append(r)             # null/empty key: keep each row distinct
    rows = list(keyed.values()) + unkeyed
    con.execute("CREATE TEMP TABLE IF NOT EXISTS _fv_stage AS SELECT * FROM file_versions WHERE 0=1")
    con.execute("DELETE FROM _fv_stage")
    con.executemany(
        f"INSERT INTO _fv_stage ({','.join(_FV_COLS)}) VALUES ({','.join(['?'] * len(_FV_COLS))})",
        [[r[c] for c in _FV_COLS] for r in rows])
    con.execute("INSERT INTO file_versions SELECT * FROM _fv_stage s "
                "WHERE NOT EXISTS (SELECT 1 FROM file_versions f WHERE f.event_id = s.event_id)")
    con.execute("DELETE FROM _fv_stage")


# --- File-history backfill (Task 8) -------------------------------------------
#
# Step 8.1 investigation, re-confirmed read-only against the live corpus before
# coding (findings; some ADAPT the plan):
#
#   Layout ~/.claude/file-history/<session_id>/<pathhash>@vN: 78 session dirs (all
#   UUIDs matching a transcript), 1557 files (all matching ^[0-9a-f]{16}@v\d+$),
#   96MB, 13 empty snapshots (valid), 0 non-UTF-8. <pathhash> = sha256(abs_path)[:16]:
#   494/494 on-disk hashes resolve from paths seen in transcripts (100%).
#
#   Authoritative mapping (M1): file-history-snapshot events carry
#   snapshot.trackedFileBackups {key -> {backupFileName:"<pathhash>@vN"|null,
#   version, backupTime}}. 16748 non-null entries name a real on-disk file (8 name a
#   since-pruned file, skipped on read); 4255 have backupFileName=null (tracked, not
#   yet backed up) and are skipped. Keys are relative ~half the time (8812 rel vs
#   7944 abs); a relative key is absolutized via the pathhash reverse-index, not the
#   key (all 8812 resolve). backupTime is the authoritative snapshot timestamp
#   (survives an rsync/copy of ~/.claude that mtime does not).
#
#   ADAPTATIONS found in 8.1 (differ from the plan's implicit assumptions):
#     * file-history-snapshot events carry NO top-level sessionId (0/2102) and no
#       top-level timestamp, so raw_events.session_id is empty for them. The
#       session_id (= the on-disk dir name) is derived from the transcript filename
#       (raw_events.transcript_path); the timestamp comes from backupTime only.
#     * a TIMESTAMPTZ cannot be fetched into Python without the optional pytz
#       package, so snapshot_ts is read back as epoch_ms (a plain int) when emitting
#       file_versions (the same footgun cmd_diff / cmd_tokens dodge).
#
#   Version chains are NON-contiguous (B1): 369/643 (session,pathhash) groups start
#   at v2+, 1 has an interior gap; pre_sha is the highest PRESENT lower version's
#   blob, never a naive v-1.
#
# Every snapshot's bytes become a blob unconditionally (irreversible-loss
# insurance). Non-UTF-8 snapshots (0 today) are recorded as a file_history row with
# sha=NULL, note='binary-skipped' and no blob / no file_versions row: a lossy
# errors="replace" decode would contradict the preservation framing.

FILE_HISTORY_DIR = Path.home() / ".claude" / "file-history"
_FH_NAME_RE = re.compile(r"^([0-9a-f]{16})@v(\d+)$")   # <pathhash>@vN


def _fh_session_id(transcript_path):
    """session_id (= the on-disk file-history dir name) for a file-history-snapshot
    raw_event, derived from the transcript filename because the event itself carries
    no sessionId. Mirrors the ingester's subagent-path handling, though these events
    only ever appear in main transcripts."""
    p = Path(transcript_path)
    return p.parent.parent.name if p.parent.name == "subagents" else p.stem


def _snapshot_records(con):
    """Authoritative driver: yield (session_id, pathhash, version, backup_file_name,
    backup_time, key) for every non-null trackedFileBackups entry across every
    file-history-snapshot raw_event. version + timestamp come from the transcript,
    not the filesystem. Never raises on a malformed line."""
    for tp, raw in con.execute(
            "SELECT transcript_path, raw FROM raw_events "
            "WHERE type='file-history-snapshot'").fetchall():
        try:
            ev = json.loads(raw)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        tfb = (ev.get("snapshot") or {}).get("trackedFileBackups")
        if not isinstance(tfb, dict):
            continue
        sid = _fh_session_id(tp)
        for key, meta in tfb.items():
            if not isinstance(meta, dict):
                continue
            bfn = meta.get("backupFileName")
            if not isinstance(bfn, str):        # null = tracked but not yet backed up
                continue
            m = _FH_NAME_RE.match(bfn)
            if not m:
                continue
            yield (sid, m.group(1), int(m.group(2)), bfn, meta.get("backupTime"), key)


def _known_paths(con):
    """Reverse index { sha256(abs_path)[:16] : abs_path } over every absolute path
    the store has seen: resolved file_versions paths, plus Read/Edit/Write/... tool
    inputs. NOTE tool_input uses snake_case file_path / notebook_path (M3), NOT
    camelCase filePath (which lives only in toolUseResult, already covered by
    file_versions.path). _sha here hashes the PATH STRING, not file content."""
    paths = {p for (p,) in con.execute(
        "SELECT DISTINCT path FROM file_versions WHERE path IS NOT NULL").fetchall()}
    for jpath in ("$.file_path", "$.notebook_path"):
        for (p,) in con.execute(
                f"SELECT DISTINCT json_extract_string(tool_input, '{jpath}') FROM events").fetchall():
            if p:
                paths.add(p)
    return {_sha(p)[:16]: p for p in paths}


_FH_COLS = ["session_id", "pathhash", "version", "path", "sha", "snapshot_ts", "note"]


def _insert_file_history(con, rows):
    """Upsert file_history rows, idempotent on (session_id, pathhash, version).
    Snapshots are immutable, so an existing PK is left untouched (DO NOTHING)."""
    con.execute("CREATE TEMP TABLE IF NOT EXISTS _fh_stage AS SELECT * FROM file_history WHERE 0=1")
    con.execute("DELETE FROM _fh_stage")
    con.executemany(
        f"INSERT INTO _fh_stage ({','.join(_FH_COLS)}) VALUES ({','.join(['?'] * len(_FH_COLS))})",
        [[r[c] for c in _FH_COLS] for r in rows])
    con.execute("INSERT INTO file_history SELECT * FROM _fh_stage "
                "ON CONFLICT (session_id, pathhash, version) DO NOTHING")
    con.execute("DELETE FROM _fh_stage")


def _read_snapshot(path):
    """(content, note) for an on-disk snapshot file: the decoded text and note=None,
    or (None, 'binary-skipped') on a non-UTF-8 file (0 today; preservation forbids a
    lossy replace), or None if the file is unreadable/gone (caller skips it)."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        return (data.decode("utf-8"), None)
    except UnicodeDecodeError:
        return (None, "binary-skipped")


def _mtime_ts(path):
    """On-disk mtime as a tz-aware UTC datetime, or None if the file is gone. The
    timestamp fallback when trackedFileBackups.backupTime is missing or unparseable."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _parse_snapshot_ts(btime):
    """Validate a trackedFileBackups.backupTime and return a value bindable into the
    snapshot_ts TIMESTAMPTZ column (the original ISO string when well-formed), or
    None when it is missing / not a string / unparseable. A bad backupTime must
    NEVER abort the sweep: the entry lives permanently in raw_events, so letting the
    ConversionException fire at the bind would wedge every future ingest with no
    self-heal (violating the plan's skip-and-continue contract). A NULL snapshot_ts
    flows safely through epoch_ms / cat, and the snapshot bytes are still preserved
    as a blob + file_history row."""
    if not isinstance(btime, str):
        return None
    try:
        datetime.fromisoformat(btime.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return btime


def _fh_row(sid, pathhash, version, sha, ts, note):
    return {"session_id": sid, "pathhash": pathhash, "version": version,
            "path": None, "sha": sha, "snapshot_ts": ts, "note": note}


def _emit_file_history_versions(con):
    """Emit a source='file-history' file_versions row for every resolved (path +
    sha) file_history row, so cat sees snapshot content. pre_sha is the blob of the
    highest PRESENT lower version in the same (session_id, pathhash) group (B1:
    never a naive v-1); patch is a difflib unified diff when a predecessor exists.
    Idempotent via the _insert_file_versions anti-join on the synthetic 'fh:'
    event_id; already-emitted rows are skipped so a re-run does no difflib work.
    Returns the number of NEW file_versions rows."""
    before = con.execute(
        "SELECT count(*) FROM file_versions WHERE source='file-history'").fetchone()[0]
    done = {r[0] for r in con.execute(
        "SELECT event_id FROM file_versions WHERE source='file-history'").fetchall()}
    # epoch_ms(): read the TIMESTAMPTZ as a plain int (no pytz), re-boxed as a
    # tz-aware UTC datetime for the insert (binding a datetime needs no pytz).
    rows = con.execute(
        "SELECT session_id, pathhash, version, path, sha, epoch_ms(snapshot_ts) "
        "FROM file_history WHERE path IS NOT NULL AND sha IS NOT NULL "
        "ORDER BY session_id, pathhash, version").fetchall()
    fvs, prev = [], {}     # prev[(session_id, pathhash)] = highest present lower version's sha
    for sid, ph, ver, path, sha, ms in rows:
        gk = (sid, ph)
        pre_sha = prev.get(gk)
        prev[gk] = sha                     # advance for the next present version, even if skipped
        eid = f"fh:{sid}:{ph}@v{ver}"
        if eid in done:                    # already emitted; skip the difflib recompute
            continue
        patch = None
        if pre_sha is not None and pre_sha != sha:
            pre = con.execute("SELECT content FROM blobs WHERE sha=?", [pre_sha]).fetchone()
            post = con.execute("SELECT content FROM blobs WHERE sha=?", [sha]).fetchone()
            if pre and post and pre[0] is not None and post[0] is not None:
                patch = "".join(difflib.unified_diff(
                    pre[0].splitlines(True), post[0].splitlines(True),
                    fromfile=path, tofile=path)) or None
        fvs.append({
            "event_id": eid, "session_id": sid,
            "ts": datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms is not None else None,
            "path": path, "pre_sha": pre_sha, "post_sha": sha,
            "patch": patch, "source": "file-history",
        })
    if fvs:
        _insert_file_versions(con, fvs)
    after = con.execute(
        "SELECT count(*) FROM file_versions WHERE source='file-history'").fetchone()[0]
    return after - before


def ingest_file_history(con, verbose=False, on_total=None, tick=None):
    """Backfill ~/.claude/file-history snapshots into blobs + file_history, and emit
    resolved snapshots as source='file-history' file_versions rows so cat can
    reconstruct the ~61% of edits that are patch-only in transcripts. Returns
    (new_snapshots, resolved). Never raises on a bad file: skip and continue.

    M4 (--watch cost): snapshots are immutable, so an already-present PK is skipped
    BEFORE any byte is read/hashed (a steady sweep reads no snapshot bytes). The
    reverse-index rebuild + promotion + emit run only when there is new work (new
    snapshots this sweep) or unresolved (path IS NULL) rows remain; the residual
    per-sweep cost is a directory walk plus a raw_events scan, which is acceptable.
    """
    existing = set(con.execute(
        "SELECT session_id, pathhash, version FROM file_history").fetchall())

    # Authoritative driver first (trackedFileBackups), deduped by PK: the same
    # (session, file, version) recurs across many snapshot events with an identical
    # backupTime, so first-seen wins. This scans raw_events, NOT the on-disk dir, so
    # it runs even after file-history has been pruned: its keys still drive deferred
    # path resolution for the snapshots already preserved in file_history.
    records = {}
    for sid, ph, ver, bfn, btime, key in _snapshot_records(con):
        records.setdefault((sid, ph, ver), (bfn, btime, key))

    # Snapshots to read this sweep (immutable; already-stored PKs are skipped
    # before any byte is read). Report the total up front so the caller can size a
    # progress bar, and log it when verbose.
    pending = (sum(1 for k in records if k not in existing)
               if FILE_HISTORY_DIR.exists() else 0)
    if on_total:
        on_total(pending)
    if verbose:
        _log(f"  file-history: {len(records)} records, {pending} new snapshots to read")

    new_rows, new_count = [], 0
    # Disk-reading passes only when the file-history dir still exists. When it has
    # been pruned (the loss scenario this phase insures against), the snapshots are
    # already preserved as blobs, so we skip straight to promotion + emit below so
    # deferred resolution of the stored rows keeps working.
    if FILE_HISTORY_DIR.exists():
        for (sid, ph, ver), (bfn, btime, key) in records.items():
            if (sid, ph, ver) in existing:
                continue                   # immutable: never re-read
            if tick:
                tick()                     # advance the bar once per pending snapshot
            snap_path = FILE_HISTORY_DIR / sid / bfn
            got = _read_snapshot(snap_path)
            if got is None:
                continue                   # referenced file gone (rare); skip
            content, note = got
            sha = _put_blob(con, content) if content is not None else None
            ts = _parse_snapshot_ts(btime)
            if ts is None:                 # bad/absent backupTime: fall back to mtime
                ts = _mtime_ts(snap_path)
            new_rows.append(_fh_row(sid, ph, ver, sha, ts, note))
            new_count += 1

        # Fallback for on-disk files with no trackedFileBackups entry (transcript
        # gone; 0 today). Uses mtime (UTC-normalized) since backupTime is unavailable.
        for f in FILE_HISTORY_DIR.glob("*/*"):
            m = _FH_NAME_RE.match(f.name)
            if not m:
                continue
            pk = (f.parent.name, m.group(1), int(m.group(2)))
            if pk in existing or pk in records:
                continue
            got = _read_snapshot(f)
            if got is None:
                continue
            content, note = got
            ts = _mtime_ts(f)
            if ts is None:
                continue
            sha = _put_blob(con, content) if content is not None else None
            new_rows.append(_fh_row(pk[0], pk[1], pk[2], sha, ts, note))
            new_count += 1

        if new_rows:
            _insert_file_history(con, new_rows)

    # Promotion (deferred resolution) + emit. Runs regardless of the dir's presence
    # (so pruned-dir rows still resolve), gated on new work or any unresolved row so
    # an idle --watch sweep does nothing here. Count every path-less row, not just
    # the ones with a blob: a binary-skipped row (sha NULL) still needs its path
    # backfilled, and dropping the sha condition keeps the promotion pass alive until
    # it is learned (the emit query below still filters sha IS NOT NULL).
    unresolved = con.execute(
        "SELECT count(*) FROM file_history WHERE path IS NULL").fetchone()[0]
    resolved = 0
    if new_rows or unresolved:
        known = _known_paths(con)
        # Augment the reverse index with self-describing absolute keys, so an
        # absolute-key path that was never seen in an event still resolves.
        for (sid, ph, ver), (bfn, btime, key) in records.items():
            if ph not in known and isinstance(key, str) and os.path.isabs(key) \
                    and _sha(key)[:16] == ph:
                known[ph] = key
        for (ph,) in con.execute(
                "SELECT DISTINCT pathhash FROM file_history WHERE path IS NULL").fetchall():
            p = known.get(ph)
            if p is not None:
                con.execute("UPDATE file_history SET path=? WHERE pathhash=? AND path IS NULL",
                            [p, ph])
        resolved = _emit_file_history_versions(con)

    # With a progress driver attached (cmd_ingest), stay silent here so this print
    # cannot corrupt the live bar; cmd_ingest reports the counts after it closes.
    # Standalone callers (ingest_all, tests) keep the original one-line summary.
    if tick is None and (new_count or resolved):
        print(f"file-history: {new_count} new snapshots, {resolved} resolved")
    return (new_count, resolved)


def compact_db(store_path=None):
    """Rewrite the store into a fresh file to reclaim free blocks; returns
    (old_size, new_size) in bytes.

    A bulk ingest leaves the file ~2x its packed size: CHECKPOINT only flushes
    the WAL and truncates trailing space, it never coalesces the free blocks left
    scattered through the file. The one thing that reclaims them is a full
    rewrite, so COPY FROM DATABASE the whole store into a sibling temp file and
    atomically swap it in (os.replace on the same directory/filesystem). The
    original is untouched until that swap, and a half-written temp is cleaned up
    on any failure, so a crash mid-compaction can never corrupt or drop the store.

    The store's read-write connection MUST already be closed (the file has to be
    unlocked). Returns (0, 0) if the store does not exist yet.
    """
    import duckdb  # lazy, same as connect()
    path = Path(store_path) if store_path is not None else STORE_PATH
    if not path.exists():
        return (0, 0)
    old_size = path.stat().st_size
    tmp = path.with_suffix(path.suffix + ".compact-tmp")

    def _cleanup_tmp():
        for p in (tmp, Path(str(tmp) + ".wal")):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    _cleanup_tmp()  # drop any stale temp left by a previous aborted run
    src_lit = str(path).replace("'", "''")   # ATTACH takes a string literal
    dst_lit = str(tmp).replace("'", "''")
    try:
        mem = duckdb.connect()  # in-memory driver; src/dst are the only real DBs
        try:
            mem.execute(f"ATTACH '{src_lit}' AS src (READ_ONLY)")
            mem.execute(f"ATTACH '{dst_lit}' AS dst")
            mem.execute("COPY FROM DATABASE src TO dst")
            mem.execute("CHECKPOINT dst")   # flush dst so its WAL is folded in before the swap
            mem.execute("DETACH src")
            mem.execute("DETACH dst")
        finally:
            mem.close()
        os.replace(tmp, path)   # same dir -> atomic; original stands until this line
    except BaseException:
        _cleanup_tmp()          # never leave a half-written temp or clobber the original
        raise
    return (old_size, path.stat().st_size)


def prune_raw_events(con):
    """Clear the verbatim `raw` payload of every raw_events row the store never
    reads back, keeping the lightweight (uuid, type, ts, session) index row.

    Only file-history-snapshot lines are read after ingest (`_snapshot_records`);
    everything else duplicates the on-disk transcript and the `events` projection.
    Malformed lines (type IS NULL) keep their raw too. The `type <> '...'` filter
    is NULL-safe on purpose: `NULL <> 'x'` is NULL, so malformed rows are skipped
    (raw retained). Returns (rows_cleared, bytes_cleared). The caller must compact
    afterwards for the space to actually return to the OS (a DuckDB UPDATE only
    frees blocks). Idempotent: a second run clears 0 rows."""
    rows, nbytes = con.execute(
        "SELECT count(*), coalesce(sum(octet_length(encode(raw))), 0) "
        "FROM raw_events WHERE raw IS NOT NULL AND type <> 'file-history-snapshot'"
    ).fetchone()
    if rows:
        con.execute("UPDATE raw_events SET raw = NULL "
                    "WHERE raw IS NOT NULL AND type <> 'file-history-snapshot'")
    return rows, nbytes


class _IngestProgress:
    """Live Rich progress for the ingest phases, auto-disabled off a tty (so cron
    and piped runs stay quiet and the summary log lines carry the detail instead).

    Exposes the tick callbacks that `_sweep_transcripts` and `ingest_file_history`
    accept; when disabled every method is a no-op, so those loops run untouched.
    """

    def __init__(self, enabled):
        self.enabled = enabled
        self._progress = None
        self._tx = None
        self._fh = None

    def __enter__(self):
        if self.enabled:
            from rich.progress import (BarColumn, MofNCompleteColumn, Progress,
                                       SpinnerColumn, TextColumn, TimeElapsedColumn)
            self._progress = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(), MofNCompleteColumn(), TimeElapsedColumn())
            self._progress.start()
        return self

    def __exit__(self, *exc):
        if self._progress:
            self._progress.stop()
        return False

    def start_transcripts(self, total):
        if self._progress:
            self._tx = self._progress.add_task("transcripts ", total=total)

    def tick_transcript(self):
        if self._progress and self._tx is not None:
            self._progress.advance(self._tx)

    def start_file_history(self, total):
        if self._progress:
            self._fh = self._progress.add_task("file-history", total=total)

    def tick_file_history(self):
        if self._progress and self._fh is not None:
            self._progress.advance(self._fh)


def _ingest_lock_path():
    """Path of the singleton lock file guarding one-shot detached ingests.

    Keyed to the STORE's directory (NOT a fixed global path): the thing we
    serialize is DuckDB's single write lock on STORE_PATH, so two processes
    pointed at different $CLAUDE_TRAIL_DB stores must NOT block each other.
    Deriving it from STORE_PATH at call time also makes tests that patch
    STORE_PATH self-isolating (each gets its own lock under its own tmp dir).
    """
    return os.path.join(os.path.dirname(str(STORE_PATH)) or ".", "trail-ingest.lock")


def _ingest_log_path():
    """Where a detached ingest child sends its stdout/stderr: one shared log
    right next to the store (the hook wrapper appends to the same file), so a
    crashing or hanging detached ingest leaves evidence instead of vanishing
    into /dev/null. Derived from STORE_PATH at call time, like the lock."""
    return str(STORE_PATH) + ".ingest.log"


def _acquire_ingest_lock():
    """Try to grab the singleton ingest lock without blocking.

    Returns an open, flock-held file descriptor on success, or None if another
    process already holds it. The caller owns the returned fd and must keep it
    open for as long as the lock should be held, then close it to release.

    fcntl is imported lazily (POSIX only: Linux + macOS, all this tool targets)
    so `import claude_trail_store` still succeeds on a platform without it -- this
    helper is only ever reached through the --detach path.
    """
    import fcntl  # lazy: POSIX-only, keeps the module import portable

    lock_path = _ingest_lock_path()
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        # LOCK_EX | LOCK_NB: take the exclusive lock or fail immediately rather
        # than block. Another live ingest already holding it -> EWOULDBLOCK ->
        # we return None so the caller can skip this run entirely.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Only EWOULDBLOCK/EAGAIN (a held LOCK_NB flock) is a clean "skip this
        # run". Any OTHER OSError (ENOLCK on some NFS, EPERM, ...) is an
        # environmental failure that must surface, not be swallowed into a
        # permanent silent skip where ingest never runs again with no signal.
        os.close(fd)
        return None
    except BaseException:
        # Propagate everything else, but do not leak the fd we just opened.
        os.close(fd)
        raise
    # Keep the lock fd out of the std range (0/1/2). os.open returns the lowest
    # free fd, so if a std fd was closed at launch (e.g. cron `0<&-`) the flock
    # could land on 0/1/2 -- and the child's dup2(devnull, 0/1/2) below would
    # then silently drop it mid-ingest, defeating the whole singleton guard. A
    # singleton lock released while the run is still going lets a second ingest
    # start and contend on DuckDB's one write lock. F_DUPFD copies onto the SAME
    # open file description, so the flock is preserved on the new fd.
    if fd < 3:
        newfd = fcntl.fcntl(fd, fcntl.F_DUPFD, 3)
        os.close(fd)
        fd = newfd
    return fd


def _detach_child_argv(argv):
    """The argv the forked child re-enters cmd_ingest with.

    --detach implies a single one-shot run: strip --detach (so the child does
    not recurse back into _ingest_detach) and --watch (detach never watches).
    Every other flag (--compact, --file-history-only, -v/--verbose) passes
    through unchanged.
    """
    return [a for a in argv if a not in ("--detach", "--watch")]


def _ingest_detach(argv):
    """Run a one-shot ingest in a detached background process, returning at once.

    For a Claude Code hook: `claude-trail ingest --detach` forks the ingest off
    and unblocks the hook immediately, with a singleton lock so concurrent
    turns/sessions never pile up ingests contending on DuckDB's one write lock.
    A skipped run loses nothing -- ingest is incremental and idempotent, so the
    next turn's detach catches up whatever a skipped one missed.
    """
    if not hasattr(os, "fork"):
        # os.fork is POSIX-only (absent on e.g. Windows). This tool targets
        # Linux + macOS; return 0 (a logged no-op) rather than 1, because a hook
        # runs this every turn and a nonzero exit would surface as a hook error
        # each time on such a platform.
        _log("--detach requires os.fork (POSIX only); nothing ingested")
        return 0

    fd = _acquire_ingest_lock()
    if fd is None:
        # Another ingest already holds the lock: skip WITHOUT forking or
        # ingesting. Reachable/testable without a fork; the next turn catches up.
        return 0

    # --detach implies a single one-shot run: strip --detach (so the forked child
    # does not recurse back into here) and --watch, keeping every other flag.
    child_argv = _detach_child_argv(argv)

    pid = os.fork()
    if pid > 0:
        # Parent (the hook process): close OUR copy of the lock fd and return
        # instantly so the hook unblocks. NOTE: fork() duplicated the fd, but the
        # parent's and child's fds refer to the SAME open file description holding
        # ONE flock; the lock releases only when ALL fds on that description are
        # closed, so closing here does NOT drop the child's lock -- it only
        # relinquishes the parent's reference to it.
        os.close(fd)
        # Reap the intermediate child: it _exits right after the second fork
        # (below), so this cannot block. Without it, a long-lived caller (say
        # the TUI one day) would accumulate one zombie per detach; the CLI
        # parent exits so fast it never noticed.
        os.waitpid(pid, 0)
        return 0

    # Child: fully detach so it outlives the hook process that spawned it.
    try:
        # New session + process group: no controlling tty, and no longer in the
        # hook's process group, so the hook exiting cannot signal us.
        os.setsid()
        # Double-fork: this intermediate child exits at once so (a) the parent's
        # waitpid above returns immediately and the worker is reparented to init
        # (no zombie ever), and (b) the worker is not a session leader, so it can
        # never accidentally reacquire a controlling tty.
        if os.fork() > 0:
            os._exit(0)
        # Worker (grandchild): stdin from /dev/null; stdout/stderr APPEND to the
        # shared ingest log next to the store (the hook wrapper writes the same
        # file), so a crashing or stalling detached ingest is diagnosable. An
        # unwritable log falls back to /dev/null: silence over dying. isatty()
        # is False on both, so the Rich progress bar stays off either way.
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        try:
            logfd = os.open(_ingest_log_path(),
                            os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
        except OSError:
            logfd = devnull
        os.dup2(logfd, 1)
        os.dup2(logfd, 2)
        # Only close fds above the std range: if a std fd was closed at launch,
        # os.open may have handed us 0/1/2, which we just dup2'd onto -- closing
        # would drop a stream we mean to keep (same guard as the lock fd's).
        if logfd > 2 and logfd != devnull:
            os.close(logfd)
        if devnull > 2:
            os.close(devnull)
        # Keep `fd` open for the whole run: it is what holds the flock, releasing
        # only when this worker exits and all fds on the description finally close.
        cmd_ingest(child_argv)
    except BaseException:
        # _exit (not sys.exit): skip atexit handlers / buffer flushes in the
        # forked child, which could otherwise double-flush the parent's buffers.
        os._exit(1)
    os._exit(0)


def cmd_ingest(argv):
    # --detach: fork a one-shot ingest into the background and return at once (for
    # a Claude Code hook). Dispatched here so `claude-trail ingest --detach` needs
    # no cli_main change; the forked child re-enters cmd_ingest with --detach
    # stripped (see _ingest_detach), taking the normal one-shot path below.
    if "--detach" in argv:
        return _ingest_detach(argv)
    watch = "--watch" in argv
    # --file-history-only runs just the file-history backfill pass (Phase 8): it does
    # NOT sweep transcripts and does NOT touch ingest_state, but uses the same
    # connect() writer and end-of-run CHECKPOINT, so --compact still composes.
    fh_only = "--file-history-only" in argv
    # -v/--verbose logs one line per changed transcript (and the file-history scan)
    # when the live bar is off; with the bar on, the bar itself carries progress.
    verbose = "-v" in argv or "--verbose" in argv
    # --compact rewrites the store to reclaim free blocks after the sweep; it is a
    # one-shot maintenance step, so it is meaningless (and skipped) under --watch.
    compact = "--compact" in argv and not watch
    if "--compact" in argv and watch:
        _log("note: --compact is ignored with --watch (never compact inside the poll loop)")
    interval = 5.0
    con = connect_writer()
    if con is None:
        # A reader (the TUI's Tokens/prompt/database lookups, or an ad-hoc
        # query) holds the store. Idempotent + on a timer, so skipping is right;
        # exit 0 so systemd does not record a routine collision as a failure.
        _log("skipped: store is held by a reader; the next run catches up")
        return 0
    mode = "watch" if watch else ("file-history-only" if fh_only else "one-shot")
    # The live bar needs a tty; off a tty (cron/pipe) it is disabled and the
    # per-file verbose logs (safe with no bar to corrupt) carry the detail.
    bar_enabled = sys.stdout.isatty()
    file_verbose = verbose and not bar_enabled
    _log(f"ingest start: {STORE_PATH} (mode {mode})")
    sweep = 0
    stored_anything = False   # drives the end-of-run CHECKPOINT (see _checkpoint_due)
    try:
        while True:
            sweep += 1
            t0 = time.monotonic()
            before = _table_counts(con)
            files = changed = lines = cwd_filled = 0
            t_tx = 0.0
            with _IngestProgress(bar_enabled) as prog:
                if not fh_only:
                    paths = _transcript_paths()
                    prog.start_transcripts(len(paths))
                    s = time.monotonic()
                    files, changed, lines = _sweep_transcripts(
                        con, paths=paths, verbose=file_verbose, tick=prog.tick_transcript)
                    # In-place backfill of sessions.cwd for rows ingested before it
                    # was written, and for rows whose sweep read started mid-file
                    # (which writes no cwd). Reuses the swept path list; only rows
                    # still NULL pay a head read. Safe after the sweep for the same
                    # reason as in `ingest_all`. Skipped under --file-history-only,
                    # which touches no transcript at all.
                    cwd_filled = backfill_session_cwd(con, paths=paths)
                    t_tx = time.monotonic() - s
                s = time.monotonic()
                fh_new, fh_resolved = ingest_file_history(
                    con, verbose=file_verbose,
                    on_total=prog.start_file_history, tick=prog.tick_file_history)
                t_fh = time.monotonic() - s
            after = _table_counts(con)
            delta = {k: after.get(k, 0) - before.get(k, 0) for k in after}
            # A backfilled cwd is an UPDATE, so it moves no row count: name it
            # explicitly or the flush would be skipped as "stored nothing".
            stored_anything = stored_anything or any(delta.values()) or bool(cwd_filled)
            elapsed = time.monotonic() - t0
            # Under --watch, stay quiet on an idle sweep (nothing new) so the poll
            # loop does not spam; always report a changed sweep and any one-shot run.
            if not watch or changed or fh_new or fh_resolved or cwd_filled or any(delta.values()):
                if not fh_only:
                    _log(f"transcripts: {changed}/{files} changed, +{lines} lines")
                if cwd_filled:   # silent on every later run: nothing is NULL any more
                    _log(f"sessions: {cwd_filled} cwd backfilled")
                _log(f"file-history: {fh_new} new snapshots, {fh_resolved} resolved")
                _log("stored: " + ", ".join(
                    f"+{delta.get(k, 0)} {k}" for k in _INGEST_COUNT_TABLES if k in after))
                _log(f"sweep {sweep} done in {elapsed:.1f}s "
                     f"(transcripts {t_tx:.1f}s, file-history {t_fh:.1f}s)")
            if not watch:
                break
            time.sleep(interval)
    finally:
        # Flush the WAL into the main file on exit (one-shot end, or --watch
        # shutdown), never per-iteration, and only when it is due: at a short
        # ingest cadence the flush is the bulk of the write-lock window every
        # reader collides with (see _checkpoint_due). Note: CHECKPOINT does NOT
        # reclaim DuckDB's interspersed free blocks, so the file stays ~2x its
        # packed size after a bulk ingest; the full rewrite that does reclaim
        # them is compact_db(), run below (non-watch only) after the write lock
        # is freed - which is why --compact forces the flush first.
        due, why = _checkpoint_due(stored_anything, force=compact or watch)
        if due:
            _log(f"checkpoint: flushing WAL to disk ({why})")
            con.execute("CHECKPOINT")
            try:
                _checkpoint_stamp().touch()
            except OSError:
                pass   # a stamp we cannot write just means the next run flushes
        else:
            _log(f"checkpoint: skipped ({why})")
        con.close()
    if compact:  # after con.close(), so the store file is unlocked for the rewrite
        old, new = compact_db()
        _log(f"compacted {_fmt_bytes(old)} -> {_fmt_bytes(new)}")
    _log(f"store: {STORE_PATH} ({_fmt_bytes(_store_size())})")
    return 0


def cmd_reset_db(argv):
    """Delete the DuckDB store file (and its WAL) so the next `ingest` rebuilds it
    from the transcripts. Prompts for confirmation unless -y/--yes is given.

    The store is a derived cache: every table rebuilds from the transcripts under
    ~/.claude/projects, and file-history snapshot blobs re-ingest from
    ~/.claude/file-history while it exists, so a reset loses nothing a later
    `ingest` cannot rebuild (barring file-history already pruned).
    """
    assume_yes = "-y" in argv or "--yes" in argv
    targets = [STORE_PATH, Path(str(STORE_PATH) + ".wal")]
    existing = [p for p in targets if p.exists()]
    if not existing:
        print(f"no store at {STORE_PATH}; nothing to reset")
        return 0
    print("reset-db will DELETE:")
    for p in existing:
        print(f"   {p}  ({_fmt_bytes(p.stat().st_size)})")
    print("It rebuilds from transcripts on the next `claude-trail ingest`.")
    if not assume_yes:
        try:
            answer = input("Delete these files? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("aborted; nothing deleted")
            return 1
    for p in existing:
        try:
            p.unlink()
            print(f"deleted {p}")
        except OSError as e:
            print(f"error: could not delete {p}: {e}")
            return 1
    print("reset done; run `claude-trail ingest` to rebuild")
    return 0


def cmd_prune_raw(argv):
    """Shrink the store by clearing the bulky verbatim raw_events payload, then
    compact to reclaim the space. Prompts for confirmation unless -y/--yes.

    raw_events is a verbatim third copy of the transcripts (which stay on disk):
    only file-history-snapshot lines are read after ingest, so clearing the rest
    frees the store's single biggest consumer (typically ~2/3 of it) and breaks
    no feature (search/tokens/cat/diff read events/api_requests/file_versions/
    blobs). The (uuid, type, ts, session) index rows and every derived table are
    kept. Going forward, `ingest` no longer stores these payloads, so the store
    stays lean; this one-shot cleans up rows written before that change.
    `--no-compact` skips the rewrite (leaves the freed blocks for a later compact).
    """
    assume_yes = "-y" in argv or "--yes" in argv
    do_compact = "--no-compact" not in argv
    if not STORE_PATH.exists():
        print(f"no store at {STORE_PATH}; nothing to prune")
        return 0
    con = connect()
    rows, nbytes = con.execute(
        "SELECT count(*), coalesce(sum(octet_length(encode(raw))), 0) "
        "FROM raw_events WHERE raw IS NOT NULL AND type <> 'file-history-snapshot'"
    ).fetchone()
    if not rows:
        con.close()
        print("raw_events already pruned; nothing to clear")
        return 0
    print(f"prune-raw will clear the verbatim payload of {rows:,} raw_events rows "
          f"(~{_fmt_bytes(nbytes)} of JSON),")
    print("keeping file-history-snapshot + malformed lines and every derived table.")
    print("search / tokens / cat / diff keep working; transcripts under "
          "~/.claude/projects stay the source of truth.")
    if not assume_yes:
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            con.close()
            print("aborted; nothing changed")
            return 1
    cleared, _ = prune_raw_events(con)
    print(f"cleared {cleared:,} rows (~{_fmt_bytes(nbytes)})")
    con.execute("CHECKPOINT")
    con.close()
    if do_compact:
        old, new = compact_db()
        print(f"compacted {_fmt_bytes(old)} -> {_fmt_bytes(new)}")
    else:
        print("skipped compaction (--no-compact); run `claude-trail ingest --compact` to reclaim")
    print(f"store: {STORE_PATH} ({_fmt_bytes(_store_size())})")
    return 0


# --- Search, stats, tokens CLI (Task 4) ---------------------------------------


def _print_rows(rows, headers):
    from rich.console import Console
    from rich.table import Table
    t = Table(*headers, box=None, pad_edge=False)
    for r in rows:
        t.add_row(*[("" if v is None else str(v)) for v in r])
    Console().print(t)


def cmd_search(argv):
    # usage: search <regex> [--project P] [--tool T] [--session S] [--since ISO] [--limit N] [-i]
    args = argv[1:]
    pat = args[0] if args and not args[0].startswith("-") else None
    if not pat:
        print("usage: claude-trail search <regex> [--tool T --project P --since ISO --limit N -i]")
        return 2
    def opt(name, default=None):
        return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default
    flags = "(?i)" if "-i" in args else ""
    where, params = ["(regexp_matches(coalesce(text,''), ?) OR regexp_matches(coalesce(stdout,''), ?) OR regexp_matches(coalesce(stderr,''), ?))"], [flags+pat]*3
    if opt("--tool"):    where.append("tool_name = ?");                 params.append(opt("--tool"))
    if opt("--session"): where.append("session_id LIKE ?");            params.append(opt("--session")+"%")
    if opt("--since"):   where.append("ts >= ?");                      params.append(opt("--since"))
    # --project matches EITHER path column: `project` is the lossy dir-name decoding
    # (`claude-trail` is stored as `claude/trail`), `cwd` the exact launch dir the
    # Prompts tab shows, so the name on screen is the name that filters.
    if opt("--project"): where.append("session_id IN (SELECT session_id FROM sessions WHERE project LIKE ? OR cwd LIKE ?)"); params += ["%"+opt("--project")+"%"]*2
    limit = int(opt("--limit", "1000"))
    sql = (f"SELECT strftime(ts,'%Y-%m-%d %H:%M'), substr(session_id,1,8), tool_name, "
           f"regexp_replace(coalesce(text, coalesce(stdout,'')), '\\s+', ' ', 'g') "
           f"FROM events WHERE {' AND '.join(where)} ORDER BY ts DESC LIMIT {limit}")
    con = connect(read_only=True)
    try:
        rows = [(a, b, c, (d or '')[:120]) for a, b, c, d in con.execute(sql, params).fetchall()]
    finally:
        con.close()
    _print_rows(rows, ["time", "sess", "tool", "match"])
    return 0


def cmd_tokens(argv):
    # usage: tokens [--session S] [--by session|command|tool|day|model] [--since ISO] [--limit N]
    # default: per-session totals; with --session: per-command breakdown for it
    args = argv[1:]
    def opt(name, default=None):
        return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default
    sess, since = opt("--session"), opt("--since")
    by = opt("--by", "command" if sess else "session")
    limit = int(opt("--limit", "30"))
    where, params = [], []
    if sess:  where.append("session_id LIKE ?"); params.append(sess + "%")
    if since: where.append("ts >= ?");           params.append(since)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    sql = {
        "session": f"""SELECT substr(session_id,1,8), count(*), sum(output_tokens),
                              sum(cache_creation_tokens), sum(cache_read_tokens),
                              strftime(min(ts),'%Y-%m-%d %H:%M'), strftime(max(ts),'%Y-%m-%d %H:%M')
                       FROM api_requests {w} GROUP BY session_id
                       ORDER BY 3 DESC LIMIT {limit}""",
        "command": f"""SELECT strftime(ts,'%m-%d %H:%M'), tool_name,
                              substr(regexp_replace(coalesce(command,''),'\\s+',' ','g'),1,60),
                              output_tokens, calls_in_request
                       FROM command_tokens {w}
                       ORDER BY output_tokens DESC LIMIT {limit}""",
        "tool":    f"""SELECT tool_name, count(*), sum(output_tokens_share)::BIGINT
                       FROM command_tokens {w} GROUP BY 1 ORDER BY 3 DESC LIMIT {limit}""",
        "day":     f"""SELECT strftime(ts,'%Y-%m-%d'), count(*), sum(output_tokens),
                              sum(cache_creation_tokens)
                       FROM api_requests {w} GROUP BY 1 ORDER BY 1 DESC LIMIT {limit}""",
        "model":   f"""SELECT model, count(*), sum(output_tokens), sum(input_tokens)
                       FROM api_requests {w} GROUP BY 1 ORDER BY 3 DESC LIMIT {limit}""",
    }.get(by)
    if sql is None:
        print("usage: claude-trail tokens [--session S] [--by session|command|tool|day|model] [--since ISO] [--limit N]")
        return 2
    headers = {"session": ["sess","reqs","out","cache_new","cache_read","first","last"],
               "command": ["time","tool","command","out_tokens","calls_in_req"],
               "tool":    ["tool","calls","out_tokens"],
               "day":     ["day","reqs","out","cache_new"],
               "model":   ["model","reqs","out","in"]}[by]
    con = connect(read_only=True)
    try:
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()
    _print_rows(rows, headers)
    return 0


def _fmt_bytes(n):
    """Human-readable byte count (e.g. '949.2 MB'). None -> '?'."""
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# Store-owned tables, in display order, for db_info's per-table row counts.
_STORE_TABLES = ["raw_events", "events", "api_requests", "file_versions",
                 "file_history", "blobs", "sessions", "ingest_state"]


def command_tokens_for(con, session_id, command, ts=None):
    """Output-token generation cost of the request that emitted a Bash command,
    matched by (session_id, command text). Used by the TUI command-detail modal.

    A tool call is billed as part of the API request that emitted it, so the cost
    is that request's `output_tokens` (thinking/text in the same message included),
    split evenly when the request emitted several parallel tool calls. Returns
    ``{"output_tokens", "calls_in_request", "share", "model"}`` or None when there
    is no matching ingested request (the store tails transcripts, so a just-run
    command may not be recorded yet). `ts` (the hook's ISO timestamp) breaks ties
    when the same command ran more than once in the session, picking the tool_use
    closest in time; without it the most recent match wins. Read-only; never
    raises for a caller that already guarded the connection.
    """
    if not session_id or not command:
        return None
    if ts:
        order = "ORDER BY abs(epoch(e.ts) - epoch(try_cast(? AS TIMESTAMPTZ))) NULLS LAST"
        params = [session_id, command, ts]
    else:
        order = "ORDER BY e.ts DESC"
        params = [session_id, command]
    row = con.execute(
        f"""SELECT e.request_id, r.output_tokens, r.model
            FROM events e JOIN api_requests r ON r.request_id = e.request_id
            WHERE e.type = 'tool_use' AND e.session_id = ? AND e.text = ?
            {order} LIMIT 1""",
        params,
    ).fetchone()
    if not row:
        return None
    request_id, output_tokens, model = row
    calls = con.execute(
        "SELECT count(*) FROM events WHERE request_id = ? AND type = 'tool_use'",
        [request_id],
    ).fetchone()[0] or 1
    out = output_tokens or 0
    return {
        "output_tokens": out,
        "calls_in_request": calls,
        "share": out / calls,
        "model": model or "",
    }


# A user turn's stored text can carry wrappers Claude Code injected around what
# the user actually typed. `<system-reminder>` blocks are context the harness
# adds, not prompt, so they are stripped for display. Slash-command blocks
# (`<command-name>` ...) are NOT stripped: invoking a command IS what the user
# did, so hiding it would misreport the turn.
_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
# Turns the harness generated rather than the user: a subagent finishing, the
# framing around a local command, output echoed back, an interrupt marker, an
# attachment echo, a skill body injected as a turn of its own after the /slash
# turn that loaded it, and the carry-over blob a compaction opens with. They are
# skipped so a turn reports the instruction a human actually gave. What a human
# authored or invoked is kept, including slash commands (`<command-name>`) and
# `!` bash-mode input (`<bash-input>`): running one IS the user's turn.
TURN_NOISE_PREFIXES = (
    "[Request interrupted",
    "<task-notification>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<bash-stdout>",
    "[Image: source:",                  # attachment echo, same ts as the typed text
    "Base directory for this skill:",   # skill body, a turn after the /slash turn
    "This session is being continued from a previous conversation",   # compaction
)


def _prefix_alternation(prefixes):
    """`^(p1|p2|...)` over literal prefixes, escaping only the metacharacters
    Python's `re` and DuckDB's RE2 read identically.

    One definition feeds both `TURN_NOISE_RE` (the per-lookup Python filter) and
    the SQL `regexp_matches` in `prompt_turns`, so the two engines can never
    drift apart. Hand-rolled rather than `re.escape`, which also emits `\\ ` for
    the spaces in these prefixes - accepted by RE2 today, but required by
    neither engine's grammar.
    """
    meta = r"\.^$|()[]{}*+?"

    def esc(prefix):
        return "".join(("\\" + c if c in meta else c) for c in prefix)

    return "^(" + "|".join(esc(p) for p in prefixes) + ")"


TURN_NOISE_PATTERN = _prefix_alternation(TURN_NOISE_PREFIXES)   # bound into SQL too
TURN_NOISE_RE = re.compile(TURN_NOISE_PATTERN)
# User turns examined per lookup, newest-first: enough to step over a run of
# auto-turns (several parallel subagents can finish back to back) without
# scanning the whole session.
PROMPT_LOOKBACK_TURNS = 16

# The block a slash command is stored as, e.g.
#   <command-name>/model</command-name> ... <command-args>opus</command-args>
# (the TUI's COLOR_CMD_RE parses the same shape for /color). Matched at the HEAD
# of the turn (`.match`, group 1 = the name) and only across the leading run of
# `<command-*>` elements: a `<command-name>` quoted mid-prose is a user ASKING
# about a command, not invoking one, and swallowing the whole turn into `/compact`
# would misreport it. `<command-args>` is then read from that head run only
# (group 0), so a later block cannot contribute the args of this one.
#
# `<command-name>` is not always first: a plugin/skill command emits
# `<command-message>dev-orchestrate</command-message>` ahead of it, so the head run
# is accepted on BOTH sides of the name (lazily on the leading side, so a turn that
# does open with `<command-name>` still matches it with zero prefix). Without that,
# those turns fell through to the raw text and the TUI's one-line Prompt cell
# rendered XML (measured: 3 of the newest 200 turns).
_SLASH_HEAD_RE = re.compile(
    r"\s*(?:<command-[a-z-]+>[^<]*</command-[a-z-]+>\s*)*?"
    r"<command-name>\s*([^<]+?)\s*</command-name>"
    r"(?:\s*<command-[a-z-]+>[^<]*</command-[a-z-]+>)*")
_SLASH_ARGS_RE = re.compile(r"<command-args>\s*([^<]*?)\s*</command-args>")


def clean_prompt_text(text):
    """A user turn's text with the harness-injected wrappers removed (see
    `_SYSTEM_REMINDER_RE`). Returns "" for a turn that was nothing else."""
    if not text:
        return ""
    return _SYSTEM_REMINDER_RE.sub("", text).strip()


# The SQL twin of `clean_prompt_text`, bound into `prompt_turns` so both sides of
# the store clean a turn the same way. The trim charset is explicit because
# DuckDB's `trim()` strips ONLY spaces where Python's `.strip()` strips all
# whitespace; leaving it implicit made the two disagree on every turn padded with
# a newline or a tab - an unstripped `preview` (which renders as a blank first
# line), a surviving leading `\n` defeating the `^` of TURN_NOISE_PATTERN so a
# noise turn became a row, and a reminder-only turn surviving as `preview='\n'`
# instead of being dropped by `clean <> ''`.
#
# ASCII whitespace is all the two engines can be made to share: a turn padded
# with Unicode-only whitespace (NBSP, U+2028) would still differ by exactly those
# characters, since `str.strip()` removes them and no fixed `trim()` charset can
# enumerate them. None exists in the corpus.
PROMPT_TRIM_CHARS = " \t\n\r\v\f"                 # == string.whitespace
PROMPT_CLEAN_SQL = "trim(regexp_replace(text, ?, '', 'sg'), ?)"
PROMPT_CLEAN_PARAMS = (_SYSTEM_REMINDER_RE.pattern, PROMPT_TRIM_CHARS)


def slash_label(text):
    """`/compact` (or `/model opus`) for a turn that STARTS with a slash-command
    block; `text` unchanged for anything else, including prose that merely quotes
    a `<command-name>` block (see `_SLASH_HEAD_RE`).

    Display only, applied by the CALLERS that render a one-line label (TUI
    prompts list, CLI print) and deliberately NOT baked into the query results:
    the full-prompt view shows the raw cleaned text, and collapsing it here would
    throw away the rest of the turn.
    """
    if not text:
        return text
    m = _SLASH_HEAD_RE.match(text)
    if not m:
        return text
    name = m.group(1).strip()
    if not name.startswith("/"):
        name = "/" + name          # stored with the slash today; tolerate either
    args = _SLASH_ARGS_RE.search(m.group(0))
    arg = args.group(1).strip() if args else ""
    return f"{name} {arg}" if arg else name


# The fields `action_label` reads off a `tool_use`'s raw input, in priority
# order. Measured on a real store: `events.text` is NULL for 2,818 of 31,279
# tool calls (every tool without a `_COMMAND_FIELD` entry - Agent, Task, Skill,
# ToolSearch, WebFetch, WebSearch, AskUserQuestion and the MCP tools), while
# `tool_input` is populated for every single `tool_use` row (0 NULLs). This list
# labels 1,935 of those text-less calls, so 97% of all calls end up with a
# label; the rest are MCP tools with keys of their own (`page_id`, `channel_id`,
# `taskId`) and render a dim `-`.
_ACTION_FIELDS = ("description", "file_path", "path", "pattern", "query", "url",
                  "skill", "notebook_path", "command")


def action_label(tool_name, text, tool_input):
    """What one tool call did, in a line: the store's `text` when it has one,
    else the first `_ACTION_FIELDS` entry of the raw `tool_input` JSON.

    Resolved at RENDER time rather than at ingest on purpose: extending
    `_COMMAND_FIELD` would only label newly ingested rows, and `events` can no
    longer be re-derived from `raw_events` (the raw payload is pruned for
    non-snapshot lines), so every already-stored row would stay blank until a
    full re-ingest.

    `tool_name` selects nothing today - the field order above is tool-agnostic
    and already puts `description` (the Agent tool's only useful field) first -
    but it is what a per-tool rule would key off, and it keeps the call site
    reading as "label this row".

    Returns "" when neither source says anything: a `tool_input` that is absent,
    is not JSON, is not an object, or carries none of the known fields. The
    caller renders that as a dim placeholder rather than an empty cell.

    Pure text, so it lives here with the turn model both readers share: the CLI
    (`prompts --at`) calls it directly and the TUI's turn page imports it lazily,
    the same way it imports `slash_label`.
    """
    if text:
        return text
    try:
        data = json.loads(tool_input) if tool_input else None
    except (TypeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    for key in _ACTION_FIELDS:
        value = data.get(key)
        # Strings only: every field above is a path, a pattern or prose. A dict
        # or a list under one of those names means a tool this list does not
        # know, and dumping its JSON into the cell would be noise, not a label.
        if isinstance(value, str) and value.strip():
            return value
    return ""


def short_tool_name(tool_name):
    """An MCP tool's own name, without its server prefix.

    Real tool names run to 63 characters
    (`mcp__plugin_chrome-devtools-mcp_chrome-devtools__evaluate_script`), all of
    which is server routing until the last `__`. A tool column is narrow, so
    keeping the prefix would ellipsize every MCP row to the same `mcp__plugin…`:
    18 of the 77 distinct tool names in a real store are MCP ones, and they would
    all read alike. A plain tool name has no `__` and is returned unchanged.

    Known limit, deliberately not fixed: dropping the server can COLLIDE two
    tools whose own names match. In a real store that is
    `mcp__plugin_linear_linear__authenticate`,
    `mcp__plugin_Notion_notion__authenticate` and
    `mcp__plugin_slack_slack__authenticate`, which all render `authenticate` -
    7 rows out of 31,279 tool calls (0.02%). (Two further collisions,
    `notion-fetch` and `notion-update-page`, are one tool reached under two
    server aliases, so they are not really collisions at all.) Disambiguating
    would mean keeping part of the prefix on those rows, which trades a legible
    column for every MCP call against 0.02% of them; the turn's full detail is
    one cell over.
    """
    return tool_name.rsplit("__", 1)[-1] if "__" in tool_name else tool_name


def prompt_for_command(con, session_id, ts=None, agent_id=None):
    """The prompt that led to a command: the newest `user` turn at or before `ts`
    in the same (session, agent) thread. Used by the TUI command-detail modal.

    One rule covers both cases the modal shows. For a main-agent row
    (``agent_id`` None) the match is the user turn that triggered the command;
    for a subagent row it is that subagent's own turn, which on its first turn is
    the task prompt it was spawned with (a subagent transcript opens with the
    spawn prompt as a user message). Threads are kept apart so a subagent's
    command never reports the parent's turn, and vice versa. Harness-generated
    turns (`TURN_NOISE_RE`) are stepped over, so the answer is the last thing a
    human actually asked rather than a task-notification blob - including an
    `[Image: source: ...]` attachment echo, which shares its timestamp with the
    text the human typed and could otherwise win the `ORDER BY ts DESC` tie.

    Matching needs only what the live hook entry already carries
    ``(session_id, ts, agent_id)``, not the command's own `tool_use` row, so it
    still answers while ingest lags on the command itself. Returns
    ``{"text", "ts"}`` (text cleaned by `clean_prompt_text`, ts as a string so no
    `pytz` is needed to materialize it) or None when the thread has no prompt
    turn yet. Read-only; never raises for a caller that already guarded the
    connection.
    """
    if not session_id:
        return None
    where = ["e.type = 'user'", "e.session_id = ?"]
    params = [session_id]
    # Bind the thread explicitly rather than `IS NOT DISTINCT FROM ?`: a NULL
    # parameter has no inferable type in a prepared statement.
    if agent_id:
        where.append("e.agent_id = ?")
        params.append(agent_id)
    else:
        where.append("e.agent_id IS NULL")
    if ts:
        # try_cast so a malformed hook timestamp degrades to "newest turn in the
        # thread" instead of comparing against NULL and matching nothing.
        where.append("(try_cast(? AS TIMESTAMPTZ) IS NULL"
                     " OR e.ts <= try_cast(? AS TIMESTAMPTZ))")
        params += [ts, ts]
    rows = con.execute(
        f"""SELECT e.ts::VARCHAR, e.text FROM events e
            WHERE {' AND '.join(where)}
            ORDER BY e.ts DESC LIMIT {PROMPT_LOOKBACK_TURNS}""",
        params,
    ).fetchall()
    for row_ts, text in rows:
        cleaned = clean_prompt_text(text)
        if cleaned and not TURN_NOISE_RE.match(cleaned):
            return {"text": cleaned, "ts": row_ts or ""}
    return None


# --- Turn model: one row per user prompt, plus what the turn did ---------------
#
# A turn is a main-thread user prompt (agent_id IS NULL) plus every event in the
# same session from that prompt's ts up to the next prompt's ts (lead(ts)), open-
# ended for the newest prompt of each session. Subagent work lands inside the
# window for free: a subagent's rows carry the PARENT session_id and a non-null
# agent_id, and they are timestamped while the turn runs.

# Preview length in the list query. The full text is fetched per turn by
# turn_prompt_text only when a detail view opens: the longest turn measured in one
# PROMPT_TURN_RAW_SCAN-sized window was 21,696 chars, and the longest anywhere in
# that store 646,245 (a hook prompt, pasted three times), so carrying every full
# text in the list would be wasteful. turn_prompt_text itself is uncapped - a
# caller that renders it needs its own bound.
PROMPT_PREVIEW_CHARS = 400
# User turns cleaned per prompt_turns call, newest-first. This bound is load-
# bearing, not a micro-optimization: cleaning EVERY main-thread user turn of a
# real store (5,433 of them, some 20 KB) costs 1.96s, versus 74ms for the newest
# 400. It is safe because the newest-N set is suffix-closed per session - the
# next prompt of any fetched turn is itself fetched, so every window is bounded
# correctly - except each session's newest turn, which has no next prompt at all
# and correctly gets next_ts NULL (an open-ended window, by design). The
# `ORDER BY ts DESC LIMIT` edge can also split a group of turns sharing one
# (session_id, ts), in which case the oldest turn in the scan collapses over
# whichever of its texts was fetched and may keep the shorter one. That costs a
# preview, never a row, and is only reachable once the kept set reaches the raw
# edge (i.e. most of the scan was noise).
PROMPT_TURN_RAW_SCAN = 400
PROMPT_TURN_LIMIT = 200
TURN_ACTION_LIMIT = 500


# The result keys of one turn row, in the SELECT's column order. Shared by
# `prompt_turns` and `find_turn` so a caller can render either the same way (the
# TUI's `PromptTurn` record is pinned against this set in the test suite).
_TURN_ROW_KEYS = ["session_id", "ts", "next_ts", "event_id", "preview", "full_len",
                  "tools", "bash", "agents", "subcalls", "output_tokens",
                  "subagent_output_tokens", "requests", "last_ts", "project", "cwd"]


def _turn_rows(con, where, params, pick, pick_params=(), raw_scan=None):
    """The turn-model query itself: the list (`prompt_turns`) and the single-turn
    lookup (`find_turn`) are both this, with a different `pick`.

    `where` bounds the raw user-turn scan and `raw_scan` caps it (None = uncapped,
    which only `find_turn` uses, and only ever scoped to ONE session). `pick` is
    the SELECT over `win` that decides which windows become result rows. What sits
    between them - the clean, the noise filter, the same-timestamp collapse, the
    `lead(ts)` window and the per-window aggregates - is written once here, so the
    list and a turn opened out of it can never disagree about where that turn
    starts or ends.

    Bind order is `where`'s params, then the clean/noise params this function
    owns, then `pick`'s own. Reminder regex, trim charset and noise regex are each
    bound from their one Python definition (`PROMPT_CLEAN_PARAMS`,
    `_prefix_alternation`), so the SQL clean cannot drift from
    `clean_prompt_text`: `preview` is always a prefix of `turn_prompt_text` for
    the same turn, and the `^` noise anchor sees the same string in both engines.
    """
    bound = f" ORDER BY ts DESC LIMIT {int(raw_scan)}" if raw_scan else ""
    rows = con.execute(
        f"""WITH raw AS (
                SELECT session_id, ts, event_id, text FROM events
                WHERE {' AND '.join(where)}{bound}),
            kept AS (
                SELECT session_id, ts, event_id, {PROMPT_CLEAN_SQL} AS clean
                FROM raw),
            -- Collapse turns sharing one (session_id, ts), keeping the longest
            -- text. Both aggregates order by the SAME (length, event_id) pair,
            -- which is unique per row, so the id and the text always come from
            -- one kept row: turn_prompt_text(event_id) can never disagree with
            -- the preview the list showed.
            turns AS (
                SELECT session_id, ts,
                       arg_max(event_id, (length(clean), event_id)) AS event_id,
                       arg_max(clean, (length(clean), event_id))    AS clean
                FROM kept
                WHERE clean <> '' AND NOT regexp_matches(clean, ?)
                GROUP BY session_id, ts),
            win AS (
                SELECT session_id, ts, event_id,
                       substr(clean, 1, {PROMPT_PREVIEW_CHARS}) AS preview,
                       length(clean) AS full_len,
                       lead(ts) OVER (PARTITION BY session_id ORDER BY ts) AS next_ts
                FROM turns),
            recent AS ({pick}),
            acts AS (
                SELECT r.event_id, count(*) AS tools,
                       count(*) FILTER (WHERE a.tool_name = 'Bash')     AS bash,
                       count(DISTINCT a.agent_id)                       AS agents,
                       count(*) FILTER (WHERE a.agent_id IS NOT NULL)   AS subcalls
                FROM recent r
                JOIN events a ON a.session_id = r.session_id AND a.type = 'tool_use'
                             AND a.ts >= r.ts
                             AND (r.next_ts IS NULL OR a.ts < r.next_ts)
                GROUP BY r.event_id),
            tok AS (
                SELECT r.event_id, sum(q.output_tokens) AS output_tokens,
                       sum(CASE WHEN q.agent_id IS NOT NULL THEN q.output_tokens
                                ELSE 0 END)             AS subagent_output_tokens,
                       count(*) AS requests, max(q.ts) AS last_ts
                FROM recent r
                JOIN api_requests q ON q.session_id = r.session_id
                                   AND q.ts >= r.ts
                                   AND (r.next_ts IS NULL OR q.ts < r.next_ts)
                GROUP BY r.event_id)
            SELECT r.session_id, r.ts::VARCHAR, r.next_ts::VARCHAR, r.event_id,
                   r.preview, r.full_len,
                   coalesce(a.tools, 0), coalesce(a.bash, 0),
                   coalesce(a.agents, 0), coalesce(a.subcalls, 0),
                   coalesce(t.output_tokens, 0), coalesce(t.subagent_output_tokens, 0),
                   coalesce(t.requests, 0), t.last_ts::VARCHAR, s.project, s.cwd
            FROM recent r
            LEFT JOIN acts a ON a.event_id = r.event_id
            LEFT JOIN tok  t ON t.event_id = r.event_id
            LEFT JOIN sessions s ON s.session_id = r.session_id
            ORDER BY r.ts DESC""",
        [*params, *PROMPT_CLEAN_PARAMS, TURN_NOISE_PATTERN, *pick_params],
    ).fetchall()
    return [dict(zip(_TURN_ROW_KEYS, row)) for row in rows]


def _raw_turn_filter(session_id=None, since=None):
    """`(where, params)` bounding the RAW user turns a turn query may look at.

    One definition for every caller that has to agree about that set: the list's
    scan (`prompt_turns`), the single-turn lookup (`find_turn`) and the CLI's
    count of how many turns a scan could still reach (`_raw_turn_count`). A
    second copy of these three predicates would make "how many are there" and
    "which ones were scanned" drift apart silently.

    The bounds go INSIDE the scan rather than after it, so `session_id=` sees
    that session's newest `raw_scan` turns rather than whatever survives a global
    cut, and `since` costs only turns older than itself (see `prompt_turns`).
    """
    where = ["type = 'user'", "agent_id IS NULL", "ts IS NOT NULL"]
    params = []
    if session_id:
        where.append("session_id = ?")
        params.append(session_id)
    if since:
        where.append("ts >= try_cast(? AS TIMESTAMPTZ)")
        params.append(since)
    return where, params


def prompt_turns(con, limit=PROMPT_TURN_LIMIT, session_id=None,
                 raw_scan=PROMPT_TURN_RAW_SCAN, since=None):
    """One row per user prompt, newest-first, with what happened in its window.

    Only main-thread turns become rows: a subagent's own turns are its task
    prompt, which belongs under its parent's turn rather than next to it. A
    mid-turn user message (Claude Code delivers one as a new `user` event while
    the turn is still running) starts a NEW group - the correct reading of
    "grouped by prompt" rather than pretending turns are atomic.

    Harness-generated turns (`TURN_NOISE_PATTERN`, the SQL twin of
    `TURN_NOISE_RE`) never become rows, and several kept turns sharing one
    `(session_id, ts)` - an image plus its caption, a `<command-name>` block plus
    a bare `/compact` - collapse to the longest cleaned text, because left alone
    they would produce phantom zero-width windows.

    Returns a list of dicts: `session_id, ts, next_ts, event_id, preview,
    full_len, tools, bash, agents, subcalls, output_tokens,
    subagent_output_tokens, requests, last_ts, project, cwd`. Timestamps come
    back as strings (materializing a raw TIMESTAMPTZ needs the optional `pytz`);
    `next_ts` is None for each session's newest turn and `last_ts` None for a
    turn that emitted no API request. Every count coalesces to 0, never None.
    Token numbers are the turn's generation cost by the same attribute-by-time
    rule the `command_tokens` view documents, so a turn whose reply carried no
    usage row honestly reports 0. `cwd` is the session's exact launch dir and
    `project` the lossy dir-name decoding of it (see `_decode_project`), so a
    caller showing a path prefers `cwd`. `cwd` is None in two cases: the
    transcript has left the disk, or it is still there but carries no cwd in its
    head (measured here: 10 of 127, all sub-300-byte stubs). Everything else
    `backfill_session_cwd` fills on the next ingest. Read-only; never raises for a
    caller that already guarded the connection.

    `since` is a lower time bound applied INSIDE the raw scan (`ts >= since`), not
    after it: filtering afterwards would cut the newest-`raw_scan` window down and
    silently return fewer rows than `limit` asked for. Taking the window after the
    bound keeps the suffix-closure argument intact - the next prompt of any kept
    turn is newer than it, so it is fetched too - and the only turns it can cost
    are ones older than the bound, which is what was asked. A value DuckDB cannot
    read as a timestamp matches nothing (`try_cast` -> NULL), so a caller taking
    one from a human should check it first (`cmd_prompts` does).
    """
    where, params = _raw_turn_filter(session_id, since)
    return _turn_rows(con, where, params,
                      f"SELECT * FROM win ORDER BY ts DESC LIMIT {int(limit)}",
                      raw_scan=raw_scan)


def find_turn(con, session_id=None, at=None, event_id=None):
    """ONE turn, resolved by `event_id` or by `(session_id, at)`.

    `event_id` is the id `prompt_turns` puts on the row, so the two compose: list
    the turns, then open one by its id. An id that is not a main-thread user turn,
    or one that LOST the same-timestamp collapse (longest cleaned text wins, ties
    to the highest event_id, see `_turn_rows`), resolves to nothing - it is not a
    turn this model has.

    `at` resolves to the newest turn AT OR BEFORE it, the rule `cat --at` already
    uses, so a time pointing into the middle of a turn finds that turn. Both
    shapes of bound answer correctly, because the two are asking different
    questions:

    - a FULL-PRECISION bound (a row's own `ts`, which `prompts --json` prints and
      `_turn_time` invites pasting back) compares exactly, `ts <= at`;
    - a WHOLE-SECOND bound (the `HH:MM:SS` a rendered list shows) is widened to
      that whole second, because `events.ts` keeps milliseconds: the turn printed
      as `11:17:31` is really `11:17:31.112`, and an exact `<=` would answer that
      printed string with the PREVIOUS turn.

    Truncating the turn's ts for BOTH (what this did before) broke the first case:
    a bound of `11:17:45.671` matched a turn at `11:17:45.777` and returned a
    turn LATER than the one the bound was copied from - measured on a real store
    at 8 of the newest 400 turns and 13 of the newest 200 in its longest session,
    every one of them a `/compact` pair ~100ms apart, each handed the following
    turn's window instead of its own.

    What a whole-second bound cannot do is tell two turns INSIDE one second
    apart: it lands on the newest of them, which is the row the list prints for
    that second. `event_id` is how to name the other one.

    `at` needs `session_id`, because turns from different sessions interleave and
    "the turn at 10:30" is only a question about one session.

    Returns a dict with the same keys as a `prompt_turns` row, or None. The scan
    is NOT capped at `raw_scan`: it is scoped to one session, whose whole turn
    history is a small fraction of the store-wide 5,433 that cap exists for, and
    capping it would put an older turn out of reach. Read-only.
    """
    if event_id:
        # The session first, so the scan below stays scoped to one session; an
        # event that is not a main-thread user turn stops here.
        row = con.execute(
            "SELECT session_id FROM events "
            "WHERE event_id = ? AND type = 'user' AND agent_id IS NULL",
            [event_id],
        ).fetchone()
        if not row or not row[0]:
            return None
        session_id = row[0]
        pick, pick_params = "SELECT * FROM win WHERE event_id = ?", [event_id]
    elif session_id and at:
        # Both rules in one predicate, decided by the BOUND rather than by the
        # caller: the second disjunct is false whenever `at` carries a sub-second
        # part, and when it does not it subsumes the first (date_trunc(ts) <= ts),
        # so each shape of bound reduces to exactly one comparison. Binding the
        # same string four times keeps the decision inside the SQL, where an
        # unreadable `at` still degrades to "no rows" through `try_cast`.
        pick = ("SELECT * FROM win "
                "WHERE ts <= try_cast(? AS TIMESTAMPTZ) "
                "   OR (date_trunc('second', ts) <= try_cast(? AS TIMESTAMPTZ) "
                "       AND try_cast(? AS TIMESTAMPTZ) "
                "           = date_trunc('second', try_cast(? AS TIMESTAMPTZ))) "
                "ORDER BY ts DESC LIMIT 1")
        pick_params = [at, at, at, at]
    else:
        return None
    where, params = _raw_turn_filter(session_id)
    rows = _turn_rows(con, where, params, pick, pick_params)
    return rows[0] if rows else None


def turn_actions(con, session_id, ts, next_ts=None, limit=TURN_ACTION_LIMIT):
    """Every tool call in one turn's window, oldest-first: the detail page's list.

    `ts`/`next_ts` are the strings `prompt_turns` returned for that turn; a
    `next_ts` of None means the open-ended window of a session's newest turn.
    Returns a list of dicts `ts, agent_id, tool_name, text, tool_input,
    is_error`: `agent_id` splits main-thread from subagent calls, `is_error`
    comes from the joined `tool_result` so the detail can mark failed calls, and
    `text` is NULL for tools with no `_COMMAND_FIELD` entry (Agent, Skill, MCP,
    ...), which the caller labels from `tool_input` at render time. A falsy
    `session_id` or `ts` returns [] rather than an unbounded list: without a lower
    bound this is not one turn but the session's whole history. Read-only; never
    raises for a caller that already guarded the connection.
    """
    if not session_id or not ts:
        return []
    # try_cast so an unparseable bound degrades to an empty window rather than
    # raising. `next_ts` is bound conditionally instead of
    # `(?::TIMESTAMPTZ IS NULL OR ...)`: a NULL parameter has no inferable type in
    # a prepared statement.
    where = ["u.type = 'tool_use'", "u.session_id = ?",
             "u.ts >= try_cast(? AS TIMESTAMPTZ)"]
    params = [session_id, ts]
    if next_ts:
        where.append("u.ts < try_cast(? AS TIMESTAMPTZ)")
        params.append(next_ts)
    # Join a result set already collapsed to one row per tool_use_id: a call can
    # carry more than one `tool_result` row (34 tool_use_ids in a real store do),
    # and a plain join would list that call twice, spend two `limit` slots on it
    # and disagree with `prompt_turns.tools`, which counts `events` unjoined.
    # `max(is_error)` reads the duplicates as "any result failed".
    rows = con.execute(
        f"""SELECT u.ts::VARCHAR, u.agent_id, u.tool_name, u.text, u.tool_input,
                   r.is_error
            FROM events u
            LEFT JOIN (SELECT tool_use_id, max(is_error) AS is_error
                       FROM events WHERE type = 'tool_result'
                       GROUP BY tool_use_id) r ON r.tool_use_id = u.tool_use_id
            WHERE {' AND '.join(where)}
            ORDER BY u.ts, u.event_id LIMIT {int(limit)}""",
        params,
    ).fetchall()
    keys = ["ts", "agent_id", "tool_name", "text", "tool_input", "is_error"]
    return [dict(zip(keys, row)) for row in rows]


def turn_prompt_text(con, event_id):
    """The full cleaned text of one turn, for the detail page's prompt view.

    `prompt_turns` caps `preview` at `PROMPT_PREVIEW_CHARS`, so the whole text is
    fetched only when something opens that turn. Returns None for an unknown
    event_id or a turn that cleaned to nothing. Read-only.
    """
    if not event_id:
        return None
    row = con.execute(
        "SELECT text FROM events WHERE event_id = ? AND type = 'user'",
        [event_id],
    ).fetchone()
    if not row:
        return None
    return clean_prompt_text(row[0]) or None


# --- Prompts CLI: the turn model without the TUI (Phase 6) ---------------------
#
# `claude-trail prompts` is the Prompts tab as plain text, over the same
# read-only path as `search` / `tokens`. Two forms: the list, and one turn's tool
# calls (`--at` + `--session`, or `--event-id`). Output is plain `print`, sized
# from its own content and never from the terminal, so it reads the same in a
# pipe as on a tty and `--json` is what a script should parse.

PROMPT_CLI_LIMIT = 30             # list default, same order as `tokens`' 30
PROMPT_CLI_SESSION_LEN = 8        # short session id, as `search`/`tokens` print it
PROMPT_CLI_AGENT_ID_LEN = 4       # short agent id, the TUI's AGENT_ID_LABEL_LEN
PROMPT_CLI_PROJECT_WIDTH = 20     # the Project cell, shortened like the TUI's
PROMPT_CLI_PREVIEW_CHARS = 100    # the one-line Prompt cell; --json keeps all 400
PROMPT_CLI_PROMPT_LINES = 10      # prompt lines quoted by the single-turn header

_PROMPTS_USAGE = (
    "usage: claude-trail prompts [--session S] [--limit N] [--since ISO] [--json]\n"
    "       claude-trail prompts --at ISO --session S [--json]\n"
    "       claude-trail prompts --event-id E [--json]")

# flag -> option key. Every one of them takes a value; `--json` and `-h/--help`
# are the only bare flags.
_PROMPTS_VALUE_FLAGS = {"--session": "session", "--limit": "limit",
                        "--since": "since", "--at": "at", "--event-id": "event_id"}

_PROMPT_CLI_HEADERS = ("time", "sess", "project", "calls", "agents", "tokens",
                       "prompt")


def _parse_prompts_args(args):
    """`prompts`' argv tail as an options dict, or `(None, message)`.

    Hand-rolled like the rest of this file's CLI (there is no argparse anywhere
    in it), but strict where the older parsers are permissive: an unknown flag is
    an error instead of being ignored, because `--sesion 29dea45c` quietly listing
    every session is worse than a usage line.
    """
    opts = {"help": False, "json": False, "session": None, "limit": None,
            "since": None, "at": None, "event_id": None}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-h", "--help"):
            opts["help"] = True
            return opts, None
        if arg == "--json":
            opts["json"] = True
            i += 1
            continue
        key = _PROMPTS_VALUE_FLAGS.get(arg)
        if key is None:
            return None, f"unknown argument: {arg}"
        if i + 1 >= len(args):
            return None, f"{arg} needs a value"
        opts[key] = args[i + 1]
        i += 2
    if opts["at"] and opts["event_id"]:
        return None, "--at and --event-id both select one turn; pass just one"
    if opts["event_id"] and opts["session"]:
        # `find_turn` reads the session off the event, so accepting both would
        # let `--session A --event-id <an id from B>` dump B's turn under A's
        # name without a word.
        return None, ("--event-id names its own session; drop --session "
                      "(use --session with --at to select by time)")
    if opts["at"] and not opts["session"]:
        return None, "--at needs --session: turns from different sessions interleave"
    # The list's bounds against the one-turn forms: the usage block already keeps
    # them on separate lines, and silently dropping them would let a scripted
    # caller believe `--since` narrowed a dump it cannot narrow.
    if opts["at"] or opts["event_id"]:
        given = [flag for flag, key in (("--since", "since"), ("--limit", "limit"))
                 if opts[key] is not None]
        if given:
            return None, (f"{' and '.join(given)} bound the LIST; "
                          "--at/--event-id already select one turn")
    if opts["limit"] is None:
        opts["limit"] = PROMPT_CLI_LIMIT
        return opts, None
    try:
        opts["limit"] = max(1, int(opts["limit"]))
    except ValueError:
        return None, f"--limit wants a number, got {opts['limit']!r}"
    return opts, None


def _short_project_path(path):
    """`short_path(path, PROMPT_CLI_PROJECT_WIDTH)`, from the TUI module.

    The ONE helper this CLI still reads upwards, because its default width comes
    from the `.env` column machinery that lives up there (`_env_int` /
    `DIR_LABEL_WIDTH`) and moving the function down would drag that with it. The
    width is passed explicitly, so nothing printed here depends on the user's
    column settings; the other shared text helpers (`action_label`,
    `short_tool_name`, `slash_label`) live in this module and the TUI imports
    them.

    Taken from `sys.modules` first: `claude_trail.py` run straight from a clone
    IS `__main__`, and a plain `import claude_trail` would then execute that whole
    module a second time just to reach one function.
    """
    for name in ("claude_trail", "__main__"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "short_path"):
            return mod.short_path(path, PROMPT_CLI_PROJECT_WIDTH)
    import claude_trail
    return claude_trail.short_path(path, PROMPT_CLI_PROJECT_WIDTH)


def _turn_time(ts, time_only=False):
    """`YYYY-MM-DD HH:MM:SS` (or just `HH:MM:SS`) out of a `ts::VARCHAR`.

    Sliced, not parsed: DuckDB already renders a TIMESTAMPTZ as
    `2026-07-27 10:30:38+02` (sub-second when it has one), so the head is the text
    to print - and `datetime.fromisoformat` would reject that 2-digit offset on
    Python 3.10, the trap the plan records against the TUI's `format_time`.
    Printing the whole date is what makes `--at` chainable: copy a list row's
    time, paste it back.
    """
    text = (ts or "").strip()
    return text[11:19] if time_only else text[:19]


# Terminal control characters, stripped from every piece of STORED text this CLI
# prints. A prompt is the likeliest place in the whole store to find pasted
# terminal output, and a `\x1b[2J` or a `\x07` in one would be obeyed by the
# terminal reading the list rather than shown in it (the TUI is safe here only
# because Rich escapes for it). Tab becomes a space so a cell's printed width
# stays its `len`, which is what `_print_columns` pads by; every other C0 control
# and DEL is dropped. `--json` needs none of this: `json.dumps` escapes controls
# to `\uXXXX`.
_PLAIN_TEXT_MAP = {c: None for c in (*range(0x20), 0x7f)}
_PLAIN_TEXT_MAP[0x09] = " "


def _plain_text(line):
    """One LINE of stored text with its control characters removed.

    Per line, after the split: the map deletes `\\n` and `\\r` too, so running it
    over a whole prompt would run its lines together.
    """
    return line.translate(_PLAIN_TEXT_MAP)


def _turn_preview_line(turn):
    """The turn's first non-blank line for the list's Prompt cell.

    `slash_label` here and not in the query, for the same reason the TUI applies
    it at render time: it collapses a whole `<command-name>` block to `/compact`,
    right for a one-line cell and wrong for the full text. Capped at
    PROMPT_CLI_PREVIEW_CHARS so twenty rows stay readable; `--json` carries the
    query's full 400-char preview. The cut is marked with the `…` that
    `short_path` puts in the Project cell of the same row, so one list row does
    not spell "shortened" two ways.
    """
    label = slash_label(turn["preview"] or "")
    lines = (_plain_text(ln).strip() for ln in label.splitlines())
    line = next((ln for ln in lines if ln), "")
    if not line:
        return "(empty)"
    if len(line) > PROMPT_CLI_PREVIEW_CHARS:
        return line[:PROMPT_CLI_PREVIEW_CHARS - 1] + "…"
    return line


def _turn_list_cells(turn):
    """One list row, in `_PROMPT_CLI_HEADERS` order.

    Same numbers the Prompts tab shows: `calls` is the window-wide total with its
    Bash share (a turn that ran nothing is a real turn and gets `-`, not a `0`
    that reads as missing data), `agents` is blank when none ran, and `tokens` is
    the window's output with the subagent part called out. Counts are printed
    unabbreviated (the TUI's `24.2k` is a width compromise a CLI does not need)
    and without thousands separators, so the column stays easy to cut and sort.
    """
    calls = "-"
    if turn["tools"]:
        calls = str(turn["tools"])
        if turn["bash"]:
            calls += f" ({turn['bash']} bash)"
    tokens = str(turn["output_tokens"])
    if turn["subagent_output_tokens"]:
        tokens += f" (+{turn['subagent_output_tokens']} sub)"
    return (
        _turn_time(turn["ts"]),
        (turn["session_id"] or "")[:PROMPT_CLI_SESSION_LEN],
        # cwd is the session's exact launch dir, `project` the lossy dir-name
        # decoding behind it; the TUI prefers the same way round.
        _short_project_path(turn["cwd"] or turn["project"] or ""),
        calls,
        str(turn["agents"]) if turn["agents"] else "",
        tokens,
        _turn_preview_line(turn),
    )


def _print_columns(rows):
    """Print `rows` (tuples of str) left-aligned, padding each column to the
    widest cell in it.

    Width comes from the CONTENT, never from the terminal: the same rows print
    identically on a tty and through a pipe, and nothing is truncated to fit a
    width the caller cannot see. The last column is therefore never padded.
    """
    if not rows:
        return
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width)
                        for cell, width in zip(row, widths)).rstrip())


def _turn_work_text(turn):
    """The Work line in plain text, wording copied from the TUI's turn page.

    `tools` is INCLUSIVE of `subcalls`, so the main-thread count is a
    SUBTRACTION; adding them would claim 105 calls for a turn that made 64. The
    Bash count is window-wide (subagents included), so it stands on its own
    rather than nested under the main-thread count.
    """
    if not turn["tools"]:
        return "no tool calls"
    text = f"{turn['tools']} tool call{'' if turn['tools'] == 1 else 's'}"
    parts = []
    if turn["subcalls"]:
        main = max(0, turn["tools"] - turn["subcalls"])
        agents = turn["agents"]
        parts.append(f"{main} main, {turn['subcalls']} across "
                     f"{agents} subagent{'' if agents == 1 else 's'}")
    if turn["bash"]:
        parts.append(f"{turn['bash']} bash")
    return text + (": " + ", ".join(parts) if parts else "")


def _turn_tokens_text(turn):
    """The Tokens line: the window's generation cost and what it took.

    `requests == 0` means no API request was attributed to the window, normally
    because the ingest has not caught up with the turn yet - said in words rather
    than printed as a bare `0 output`, which would read as a measurement.
    """
    if not turn["requests"]:
        return "no requests attributed to this window yet (ingest lags the turn)"
    parts = [f"{turn['output_tokens']} output"]
    if turn["subagent_output_tokens"]:
        parts.append(f"{turn['subagent_output_tokens']} by subagents")
    parts.append(f"{turn['requests']} request"
                 f"{'' if turn['requests'] == 1 else 's'}")
    return ", ".join(parts)


def _print_turn_prompt(turn):
    """Quote the turn's prompt under the single-turn header.

    The row's own preview, not `turn_prompt_text`: the preview is capped at 400
    chars by the query while a real turn can run to 646,245, and a dump is not
    the place to discover that. Both cuts (lines beyond
    PROMPT_CLI_PROMPT_LINES, chars beyond the preview) are stated rather than
    silent.
    """
    preview = turn["preview"] or ""
    # `prompt_turns` drops a turn whose cleaned text is empty, so the placeholder
    # is a guard, not a case: it keeps the label from standing over nothing.
    lines = preview.splitlines() or ["(empty)"]
    shown = [_plain_text(line) for line in lines[:PROMPT_CLI_PROMPT_LINES]]
    for idx, line in enumerate(shown):
        label = "prompt" if idx == 0 else ""
        print(f"{label:<9} {line}".rstrip() if line else label)
    notes = []
    if len(lines) > len(shown):
        notes.append(f"{len(lines) - len(shown)} more lines")
    dropped = max(0, (turn["full_len"] or 0) - len(preview))
    if dropped:
        notes.append(f"{dropped} more chars")
    if notes:
        print(f"{'':<9} [+{', '.join(notes)}]")


def _print_turn_detail(turn, actions):
    """One turn's header plus its tool calls, oldest-first.

    Oldest-first is the order the work happened in and the order `turn_actions`
    returns, which is what makes the dump readable as a story. The Agent cell is
    `main` or the subagent's short id (the TUI draws a tree gutter for the same
    thing; a flat column greps better). A failed call is marked at the END of the
    line, so the Detail column keeps its alignment and `grep '\\[error\\]'` finds
    every one. A turn that ran nothing gets the sentence and no table: a header
    over no rows reads as a truncated dump.
    """
    project = turn["cwd"] or turn["project"] or ""
    sent = _turn_time(turn["ts"])
    if turn["last_ts"]:
        # The last request the store attributed to the window, not `next_ts`:
        # next_ts is when the user next typed, so a turn answered in seconds and
        # left on screen overnight would claim fourteen hours of work.
        sent += f"   last request {_turn_time(turn['last_ts'])}"
    head = [("session", f"{turn['session_id']}  {project}".rstrip()),
            ("sent", sent),
            ("work", _turn_work_text(turn)),
            ("tokens", _turn_tokens_text(turn)),
            ("event-id", turn["event_id"])]
    for label, value in head:
        print(f"{label:<9} {value}")
    _print_turn_prompt(turn)
    print()
    if not actions:
        print("(no tool calls in this turn)")
        return
    rows = [("time", "agent", "tool", "detail")]
    for action in actions:
        label = action_label(action["tool_name"], action["text"],
                             action["tool_input"])
        detail = " ".join(_plain_text(label).split())
        if action["is_error"]:
            detail = f"{detail or '-'}  [error]"
        rows.append((
            _turn_time(action["ts"], time_only=True),
            # The same first-N slice the TUI's `short_agent_id` makes, taken here
            # rather than out of the TUI: N is that module's own label constant,
            # and one number is not worth an upward import.
            (action["agent_id"] or "")[:PROMPT_CLI_AGENT_ID_LEN] or "main",
            short_tool_name(action["tool_name"] or "?"),
            detail or "-",
        ))
    _print_columns(rows)


def _castable_ts(con, value):
    """True when DuckDB reads `value` as a timestamp. Asked BEFORE the query so a
    typo in `--since`/`--at` is a message rather than a silently empty result
    (both bind through `try_cast`, which turns an unreadable value into NULL and
    therefore into "no rows")."""
    return bool(con.execute("SELECT try_cast(? AS TIMESTAMPTZ) IS NOT NULL",
                            [value]).fetchone()[0])


def _resolve_session_prefix(con, prefix):
    """The full session id a `--session` prefix names, or `(None, message)`.

    `search` and `tokens` match a session prefix with LIKE, and a user reading the
    8-char `sess` column has only a prefix to give back. `prompt_turns` itself
    takes an EXACT id (the TUI always holds one), so the prefix is resolved here
    instead of loosening the query. An unknown prefix comes back unchanged and the
    query returns nothing, which is the honest answer for a session the store
    never saw.
    """
    ids = [row[0] for row in con.execute(
        "SELECT session_id FROM sessions WHERE session_id LIKE ? ORDER BY session_id",
        [prefix + "%"]).fetchall()]
    if prefix in ids or not ids:
        return prefix, None
    if len(ids) > 1:
        sample = ", ".join(i[:PROMPT_CLI_SESSION_LEN] for i in ids[:3])
        return None, (f"--session {prefix} matches {len(ids)} sessions "
                      f"({sample}, ...); use more characters")
    return ids[0], None


def _raw_turn_count(con, session_id=None, since=None):
    """How many raw user turns a list scan could reach at most, under the same
    bounds the scan itself applies (`_raw_turn_filter`). One cheap count, so
    `_list_turns` knows when widening the scan has stopped buying anything."""
    where, params = _raw_turn_filter(session_id, since)
    return con.execute(
        f"SELECT count(*) FROM events WHERE {' AND '.join(where)}",
        params).fetchone()[0]


def _list_turns(con, limit, session_id=None, since=None):
    """`prompt_turns`, scanned until `limit` rows come back or the store runs out.

    `raw_scan` bounds RAW user turns and the noise filter drops them AFTER the
    scan, so no single guess at the scan size makes `--limit` mean what it says:
    on the live store roughly half of the raw turns are harness noise, and a
    fixed 2x scan for `--limit 500` came back with 466 rows. A scan cannot keep
    more turns than it scanned, so the first pass starts at `limit` (never below
    PROMPT_TURN_RAW_SCAN, whose cost is the measured one) and each further pass
    doubles towards the raw count. The count is the stop condition: once the scan
    covers every candidate turn, a short list is the honest answer rather than a
    scan-size artifact.

    Widening rather than scanning everything up front is the point: an uncapped
    clean of a real store's 5,433 main-thread turns costs 1.96s against 74ms for
    the newest 400, and almost every call is a small `--limit` that the first
    pass already fills.
    """
    total = _raw_turn_count(con, session_id, since)
    if not total:
        return []
    scan = min(total, max(PROMPT_TURN_RAW_SCAN, limit))
    while True:
        turns = prompt_turns(con, limit=limit, session_id=session_id,
                             since=since, raw_scan=scan)
        if len(turns) >= limit or scan >= total:
            return turns
        scan = min(total, scan * 2)


def _run_prompts(con, opts):
    """`cmd_prompts` with the store already open (so the open/close stays in one
    place and every exit path closes it)."""
    session_id = opts["session"]
    if session_id:
        session_id, err = _resolve_session_prefix(con, session_id)
        if err:
            print(err, file=sys.stderr)
            return 2
    for flag in ("since", "at"):
        if opts[flag] and not _castable_ts(con, opts[flag]):
            print(f"--{flag} wants a timestamp, got {opts[flag]!r}", file=sys.stderr)
            return 2
    if opts["at"] or opts["event_id"]:
        turn = find_turn(con, session_id=session_id, at=opts["at"],
                         event_id=opts["event_id"])
        if turn is None:
            if opts["event_id"]:
                print(f"no turn with event-id {opts['event_id']}; "
                      "`prompts --json` prints each turn's event_id",
                      file=sys.stderr)
            else:
                print(f"no turn at or before {opts['at']} in session "
                      f"{session_id[:PROMPT_CLI_SESSION_LEN]}", file=sys.stderr)
            return 1
        actions = turn_actions(con, turn["session_id"], turn["ts"], turn["next_ts"])
        if opts["json"]:
            # No `default=`: every value a turn row and `turn_actions` carry is
            # already str/int/bool/None (both timestamps come back ::VARCHAR), so
            # a fallback stringifier could only hide a query that started
            # returning something else. The `--json` tests are what pin that.
            print(json.dumps({**turn, "actions": actions}, indent=2))
        else:
            _print_turn_detail(turn, actions)
        return 0
    turns = _list_turns(con, opts["limit"], session_id, opts["since"])
    if opts["json"]:
        print(json.dumps(turns, indent=2))
    else:
        _print_columns([_PROMPT_CLI_HEADERS,
                        *(_turn_list_cells(t) for t in turns)])
    return 0


def cmd_prompts(argv):
    """`claude-trail prompts`: the Prompts tab's list, and one turn's tool calls.

    The CLI twin of the TUI tab, over the same read-only store open, so the turn
    model has coverage that does not depend on a terminal. Exit codes follow the
    other read commands: 2 for an argument the parser refuses, 1 for a store that
    is not there (or is held by an ingest, which `cli_main` catches), 0 otherwise
    - including an empty list, which is an answer, not a failure.
    """
    opts, err = _parse_prompts_args(argv[1:])
    if opts is None:
        print(err, file=sys.stderr)
        print(_PROMPTS_USAGE, file=sys.stderr)
        return 2
    if opts["help"]:
        print(_PROMPTS_USAGE)
        return 0
    if not STORE_PATH.exists():
        # Opening read-only would raise on a missing file (`cmd_stats` guards the
        # same way); say where it would live and how to make one.
        print(f"no audit store at {STORE_PATH} yet - run: claude-trail ingest",
              file=sys.stderr)
        return 1
    con = connect(read_only=True)
    try:
        return _run_prompts(con, opts)
    finally:
        con.close()


def session_token_totals(con, session_id):
    """Exact per-session token totals from the `session_tokens` view (subagent
    spend included, since subagent transcripts ingest under the parent sessionId).

    Returns ``{"requests", "output_tokens", "input_tokens", "cache_read_tokens",
    "cache_creation_tokens", "subagent_output_tokens"}`` (Nones coalesced to 0) or
    None when the session has no ingested API requests. Used by the TUI
    session-detail modal's Tokens total. Read-only.
    """
    if not session_id:
        return None
    row = con.execute(
        """SELECT requests, output_tokens, input_tokens,
                  cache_read_tokens, cache_creation_tokens, subagent_output_tokens
           FROM session_tokens WHERE session_id = ?""",
        [session_id],
    ).fetchone()
    if not row:
        return None
    keys = ["requests", "output_tokens", "input_tokens",
            "cache_read_tokens", "cache_creation_tokens", "subagent_output_tokens"]
    return {k: (v or 0) for k, v in zip(keys, row)}


def db_info(con):
    """DB properties + statistics as an ordered list of (label, value) string
    pairs, shared by the `stats` CLI section and the TUI's database modal.

    Read-only and safe on a not-yet-migrated schema: every optional table and
    pragma is guarded, so an older (schema 1) or empty store degrades to the
    subset it can answer rather than raising.
    """
    out = [
        ("connection", str(STORE_PATH)),
        ("source", "$CLAUDE_TRAIL_DB" if "CLAUDE_TRAIL_DB" in os.environ
         else "default (~/.claude/trail.duckdb)"),
    ]
    try:
        out.append(("duckdb", con.execute("SELECT version()").fetchone()[0]))
    except Exception:
        pass
    try:
        sv = con.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
        out.append(("schema version", sv[0] if sv else "?"))
    except Exception:
        pass
    try:
        out.append(("file size", _fmt_bytes(STORE_PATH.stat().st_size)))
    except OSError:
        pass
    walp = Path(str(STORE_PATH) + ".wal")
    if walp.exists():
        out.append(("wal size", _fmt_bytes(walp.stat().st_size)))
    # On-disk block accounting; PRAGMA columns vary by version, so map by name.
    try:
        row = con.execute("PRAGMA database_size").fetchone()
        d = dict(zip([c[0] for c in con.description], row))
        if d.get("database_size") is not None:
            out.append(("db size", str(d["database_size"])))
        if d.get("block_size") is not None:
            out.append(("block size", _fmt_bytes(d["block_size"])))
        if d.get("used_blocks") is not None and d.get("total_blocks") is not None:
            out.append(("blocks used/total", f"{d['used_blocks']}/{d['total_blocks']}"))
        if d.get("free_blocks") is not None:
            out.append(("free blocks", str(d["free_blocks"])))
    except Exception:
        pass
    # Per-table row counts, only for tables that exist (fresh/older schema safe).
    try:
        present = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main'").fetchall()}
        for t in _STORE_TABLES:
            if t in present:
                out.append((f"rows {t}",
                            f"{con.execute(f'SELECT count(*) FROM {t}').fetchone()[0]:,}"))
    except Exception:
        pass
    return out


def cmd_stats(argv):
    if not STORE_PATH.exists():
        # Opening read-only would raise on a missing file, so report where the
        # store would live and how to create it instead of crashing.
        print("== database ==")
        print("   connection        ", str(STORE_PATH))
        print("   source            ",
              "$CLAUDE_TRAIL_DB" if "CLAUDE_TRAIL_DB" in os.environ
              else "default (~/.claude/trail.duckdb)")
        print("   status             not created yet; run `claude-trail ingest`")
        return 0
    con = connect(read_only=True)
    try:
        print("== database ==")
        for label, value in db_info(con):
            print(f"   {label:<18} {value}")
        for label, q in [
            ("sessions", "SELECT count(*) FROM sessions"),
            ("events",   "SELECT count(*) FROM events"),
            ("by tool",  "SELECT tool_name, count(*) c FROM events WHERE tool_name IS NOT NULL GROUP BY 1 ORDER BY c DESC LIMIT 10"),
            ("file versions", "SELECT count(*) FROM file_versions"),
            ("blobs / bytes", "SELECT count(*), coalesce(sum(size),0) FROM blobs"),
            ("api requests / output tokens", "SELECT count(*), coalesce(sum(output_tokens),0) FROM api_requests"),
        ]:
            print(f"== {label} ==")
            for r in con.execute(q).fetchall():
                print("  ", *r)
        # Reconstruction coverage (Phase 8). Guarded via information_schema so a
        # not-yet-migrated 0.6.0 DB opened read-only (no `source` column / no
        # file_history table) does not raise a binder error: readers never run DDL.
        fv_cols = {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='file_versions'").fetchall()}
        has_fh = con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name='file_history'").fetchone()[0] > 0
        if "source" in fv_cols:
            print("== reconstruction coverage (cat-able paths) ==")
            total, catable = con.execute(
                "SELECT count(DISTINCT path), "
                "count(DISTINCT path) FILTER (WHERE post_sha IS NOT NULL) "
                "FROM file_versions WHERE path IS NOT NULL").fetchone()
            print("   cat-able / edited paths ", catable, "/", total)
            for src, c in con.execute(
                    "SELECT coalesce(source,'transcript'), count(DISTINCT path) "
                    "FROM file_versions WHERE post_sha IS NOT NULL AND path IS NOT NULL "
                    "GROUP BY 1 ORDER BY 1").fetchall():
                print(f"     via {src} ", c)
        if has_fh:
            print("== file history ==")
            rows, hashes, resolved, unresolved = con.execute(
                "SELECT count(*), count(DISTINCT pathhash), "
                "count(*) FILTER (WHERE path IS NOT NULL), "
                "count(*) FILTER (WHERE path IS NULL) FROM file_history").fetchone()
            fh_bytes = con.execute(
                "SELECT coalesce(sum(b.size),0) FROM file_history fh "
                "JOIN blobs b ON b.sha=fh.sha").fetchone()[0]
            print("   snapshots / distinct paths ", rows, "/", hashes)
            print("   resolved / unresolved rows ", resolved, "/", unresolved)
            print("   snapshot bytes ", fh_bytes)
    finally:
        con.close()
    return 0


# --- Reconstruction CLI: cat / diff (Task 6) ----------------------------------


def cmd_cat(argv):
    # usage: cat <path> [--session S] [--at ISO]  -> latest content at/before --at.
    # Two steps. (1) The newest version with reconstructable bytes: the JOIN on
    # post_sha skips patch-only versions (null post_sha). (2) Any NEWER patch-only
    # edits are rolled forward on top via strict structuredPatch application --
    # file-history snapshots checkpoint the PRE-turn state, so a file last edited
    # in a session's final turn had no snapshot of its final bytes and `cat` used
    # to be one turn stale here. A patch that no longer applies cleanly (file
    # changed outside Claude, interleaved session) stops the roll-forward:
    # stale-but-consistent bytes plus a stderr warning, never a corrupted mix.
    args = argv[1:]
    path = args[0]
    def opt(n):
        return args[args.index(n) + 1] if n in args and args.index(n) + 1 < len(args) else None
    where, params = ["fv.path = ?"], [path]
    if opt("--session"): where.append("fv.session_id LIKE ?"); params.append(opt("--session") + "%")
    if opt("--at"):      where.append("fv.ts <= ?");           params.append(opt("--at"))
    # epoch_ms + rowid anchor the tail strictly after the base row (rowid breaks
    # same-millisecond ties in insertion = transcript order); the raw TIMESTAMPTZ
    # is never fetched because materializing it needs the optional pytz package.
    sql = (f"SELECT b.content, epoch_ms(fv.ts), fv.rowid "
           f"FROM file_versions fv JOIN blobs b ON b.sha = fv.post_sha "
           f"WHERE {' AND '.join(where)} ORDER BY fv.ts DESC, fv.rowid DESC LIMIT 1")
    con = connect(read_only=True)
    try:
        row = con.execute(sql, params).fetchone()
        if not row:
            print("no stored version for that path/time")
            return 1
        content, base_ms, base_rowid = row
        # The patch-only tail: same filters, strictly after the base row, oldest
        # first. Every row here has a null post_sha by construction (a later row
        # WITH bytes would itself have been picked as the base above).
        tail_sql = (f"SELECT fv.patch FROM file_versions fv "
                    f"WHERE {' AND '.join(where)} AND (epoch_ms(fv.ts) > ? OR "
                    f"(epoch_ms(fv.ts) = ? AND fv.rowid > ?)) "
                    f"ORDER BY fv.ts, fv.rowid")
        tail = con.execute(tail_sql, params + [base_ms, base_ms, base_rowid]).fetchall()
    finally:
        con.close()
    for i, (patch,) in enumerate(tail):
        patched = _apply_structured_patch(content, patch) if patch else None
        if patched is None:
            print(f"warning: output is stale by {len(tail) - i} edit(s): "
                  f"a later patch no longer applies cleanly", file=sys.stderr)
            break
        content = patched
    print(content, end="")
    return 0


def cmd_diff(argv):
    # usage: diff <path> [--session S] [--all-sources]
    # Defaults to transcript-derived rows only (source IS DISTINCT FROM
    # 'file-history', which also keeps pre-migration NULL-source rows). File-history
    # snapshot deltas are hidden by default because a snapshot spans a whole user
    # turn, so including them double-lists every edit and, for the headless first
    # snapshot of a group (B1), spams '(no patch)'. --all-sources opts them in, and
    # then rows with a NULL patch are skipped to keep the output readable.
    args = argv[1:]
    path = args[0]
    all_sources = "--all-sources" in args
    def opt(n):
        return args[args.index(n) + 1] if n in args and args.index(n) + 1 < len(args) else None
    where, params = ["path = ?"], [path]
    if opt("--session"): where.append("session_id LIKE ?"); params.append(opt("--session") + "%")
    con = connect(read_only=True)
    try:
        # Guard the source filter behind the same information_schema check cmd_stats
        # uses: a not-yet-migrated 0.6.0 DB opened read-only has no `source` column,
        # and referencing it would raise a binder error (readers never run DDL, so
        # the column stays absent until a writable ingest). A pre-migration DB holds
        # only transcript rows, so skipping the filter is semantically identical.
        has_source = "source" in {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='file_versions'").fetchall()}
        if has_source and not all_sources:
            where.append("source IS DISTINCT FROM 'file-history'")
        # strftime(ts) instead of raw ts: DuckDB needs the optional `pytz` package to
        # materialize a TIMESTAMPTZ into a Python datetime, so fetching the raw column
        # crashes on a clean install without pytz. The header only needs the string.
        sql = (f"SELECT strftime(ts,'%Y-%m-%d %H:%M:%S'), substr(session_id,1,8), patch "
               f"FROM file_versions WHERE {' AND '.join(where)} ORDER BY ts")
        for ts, sid, patch in con.execute(sql, params).fetchall():
            if all_sources and patch is None:
                continue                     # skip headless/NULL-patch file-history rows
            print(f"=== {ts}  {sid} ===\n{patch or '(no patch)'}\n")
    finally:
        con.close()
    return 0


def cli_main(argv):
    cmd = argv[0]
    fn = {"ingest": cmd_ingest, "search": cmd_search, "stats": cmd_stats,
          "tokens": cmd_tokens, "prompts": cmd_prompts, "cat": cmd_cat,
          "diff": cmd_diff, "reset-db": cmd_reset_db,
          "prune-raw": cmd_prune_raw}[cmd]
    try:
        return fn(argv)
    except Exception as e:
        # The ingest holds the store for its run, and DuckDB's lock excludes
        # readers outright. With an ingest every 30s a read command lands on one
        # often enough that a raw traceback would read as a broken store; say
        # what happened and let the caller retry. Non-lock errors still raise.
        if not is_lock_conflict(e):
            raise
        print(f"store busy: an ingest is writing {STORE_PATH}; try again in a moment",
              file=sys.stderr)
        return 1
