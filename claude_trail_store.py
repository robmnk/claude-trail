"""DuckDB-backed audit trail store for Claude Code sessions.

Ingests transcripts (~/.claude/projects/**/*.jsonl) and file snapshots into a
single local DuckDB file; provides substring/regex search and file
reconstruction. Single-writer ingester, read-only readers.
"""
import difflib
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
        if ev.get("toolUseResult") is not None:  # edit/write results -> file version + blobs
            fv = file_version_from_event(con, ev)
            if fv:
                fvs.append(fv)
    if raw_rows:
        _insert_raw(con, raw_rows)            # verbatim, always
        _upsert_session(con, path, raw_rows)
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
    }


_FV_COLS = ["event_id", "session_id", "ts", "path", "pre_sha", "post_sha", "patch"]


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


def cmd_ingest(argv):
    watch = "--watch" in argv
    # --compact rewrites the store to reclaim free blocks after the sweep; it is a
    # one-shot maintenance step, so it is meaningless (and skipped) under --watch.
    compact = "--compact" in argv and not watch
    if "--compact" in argv and watch:
        print("note: --compact is ignored with --watch (never compact inside the poll loop)")
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
        # Flush the WAL into the main file on exit (one-shot end, or --watch
        # shutdown), never per-iteration. Note: CHECKPOINT does NOT reclaim
        # DuckDB's interspersed free blocks, so the file stays ~2x its packed
        # size after a bulk ingest; the full rewrite that does reclaim them is
        # compact_db(), run below (non-watch only) after the write lock is freed.
        con.execute("CHECKPOINT")
        con.close()
    if compact:  # after con.close(), so the store file is unlocked for the rewrite
        old, new = compact_db()
        print(f"compacted {old // (1024 * 1024)}MB -> {new // (1024 * 1024)}MB")
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
    if opt("--project"): where.append("session_id IN (SELECT session_id FROM sessions WHERE project LIKE ?)"); params.append("%"+opt("--project")+"%")
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


def cmd_stats(argv):
    con = connect(read_only=True)
    try:
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
    finally:
        con.close()
    return 0


def cmd_cat(argv):
    # Stub until Phase 6 (file reconstruction over file_versions/blobs). Kept here
    # only so cli_main's dispatch dict resolves; Phase 6 replaces it with the real
    # `cat <path>` reconstruction command.
    print("cat: file reconstruction is not available until phase 6")
    return 0


def cmd_diff(argv):
    # Stub until Phase 6 (file reconstruction over file_versions/blobs). Kept here
    # only so cli_main's dispatch dict resolves; Phase 6 replaces it with the real
    # `diff <path>` reconstruction command.
    print("diff: file reconstruction is not available until phase 6")
    return 0


def cli_main(argv):
    cmd = argv[0]
    return {"ingest": cmd_ingest, "search": cmd_search, "stats": cmd_stats,
            "tokens": cmd_tokens, "cat": cmd_cat, "diff": cmd_diff}[cmd](argv)
