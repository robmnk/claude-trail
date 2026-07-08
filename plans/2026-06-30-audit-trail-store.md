# Audit Trail Store (DuckDB) Implementation Plan

> **For agentic workers:** implement task-by-task in order. Steps use checkbox (`- [ ]`) syntax for tracking. Each task ends with a verification step and a commit. Phases 1 and 2 are independently shippable.

**Goal:** A local, queryable, well-compressed audit trail of every Claude Code session: prompts, assistant messages, every tool call with its input and result, the diffs/snapshots needed to reconstruct any edited file, and per-API-request token usage so spend can be analyzed per session and per command. Searchable by substring/regex from the CLI.

**Key insight (drives the whole design):** the data already exists on disk. We ingest, we do not capture.
- Transcripts `~/.claude/projects/**/<session>.jsonl` (484 MB, 1405 sessions): user/assistant messages, tool calls (`tool_use`), `toolUseResult` (stdout/stderr for Bash, `structuredPatch` for edits), and per-request token usage (`message.usage` + top-level `requestId` on every assistant line).
- Subagent transcripts `~/.claude/projects/<project>/<sid>/subagents/agent-<id>.jsonl` (329 files): same event shape, and they carry the parent `sessionId` inside. Their tool calls and token usage belong to the session, so they must be ingested too (agent-heavy sessions spend most of their tokens here).
- `~/.claude/file-history/<uuid>/<pathhash>@vN` (51 MB): full-content, versioned file snapshots.
- `~/.claude/history.jsonl` (1.5 MB): prompt history with project + timestamp.

The existing PostToolUse hook (`claude-trail hook`, Bash-only) and the TUI are unchanged. The store reads transcripts, so it covers every tool and gets results + patches for free.

**Architecture:**
```
transcripts + file-history  ──►  claude-trail ingest  ──►  ~/.claude/trail.duckdb  ──►  search/tokens/cat/diff (read-only)
        (source files)          (single writer, incremental)      (one file)
```
A single ingester process holds DuckDB's one read-write connection. Search/stats/tokens/cat/diff and the TUI open the file read-only. Do not write to DuckDB from the hook (many concurrent short-lived hook processes would contend on the lock).

**Decisions (locked):** DuckDB single file. Search = substring/regex full scan (no FTS index). File reconstruction = self-contained content-addressed blob store (copy snapshots in, deduped by sha256), seeded from `file-history` + `structuredPatch`.

**Tech stack:** Python 3.11+, `duckdb>=1.0` (new dep), stdlib `hashlib`/`difflib`/`json`. Rich (already a dep) for CLI output. No `zstandard`: DuckDB compresses columns on disk and content-addressing removes the large redundancy.

**Scope:**
- In: new module `claude_trail_store.py`; subcommand dispatch in `claude_trail.py main()`; `pyproject.toml` dep; `tests/`; docs.
- Out (this plan): a long-running daemon/service unit (we ship `--watch` polling instead), encryption at rest, multi-machine sync, FTS/BM25 ranking, TUI search view (deferred Task 8), dollar cost estimation (pricing tables drift; store token counts and price them at query time).

**Non-goals:** capturing anything via new hooks; modifying the existing TUI or hook behavior; deleting or rotating the source transcripts.

**Risk / unknowns:**
- The `file-history` mapping (how `<pathhash>@vN` ties to a session + real path + order) and the exact location of `structuredPatch` inside `toolUseResult` are undocumented. Task 5 starts with an investigation step and has a transcript-only fallback (Write content + apply structuredPatch) so reconstruction works even if file-history mapping is not cracked.
- Transcript schema may vary across Claude Code versions. Parser must be defensive: unknown shapes are skipped, never crash ingest.

---

## Data mapping (transcript line -> rows)

Every transcript line is first persisted **verbatim** to `raw_events` (the lossless source of truth: all event types, byte-for-byte, nothing filtered). `events`, `api_requests`, `file_versions`, and `sessions` are derived projections built from those same lines for search, token accounting, and reconstruction. Drop the derived tables and you can rebuild them from `raw_events`; delete the source transcripts and you lose nothing.

| Transcript line | Emits |
|---|---|
| every line, all event types | `raw_events` row (complete original JSON line, verbatim) |
| `type:"assistant"`, content `text` block | `events` row, `type='assistant'` |
| `type:"assistant"`, content `tool_use` block | `events` row, `type='tool_use'`, `tool_use_id=block.id`, `tool_name`, `tool_input`, `text`=command/representative field |
| `type:"assistant"`, top-level `requestId` + `message.usage` | `api_requests` row (one per API request; the request's several content-block lines dedupe on `request_id`) |
| `type:"user"`, content `tool_result` block (+ top-level `toolUseResult`) | `events` row, `type='tool_result'`, `tool_use_id`, `stdout`/`stderr`/`is_error` |
| `type:"user"`, plain string/text content | `events` row, `type='user'` (the prompt) |
| edit/write `toolUseResult` (Task 5) | `file_versions` row + `blobs` rows |

Append-only model: tool calls and their results are **separate rows** joined on `tool_use_id` (no UPDATEs, idempotent re-ingest). A view stitches them back together. Unparseable lines are still stored in `raw_events` (keyed by content hash), so ingest never silently drops anything.

## Token accounting (spec added 2026-07-07)

**Spec:** answer "how many tokens did each session spend" and "how many tokens did each command spend" from the DuckDB store.

What the transcripts give us (verified against the live corpus):
- Every assistant line carries top-level `requestId` plus `message.id`, `message.model`, and `message.usage` (`input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `service_tier`, ...).
- One API request is written as several JSONL lines (one per content block), each repeating the same `requestId` and identical usage. Verified sample: 1501 usage-bearing assistant lines collapse to 585 unique requests. So `api_requests` keys on `request_id` and dedupes with `ON CONFLICT DO NOTHING`; fallback key when `requestId` is absent: `message.id`, else the line `uuid`.
- Synthetic assistant lines (`model == "<synthetic>"`, all-zero usage, no real API call) are skipped by `parse_usage`.
- Subagent transcripts (`<project>/<sid>/subagents/agent-<id>.jsonl`) carry their own usage lines under the parent `sessionId`. Session totals are complete only because Task 3 ingests them; the `agent_id` column splits main-thread vs subagent spend.

Attribution model (the API bills per request, not per tool call; be explicit about what is exact vs derived):
- **Per session: exact.** Sum `api_requests` grouped by `session_id` (the `session_tokens` view).
- **Per command, generation cost: exact per request.** Each `tool_use` event row records the `request_id` of the assistant message that emitted it; that request's `output_tokens` is what it cost to generate the command (including any thinking/text in the same message). When one request emits N parallel tool calls, `command_tokens` exposes `calls_in_request` and an even `output_tokens_share` split.
- **Per command, read-back cost: derived approximation.** A command's output is paid for as input on the NEXT request. The `request_context` view computes each request's total context (`input + cache_read + cache_creation`) and its growth over the previous request in the same (session, agent) thread; growth after a tool-running turn ~= the size of those results plus any new user text. It goes negative across compaction boundaries; that is real signal, not an error. Finer splits (proportional to stdout size across parallel results) are left to ad-hoc SQL over `events` + `request_context`.

Canonical queries the store must support:
```sql
-- tokens per session
SELECT * FROM session_tokens ORDER BY output_tokens DESC LIMIT 20;
-- tokens per command (generation cost), most expensive first
SELECT ts, tool_name, command, output_tokens, calls_in_request
FROM command_tokens WHERE session_id LIKE 'bd9991f8%' ORDER BY output_tokens DESC;
-- context growth per request (read-back cost of the preceding tool results)
SELECT ts, context_tokens, context_growth FROM request_context
WHERE session_id LIKE 'bd9991f8%' ORDER BY ts;
```

Because `raw_events` is lossless, `api_requests` is rebuildable retroactively (`json_extract` over `raw_events` where `type='assistant'`) even for transcripts ingested before this spec existed; usage-schema drift (new fields like `iterations`) is absorbed the same way: `parse_usage` reads only the four stable counters, the full JSON stays in `raw_events`.

## Schema

```sql
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
```

DB path: `~/.claude/trail.duckdb` (constant `STORE_PATH`, overridable via `$CLAUDE_TRAIL_DB`).

---

## Task 1: Dependency, module skeleton, DB bootstrap

**Files:**
- Modify: `pyproject.toml` (add `duckdb>=1.0`)
- Create: `claude_trail_store.py`
- Modify: `claude_trail.py` (`main()` subcommand dispatch)
- Create: `tests/test_store.py`

- [ ] **Step 1.1: Add the dependency**

In `pyproject.toml`, add to `dependencies` (alongside `rich>=13.0`):
```toml
"duckdb>=1.0",
```

- [ ] **Step 1.2: Create `claude_trail_store.py` with connect + schema**

```python
"""DuckDB-backed audit trail store for Claude Code sessions.

Ingests transcripts (~/.claude/projects/**/*.jsonl) and file snapshots into a
single local DuckDB file; provides substring/regex search and file
reconstruction. Single-writer ingester, read-only readers.
"""
import os
from pathlib import Path

STORE_PATH = Path(os.environ.get("CLAUDE_TRAIL_DB", Path.home() / ".claude" / "trail.duckdb"))
SCHEMA_VERSION = "1"

SCHEMA_SQL = """  -- the full DDL block from the Schema section above
... (paste the CREATE TABLE / INDEX / VIEW statements verbatim) ...
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
```

- [ ] **Step 1.3: Wire subcommand dispatch in `claude_trail.py`**

In `main()`, replace the current top of the function:
```python
def main():
    if len(sys.argv) > 1 and sys.argv[1] == "hook":
        sys.exit(hook_main())
```
with:
```python
def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "hook":
        sys.exit(hook_main())
    if argv and argv[0] in ("ingest", "search", "cat", "diff", "stats", "tokens"):
        from claude_trail_store import cli_main  # lazy: only loads duckdb on demand
        sys.exit(cli_main(argv))
```
(Leave the rest of `main()` -- the TUI -- unchanged.)

- [ ] **Step 1.4: Verify schema bootstraps and reopens read-only**
```bash
cd /home/naka/Projects/personal/claude-trail
CLAUDE_TRAIL_DB=/tmp/trail-test.duckdb python3 -c "import claude_trail_store as s; c=s.connect(); print([r[0] for r in c.execute(\"select table_name from information_schema.tables order by 1\").fetchall()]); c.close(); c2=s.connect(read_only=True); print('reopened ro'); c2.close()"
rm -f /tmp/trail-test.duckdb
```
Expected: prints the table list (`actions`, `api_requests`, `blobs`, `command_tokens`, `events`, `file_versions`, `ingest_state`, `raw_events`, `request_context`, `schema_meta`, `session_tokens`, `sessions`) then `reopened ro`.

- [ ] **Step 1.5: Commit**
```bash
git -C /home/naka/Projects/personal/claude-trail add pyproject.toml claude_trail_store.py claude_trail.py
git -C /home/naka/Projects/personal/claude-trail commit -m "Add DuckDB audit store skeleton and schema"
```

---

## Task 2: Transcript parser (pure, unit-tested)

**Files:**
- Modify: `claude_trail_store.py` (add parsing functions)
- Modify: `tests/test_store.py`

Background: parsing is the riskiest part, so keep it pure (event dict in, list of row dicts out) and test it against real shapes. No DB, no I/O. The parser only feeds the derived `events` and `api_requests` tables; the lossless `raw_events` layer (Task 3) is written separately from the raw line, so a parser gap never loses data, it just means a row is missing from the search projection until the parser is improved and `events` is rebuilt.

- [ ] **Step 2.1: Write failing tests with real event shapes**

In `tests/test_store.py`, add cases covering: assistant text + tool_use (Bash), user tool_result with `toolUseResult.stdout/stderr`, plain user prompt (string content), plain user prompt (list-of-text content), and a malformed event (returns `[]`, never raises). For `parse_usage`: an assistant line with `message.usage` returns a row keyed by `requestId` carrying the four token counters; a `<synthetic>` assistant line returns `None`; two content-block lines of one request return identical rows sharing `request_id` (dedup happens at insert). For attribution: a tool_use row carries the line's `requestId`, and `parse_event(ev, agent_id="abc")` stamps `agent_id` on every row. Use fixtures captured from a real transcript:
```bash
# helper to capture real samples while writing tests:
F=$(find ~/.claude/projects -name '*.jsonl' | head -1)
grep -m1 '"type":"assistant"' "$F"
grep -m1 'toolUseResult' "$F"
```

- [ ] **Step 2.2: Implement the parser**

```python
import json

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
```

- [ ] **Step 2.3: Run parser tests**
```bash
cd /home/naka/Projects/personal/claude-trail && python3 -m pytest tests/test_store.py -v
```
Expected: all parser tests pass; malformed-event test returns `[]`.

- [ ] **Step 2.4: Commit**
```bash
git -C /home/naka/Projects/personal/claude-trail add claude_trail_store.py tests/test_store.py
git -C /home/naka/Projects/personal/claude-trail commit -m "Add transcript event parser"
```

---

## Task 3: Incremental ingester (`claude-trail ingest [--watch]`)

**Files:**
- Modify: `claude_trail_store.py`

Background: reuse the byte-offset tailing pattern the TUI already uses for `/color` (`claude_trail.load_session_colors`). One DuckDB write connection; per transcript, read from `last_offset`, then for each complete line: (1) persist it verbatim to `raw_events` (lossless), (2) parse it into `events` rows and (for assistant lines) an `api_requests` row. Upsert the session, advance the offset. `raw_events` is written first and unconditionally, so nothing is lost even if parsing skips a line. The sweep covers BOTH main transcripts (`<project>/<sid>.jsonl`) and subagent transcripts (`<project>/<sid>/subagents/agent-<id>.jsonl`); subagent files carry the parent `sessionId` inside, so session attribution is automatic, and the `agent-<id>` filename supplies `agent_id`.

- [ ] **Step 3.1: Implement ingest of one transcript**

```python
import hashlib, time

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
```
Note: the staging-temp-table + `ON CONFLICT DO NOTHING` pattern keeps re-ingest idempotent and avoids per-row conflict overhead. (If profiling shows it matters, switch `_insert_events` to a DuckDB Appender writing to `_stage`.) `ON CONFLICT` only resolves conflicts against rows already in the table, so each `_insert_*` dedupes its batch in Python first where the key can repeat within one batch (`raw_events` sha-keyed lines, `api_requests` content-block repeats).

- [ ] **Step 3.2: Implement session upsert**

```python
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
```

- [ ] **Step 3.3: Implement the ingest sweep + `--watch`**

```python
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
```

- [ ] **Step 3.4: First full ingest + sanity counts**
```bash
cd /home/naka/Projects/personal/claude-trail
CLAUDE_TRAIL_DB=/tmp/trail-test.duckdb python3 claude_trail.py ingest
CLAUDE_TRAIL_DB=/tmp/trail-test.duckdb python3 -c "import claude_trail_store as s; c=s.connect(read_only=True); print('raw', c.execute('select count(*) from raw_events').fetchone()[0]); print('events', c.execute('select count(*) from events').fetchone()[0]); print('sessions', c.execute('select count(*) from sessions').fetchone()[0]); print('bash', c.execute(\"select count(*) from events where tool_name='Bash'\").fetchone()[0])"
# every transcript event type should be present in raw_events, not just the parsed four:
CLAUDE_TRAIL_DB=/tmp/trail-test.duckdb python3 -c "import claude_trail_store as s; c=s.connect(read_only=True); print(c.execute('select type, count(*) n from raw_events group by 1 order by n desc').fetchall())"
# token accounting: requests deduped (well below assistant line count), subagents attributed
CLAUDE_TRAIL_DB=/tmp/trail-test.duckdb python3 -c "import claude_trail_store as s; c=s.connect(read_only=True); print('api_requests', c.execute('select count(*) from api_requests').fetchone()[0]); print('assistant lines', c.execute(\"select count(*) from raw_events where type='assistant'\").fetchone()[0]); print('subagent requests', c.execute('select count(*) from api_requests where agent_id is not null').fetchone()[0]); print('top session', c.execute('select substr(session_id,1,8), output_tokens from session_tokens order by output_tokens desc limit 1').fetchall())"
ls -lh /tmp/trail-test.duckdb
```
Expected: `raw` >= `events` and in the hundreds of thousands, ~1405 sessions, a large Bash count, `raw_events` type list showing all types (assistant, user, system, attachment, file-history-snapshot, mode, ...), and a DB file far smaller than 484 MB (compression sanity). `api_requests` count is well below the assistant line count (content-block lines collapse onto one request, roughly 2-3x in the sampled transcript) and `subagent requests` > 0. Re-running `ingest` should report `ingested 0 events` (idempotent, `raw_events.uuid` conflict-skips).

- [ ] **Step 3.5: Commit**
```bash
git -C /home/naka/Projects/personal/claude-trail add claude_trail_store.py
git -C /home/naka/Projects/personal/claude-trail commit -m "Add incremental transcript ingester with --watch"
```

---

## Task 4: Search, stats, and tokens CLI

**Files:**
- Modify: `claude_trail_store.py`

- [ ] **Step 4.1: Implement `search` (regex/substring), `stats`, and `tokens`**

```python
import re as _re

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
        return args[args.index(name) + 1] if name in args else default
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
        return args[args.index(name) + 1] if name in args else default
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
                              min(ts), max(ts)
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
            print(f"== {label} =="); 
            for r in con.execute(q).fetchall(): print("  ", *r)
    finally:
        con.close()
    return 0

def cli_main(argv):
    cmd = argv[0]
    return {"ingest": cmd_ingest, "search": cmd_search, "stats": cmd_stats,
            "tokens": cmd_tokens, "cat": cmd_cat, "diff": cmd_diff}[cmd](argv)
```
(`cmd_cat`/`cmd_diff` are added in Task 6; stub them to `return 0` until then so dispatch imports cleanly, or land Task 6 before exercising them.)

- [ ] **Step 4.2: Verify search and tokens against the test DB**
```bash
cd /home/naka/Projects/personal/claude-trail
CLAUDE_TRAIL_DB=/tmp/trail-test.duckdb python3 claude_trail.py search 'git commit' --tool Bash --limit 5
CLAUDE_TRAIL_DB=/tmp/trail-test.duckdb python3 claude_trail.py search 'duckdb' -i --limit 5
CLAUDE_TRAIL_DB=/tmp/trail-test.duckdb python3 claude_trail.py stats
# the two spec questions: tokens per session, tokens per command
CLAUDE_TRAIL_DB=/tmp/trail-test.duckdb python3 claude_trail.py tokens --limit 5
SID=$(CLAUDE_TRAIL_DB=/tmp/trail-test.duckdb python3 -c "import claude_trail_store as s; c=s.connect(read_only=True); print(c.execute('select session_id from session_tokens order by output_tokens desc limit 1').fetchone()[0])")
CLAUDE_TRAIL_DB=/tmp/trail-test.duckdb python3 claude_trail.py tokens --session "$SID" --limit 10
CLAUDE_TRAIL_DB=/tmp/trail-test.duckdb python3 claude_trail.py tokens --by day --since 2026-06-01
```
Expected: matching rows newest-first; `stats` prints counts and top tools; `tokens` prints per-session totals (most expensive first), then the per-command breakdown for the top session, then daily totals. Cross-check one session's totals against `/usage` or claude-usage if available. Time a broad query to confirm sub-second/low-second scan:
```bash
CLAUDE_TRAIL_DB=/tmp/trail-test.duckdb python3 -c "import time,claude_trail_store as s; c=s.connect(read_only=True); t=time.time(); n=c.execute(\"select count(*) from events where regexp_matches(coalesce(text,''),'(?i)error')\").fetchone()[0]; print(n,'matches in', round(time.time()-t,3),'s')"
```

- [ ] **Step 4.3: Commit**
```bash
git -C /home/naka/Projects/personal/claude-trail add claude_trail_store.py
git -C /home/naka/Projects/personal/claude-trail commit -m "Add search, stats, and tokens CLI over the audit store"
```

**Phase 1 is shippable here:** full-history search over prompts, messages, commands, and results, plus token-spend analytics per session and per command.

---

## Task 5: File versions + content-addressed blobs (Phase 2)

**Files:**
- Modify: `claude_trail_store.py`
- Modify: `tests/test_store.py`

Background: reconstruction needs full content per edit, stored once per unique content (dedup by sha256). Sources, in priority order: (1) `Write`/`MultiEdit` `tool_input` carries the full new content; (2) `toolUseResult.structuredPatch` gives the diff and often `originalFile`/`file.content`; (3) `~/.claude/file-history/<uuid>/<pathhash>@vN` holds full snapshots.

- [ ] **Step 5.1: INVESTIGATION -- pin down the real shapes (do before coding)**

```bash
F=$(find ~/.claude/projects -name '*.jsonl' -printf '%s\t%p\n' | sort -rn | head -1 | cut -f2)
echo "--- an Edit toolUseResult ---"; grep -m1 'structuredPatch' "$F" | python3 -m json.tool | head -60
echo "--- a file-history-snapshot event ---"; grep -m1 'file-history-snapshot' "$F" | python3 -m json.tool | head -40
echo "--- a file-history dir vs a session ---"; ls ~/.claude/file-history | head; head -c 200 "$(find ~/.claude/file-history -type f | head -1)"
```
Record, in a comment block in `claude_trail_store.py`: the exact path to `structuredPatch` inside `toolUseResult`; whether `originalFile`/full content is present; and how `file-history/<uuid>` and `<pathhash>@vN` map to (session_id, real path, order). If the file-history mapping cannot be determined, proceed with sources (1) and (2) only and note the limitation.

- [ ] **Step 5.2: Implement blob put + file-version extraction**

```python
import difflib
# _sha() is defined in Task 3 (used for raw_events content-hash keys); reused here.

def _put_blob(con, text):
    if text is None:
        return None
    sha = _sha(text)
    con.execute("INSERT INTO blobs VALUES (?,?,?) ON CONFLICT (sha) DO NOTHING",
                [sha, len(text.encode("utf-8", "replace")), text])
    return sha

def file_version_from_event(con, ev):
    """For Edit/Write/MultiEdit user-result events, return a file_versions row
    (and store blobs). Uses the shapes confirmed in Step 5.1. Returns None if N/A."""
    tur = ev.get("toolUseResult")
    if not isinstance(tur, dict):
        return None
    # shape resolved in 5.1, e.g. tur.get("filePath"), tur.get("originalFile"),
    # the new content, and tur.get("structuredPatch")
    path = tur.get("filePath") or (tur.get("file") or {}).get("filePath")
    if not path:
        return None
    pre  = tur.get("originalFile")                     # may be None for create
    post = tur.get("content") or (tur.get("file") or {}).get("content")
    patch = _render_patch(tur.get("structuredPatch"), pre, post, path)
    return {
        "event_id": ev.get("uuid"), "session_id": ev.get("sessionId", ""),
        "ts": ev.get("timestamp"), "path": path,
        "pre_sha": _put_blob(con, pre), "post_sha": _put_blob(con, post),
        "patch": patch,
    }

def _render_patch(structured, pre, post, path):
    if structured:                                     # already-computed hunks
        return json.dumps(structured)
    if pre is not None and post is not None:
        return "".join(difflib.unified_diff(
            pre.splitlines(True), post.splitlines(True),
            fromfile=path, tofile=path))
    return None
```
Hook `file_version_from_event` into `ingest_transcript`: when an event yields a file version, `INSERT INTO file_versions`. Keep it in the same transaction as the events insert for that batch.

- [ ] **Step 5.3: Tests for blob dedup and patch rendering**

Assert: two identical contents produce one `blobs` row; `_render_patch` falls back to difflib when `structuredPatch` is absent; `file_version_from_event` returns `None` for a Bash result.

- [ ] **Step 5.4: Re-ingest and verify**
```bash
rm -f /tmp/trail-test.duckdb
cd /home/naka/Projects/personal/claude-trail
CLAUDE_TRAIL_DB=/tmp/trail-test.duckdb python3 claude_trail.py ingest
CLAUDE_TRAIL_DB=/tmp/trail-test.duckdb python3 claude_trail.py stats   # file versions / blobs now non-zero
```

- [ ] **Step 5.5: Commit**
```bash
git -C /home/naka/Projects/personal/claude-trail add claude_trail_store.py tests/test_store.py
git -C /home/naka/Projects/personal/claude-trail commit -m "Capture file versions and content-addressed blobs"
```

---

## Task 6: Reconstruction CLI (`cat` / `diff`)

**Files:**
- Modify: `claude_trail_store.py`

- [ ] **Step 6.1: Implement `cat` and `diff`**

```python
def cmd_cat(argv):
    # usage: cat <path> [--session S] [--at ISO]   -> latest content at/before --at
    args = argv[1:]
    path = args[0]
    def opt(n): return args[args.index(n)+1] if n in args else None
    where, params = ["fv.path = ?"], [path]
    if opt("--session"): where.append("fv.session_id LIKE ?"); params.append(opt("--session")+"%")
    if opt("--at"):      where.append("fv.ts <= ?");           params.append(opt("--at"))
    sql = (f"SELECT b.content FROM file_versions fv JOIN blobs b ON b.sha=fv.post_sha "
           f"WHERE {' AND '.join(where)} ORDER BY fv.ts DESC LIMIT 1")
    con = connect(read_only=True)
    try:
        row = con.execute(sql, params).fetchone()
    finally:
        con.close()
    if not row:
        print("no stored version for that path/time"); return 1
    print(row[0], end="")
    return 0

def cmd_diff(argv):
    # usage: diff <path> [--session S]  -> chronological list of edits with patches
    args = argv[1:]; path = args[0]
    def opt(n): return args[args.index(n)+1] if n in args else None
    where, params = ["path = ?"], [path]
    if opt("--session"): where.append("session_id LIKE ?"); params.append(opt("--session")+"%")
    sql = f"SELECT ts, substr(session_id,1,8), patch FROM file_versions WHERE {' AND '.join(where)} ORDER BY ts"
    con = connect(read_only=True)
    try:
        for ts, sid, patch in con.execute(sql, params).fetchall():
            print(f"=== {ts}  {sid} ===\n{patch or '(no patch)'}\n")
    finally:
        con.close()
    return 0
```

- [ ] **Step 6.2: Verify reconstruction round-trips a known file**
```bash
# pick a path that the store has versions for:
CLAUDE_TRAIL_DB=/tmp/trail-test.duckdb python3 -c "import claude_trail_store as s; c=s.connect(read_only=True); print(c.execute('select path,count(*) from file_versions group by 1 order by 2 desc limit 3').fetchall())"
CLAUDE_TRAIL_DB=/tmp/trail-test.duckdb python3 claude_trail.py cat <one-of-those-paths> | head
CLAUDE_TRAIL_DB=/tmp/trail-test.duckdb python3 claude_trail.py diff <one-of-those-paths> | head -40
```
Expected: `cat` prints reconstructed content; `diff` lists edits oldest-first with patches.

- [ ] **Step 6.3: Commit**
```bash
git -C /home/naka/Projects/personal/claude-trail add claude_trail_store.py
git -C /home/naka/Projects/personal/claude-trail commit -m "Add cat/diff file reconstruction CLI"
```

---

## Task 7: Docs + dependency note

**Files:**
- Modify: `CLAUDE.md` (new "Audit store" section), `README.md` if present, `requirements.txt`

- [ ] **Step 7.1:** Add `duckdb>=1.0` to `requirements.txt`.
- [ ] **Step 7.2:** Add a CLAUDE.md section documenting: the store path (`~/.claude/trail.duckdb`, `$CLAUDE_TRAIL_DB`), the six subcommands (`ingest`, `search`, `stats`, `tokens`, `cat`, `diff`), that ingest is the single writer (cron or `--watch`) and readers are read-only, the append-only event model + `actions` view, the token accounting model (`api_requests` deduped on `request_id`; `session_tokens` exact per session, `command_tokens` generation cost per command, `request_context` read-back approximation), and the blob/file-version reconstruction model.
- [ ] **Step 7.3:** Note in CLAUDE.md that the store reads transcripts (not the hook), so it covers all tools, while the live TUI still uses `command-log.jsonl`.
- [ ] **Step 7.4: Bump minor version** in `pyproject.toml` (new feature: `0.3.x` -> `0.4.0`) and reinstall (`uv tool install --force --reinstall --no-cache .`).
- [ ] **Step 7.5: Commit.**

---

## Task 8 (deferred): TUI search view

Out of scope for the first cut; sketch only. Add a `/` key in the TUI that opens a search prompt, runs `cmd_search` against the read-only store, and renders results in a panel (reuse the detail-view modal pattern added in 0.3.0). Decide later whether the live TUI keeps tailing `command-log.jsonl` or switches to tailing the DuckDB store. Revisit once ingest runs continuously.

---

## Self-Review

**Spec coverage:**
- Persist every session event verbatim (lossless) -> `raw_events` (Task 3), all event types, byte-for-byte, including unparseable lines. `events`/`file_versions`/`sessions` are derived projections rebuildable from it.
- Store all sessions + all commands -> Tasks 2-3 (events from every transcript, all tools).
- Text search -> Task 4 (regex/substring scan, the locked choice).
- Good compression, local, DuckDB -> Tasks 1, 3 (single columnar file; Step 3.4 checks file size vs 484 MB).
- Metadata columns (files edited) -> `events.tool_name`/`tool_input` + `file_versions.path`.
- Diffs to reconstruct files -> Tasks 5-6 (content-addressed blobs, the locked choice; `cat`/`diff`).
- Commands + results -> append-only `tool_use`/`tool_result` rows joined by the `actions` view.
- Token spend per session and per command (spec 2026-07-07) -> `api_requests` (Tasks 2-3: `parse_usage`, `request_id` stamped on tool_use rows, subagent transcripts ingested) + `session_tokens`/`command_tokens`/`request_context` views + `tokens` CLI (Task 4). Per-session numbers are exact; per-command generation cost is exact per request (even split across parallel calls); read-back cost is a documented approximation (context growth).

**Concurrency:** single writer (ingester) holds DuckDB's RW connection; all readers `read_only=True`. Hook never touches DuckDB. Stated in Architecture and Task 7.

**Idempotency:** offset-tracked tailing + `raw_events.uuid` / `event_id` / `request_id` / `sha` primary keys + `ON CONFLICT DO NOTHING`, with per-batch Python dedupe where a key can repeat inside one insert. Step 3.4 asserts re-ingest = 0 new events. A line without a `uuid` (or malformed) keys on `sha256(line)`, so re-ingesting the same bytes still conflict-skips.

**Placeholder scan:** real code throughout except (a) `SCHEMA_SQL` says "paste DDL verbatim" (the DDL is fully given in the Schema section), and (b) Task 5 deliberately defers exact `toolUseResult`/file-history field paths to the Step 5.1 investigation, with a difflib fallback so it ships regardless. `_insert_events` flags the Appender as an optional optimization.

**Type/name consistency:** `_EVENT_COLS` order matches the `events` DDL and the staging insert (including `request_id`/`agent_id`); `_REQ_COLS` matches `api_requests`. `parse_event` returns row dicts with exactly those keys; `parse_usage` returns `_REQ_COLS` keys. `cli_main` dispatch keys match the subcommands gated in `claude_trail.py main()` (`ingest|search|cat|diff|stats|tokens`).

**Risks not caught:**
- DuckDB `executemany` into a staging table for very large first ingests may be slow; if Step 3.4 is unacceptably slow, switch `_insert_events` to a DuckDB Appender or `read_json_auto` bulk path. Functionally correct either way.
- `_decode_project` is a lossy reverse of the dir encoding (paths with literal hyphens won't round-trip perfectly); it is a display label only, not a key, so imperfect decoding is cosmetic.
- If a transcript mixes sessions (it should not), `_upsert_session` takes the first non-empty `session_id`; acceptable since transcripts are per-session.
- File-history mapping may stay unsolved (Task 5.1); reconstruction then relies on Write content + structuredPatch, which covers edits seen in transcripts but not files changed outside Claude. Documented as a known limit.
- `context_growth` in `request_context` is negative across compaction boundaries and includes user text, not just tool results; it is an attribution aid, never an exact per-command cost. Documented in the Token accounting section.
- Old-format inline sidechains (`isSidechain` lines inside the parent transcript, before subagents/ files existed) are counted as main-thread spend (`agent_id` NULL). Session totals stay correct; only the main-vs-subagent split is off for those old sessions.
- Usage schema drift (new nested fields like `iterations`) is safe: `parse_usage` reads only the four stable counters and `raw_events` keeps the full JSON, so `api_requests` can be rebuilt with a smarter parser later.
