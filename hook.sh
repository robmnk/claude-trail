#!/bin/bash
# Claude Code PostToolUse hook — logs Bash commands to ~/.claude/command-log.jsonl
set -euo pipefail

ts=$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")
jq -c --arg ts "$ts" '
  select(.tool_name == "Bash")
  | {timestamp: $ts, command: .tool_input.command, cwd, session_id}
' >> ~/.claude/command-log.jsonl
