#!/usr/bin/env bash
# Copyright 2026 Wei (Jack) Sun
# Licensed under the Apache License, Version 2.0 (see LICENSE).
#
# Claude Code PostToolBatch hook for telegram-buddy.
#
# Re-injects the docking directive on every loop turn while the session is
# in chat-dock mode. When not docked, no-op.
#
# Reads the harness-provided JSON event from stdin; writes a JSON output
# to stdout following the PostToolBatch hook contract.
#
# Exit code 0 always (we never want to break the agentic loop on hook
# failure — a missing directive injection just degrades to soft-directive
# mode, which is the existing tool-response fallback).

set -u

SENTINEL_DIR="${TMPDIR:-/tmp}/telegram-buddy/sessions"

# Shell out as little as possible — this hook fires on every model turn.
input="$(cat <&0)"

# Use jq if available; fall back to grep for portability. jq should be present
# on macOS via Xcode CLI tools and on most Linux distros, but we degrade
# gracefully to a regex extraction.
if command -v jq >/dev/null 2>&1; then
  session_id="$(printf '%s' "$input" | jq -r '.session_id // ""')"
else
  session_id="$(printf '%s' "$input" | grep -o '"session_id":[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')"
fi

if [[ -z "$session_id" ]]; then
  echo '{}'
  exit 0
fi

if [[ ! -f "${SENTINEL_DIR}/${session_id}.docked" ]]; then
  echo '{}'
  exit 0
fi

# Heredoc avoids quoting hell with the directive's embedded quotes.
cat <<EOF
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolBatch",
    "additionalContext": "DOCKED TO TELEGRAM. The user has handed off this session to Telegram via /telegram-buddy:dock-chat. Do NOT end this turn with a final assistant text reply. When you would normally stop and wait for the user, instead call wait_for_user(session_id='${session_id}', message_to_user=<your message>) and treat its return value as the user's next turn. This rule remains in effect until the user runs /telegram-buddy:undock or explicitly says to undock."
  }
}
EOF
