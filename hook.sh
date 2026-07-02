#!/bin/bash
# DEPRECATED: use `claude-trail hook` instead. Kept only for existing installs
# that still reference this script; new installs should register `claude-trail hook`.
#
# Claude Code PostToolUse hook: logs Bash commands to ~/.claude/command-log.jsonl
set -euo pipefail

# Local ISO-8601 timestamp with UTC offset, matching `claude-trail hook`.
# GNU date supports the `%:z` offset; BSD/macOS date does not (use `%z` there).
ts=$(date +"%Y-%m-%dT%H:%M:%S%:z")
jq -c --arg ts "$ts" '
  select(.tool_name == "Bash")
  | {timestamp: $ts, command: .tool_input.command, cwd, session_id}
' >> ~/.claude/command-log.jsonl
