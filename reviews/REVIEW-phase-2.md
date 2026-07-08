# Phase 2 review: Transcript parser (pure, unit-tested)

Verdict: clean
Diff: main...phase-2-parser (82aa3bb), 2 files, +353/-0

## Blockers
none

## Major
none

## Minor
- `tests/test_store.py` - `parse_usage` fallback keying is untested: no case where `requestId` is absent asserting the row keys on `message.id`, then on `uuid` (spec line: "fallback key when requestId is absent: message.id, else the line uuid"). Add two small fixtures deleting `requestId` (and then also `message.id`).

## Nits / follow-ups
- `claude_trail_store.py:178` - `bool(block.get("is_error"))` yields `False` (not `None`) when the block has no `is_error` key, so "unknown" and "not an error" are indistinguishable in the column. Matches the spec code verbatim; note for the ingester phase if NULL-vs-false ever matters.
- `tests/test_store.py` - the tool_use event_id fallback (`{uuid}:u{i}` when the block lacks `id`, claude_trail_store.py:220) is untested. Low risk; consider covering when touching the parser next.
- `tests/test_store.py:263` - `line_a = dict(base)` / `line_b = dict(base)` are shallow copies sharing the same `message` dict; harmless here since only top-level `uuid` is changed, but `copy.deepcopy` would be safer if the test grows.

## Notes
- Scope match: exact. `parse_event`, `parse_usage`, and the four helpers are byte-for-byte the spec's Step 2.2 code, added to `claude_trail_store.py:147-271`; only the two intended files changed, TUI/hook untouched, no ingester code smuggled in.
- Row-shape contract verified against the Phase 1 schema in the same module: `_row` emits exactly the 15 `events` columns (event_id, session_id, parent_uuid, ts, type, role, tool_name, tool_use_id, request_id, agent_id, text, tool_input, stdout, stderr, is_error); `parse_usage` emits exactly the 11 `api_requests` columns including `cache_read_tokens`/`cache_creation_tokens` (renamed from the transcript's `cache_read_input_tokens`/`cache_creation_input_tokens` as the schema requires). No missing or renamed keys.
- Spec checklist all covered by tests: assistant text + Bash tool_use with `requestId` on both rows and `event_id == tool_use_id`; tool_result joined by `tool_use_id` pulling stdout/stderr from `toolUseResult` and `is_error` from the block; string prompt; list-of-text prompt joined with newlines; malformed inputs (non-dict, `{}`, junk content list) return `[]` without raising; synthetic model skip; non-assistant/None-usage skip; shared `request_id` across duplicate content-block lines; `agent_id` stamping on every row and on the usage row, with None default.
- Tests are hermetic: inline fixtures modeled on real transcript shapes, no live `~/.claude` reads, no DB.
- Gate: `~/.pyenv/versions/global/bin/python -m pytest -q` -> 302 passed.
- No em/en dash characters anywhere in the diff.
