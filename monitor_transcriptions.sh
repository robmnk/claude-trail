#!/usr/bin/env bash
# Monitor push-to-talk transcriptions in real time.
# Usage: ./monitor_transcriptions.sh

TRANSCRIPTION_FILE="/tmp/claude-voice-transcription"

touch "$TRANSCRIPTION_FILE"

echo "[Monitor] Watching for transcriptions at $TRANSCRIPTION_FILE"
echo "[Monitor] Press Ctrl+C to stop."
echo ""

tail -f "$TRANSCRIPTION_FILE"
