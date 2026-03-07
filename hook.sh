#!/bin/bash
# Claude Code PostToolUse hook — logs Bash commands to ~/.claude/command-log.jsonl
set -euo pipefail

input=$(cat)
tool_name=$(echo "$input" | jq -r '.tool_name // empty')

if [ "$tool_name" = "Bash" ]; then
  command=$(echo "$input" | jq -r '.tool_input.command // empty')
  cwd=$(echo "$input" | jq -r '.cwd // empty')
  session_id=$(echo "$input" | jq -r '.session_id // empty')
  timestamp=$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")

  jq -nc \
    --arg ts "$timestamp" \
    --arg cmd "$command" \
    --arg cwd "$cwd" \
    --arg sid "$session_id" \
    '{timestamp: $ts, command: $cmd, cwd: $cwd, session_id: $sid}' \
    >> ~/.claude/command-log.jsonl
fi
