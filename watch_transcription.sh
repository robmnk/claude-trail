#!/usr/bin/env bash
# Watches /tmp/claude-voice-transcription for new lines.
# Prints the latest new line and exits.

FILE="/tmp/claude-voice-transcription"
touch "$FILE"

last_size=$(stat -c%s "$FILE")

while true; do
    current_size=$(stat -c%s "$FILE" 2>/dev/null || echo 0)
    if [ "$current_size" -gt "$last_size" ]; then
        tail -c +"$((last_size + 1))" "$FILE" | tail -n 1
        break
    fi
    sleep 0.3
done
