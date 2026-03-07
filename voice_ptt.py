#!/usr/bin/env python3
"""
voice_ptt.py — Push-to-talk voice bridge for Claude Code.

Listens on a named pipe for start/stop signals from keypad_macros.py.
On "start": begins recording audio from the microphone.
On "stop": stops recording, transcribes via local Whisper, writes result.

Usage:
    python3 voice_ptt.py

The transcription is written to /tmp/claude-voice-transcription.
Claude Code polls this file and picks up the text as user input.
"""

import os
import sys
import signal
import subprocess
import tempfile
import json
import urllib.request
import urllib.error

PIPE_PATH = "/tmp/claude-voice-trigger"
OUTPUT_PATH = "/tmp/claude-voice-transcription"
WHISPER_URL = "http://127.0.0.1:2022/v1/audio/transcriptions"
RECORD_PATH = "/tmp/claude-voice-recording.wav"


def ensure_pipe():
    if os.path.exists(PIPE_PATH):
        if not stat_is_fifo(PIPE_PATH):
            os.remove(PIPE_PATH)
            os.mkfifo(PIPE_PATH)
    else:
        os.mkfifo(PIPE_PATH)


def stat_is_fifo(path):
    import stat
    return stat.S_ISFIFO(os.stat(path).st_mode)


def start_recording():
    """Start arecord in the background, recording to WAV."""
    proc = subprocess.Popen(
        ["arecord", "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "wav", RECORD_PATH],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"[PTT] Recording started (pid {proc.pid})")
    return proc


def stop_recording(proc):
    """Stop the arecord process."""
    if proc and proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=3)
    print("[PTT] Recording stopped")


def transcribe():
    """Send the recorded WAV to local Whisper and return the text."""
    if not os.path.exists(RECORD_PATH):
        return ""

    file_size = os.path.getsize(RECORD_PATH)
    if file_size < 1000:  # too short
        print("[PTT] Recording too short, skipping transcription")
        return ""

    # Multipart form upload
    boundary = "----VoicePTTBoundary"
    with open(RECORD_PATH, "rb") as f:
        audio_data = f.read()

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="recording.wav"\r\n'
        f"Content-Type: audio/wav\r\n\r\n"
    ).encode() + audio_data + (
        f"\r\n--{boundary}\r\n"
        f'Content-Disposition: form-data; name="model"\r\n\r\n'
        f"whisper-1"
        f"\r\n--{boundary}--\r\n"
    ).encode()

    req = urllib.request.Request(
        WHISPER_URL,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            text = result.get("text", "").strip()
            print(f"[PTT] Transcription: {text}")
            return text
    except Exception as e:
        print(f"[PTT] Transcription error: {e}", file=sys.stderr)
        return ""


def write_transcription(text):
    """Append transcription to output file."""
    if text:
        with open(OUTPUT_PATH, "a") as f:
            f.write(text + "\n")
        print(f"[PTT] Written to {OUTPUT_PATH}")


def main():
    ensure_pipe()
    print(f"[PTT] Push-to-talk ready. Listening on {PIPE_PATH}")
    print(f"[PTT] Transcriptions written to {OUTPUT_PATH}")

    recorder = None

    def cleanup(*_):
        if recorder and recorder.poll() is None:
            recorder.send_signal(signal.SIGINT)
            recorder.wait(timeout=3)
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    while True:
        # Opening a FIFO for reading blocks until a writer opens the other end
        with open(PIPE_PATH, "r") as pipe:
            for line in pipe:
                cmd = line.strip()
                if cmd == "start":
                    if recorder and recorder.poll() is None:
                        stop_recording(recorder)
                    recorder = start_recording()
                elif cmd == "stop":
                    if recorder:
                        stop_recording(recorder)
                        recorder = None
                        text = transcribe()
                        write_transcription(text)


if __name__ == "__main__":
    main()
