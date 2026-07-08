"""DuckDB-backed audit trail store for Claude Code sessions.

Ingests transcripts (~/.claude/projects/**/*.jsonl) and file snapshots into a
single local DuckDB file; provides substring/regex search and file
reconstruction. Single-writer ingester, read-only readers.
"""
import os
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
