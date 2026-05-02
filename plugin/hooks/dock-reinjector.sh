#!/usr/bin/env bash
# Copyright 2026 Wei (Jack) Sun
# Licensed under the Apache License, Version 2.0 (see LICENSE).
#
# Claude Code SessionStart hook for telegram-buddy (matcher: "compact").
#
# Re-injects the docking directive after a context compaction. Compaction
# can lose conversation context including the dock-state directive that
# PostToolBatch normally repeats; this hook is the recovery point so a
# long-docked session survives compaction without dropping the contract.

set -u

SENTINEL_DIR="${TMPDIR:-/tmp}/telegram-buddy/sessions"

input="$(cat <&0)"

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

cat <<EOF
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "DOCKED TO TELEGRAM (re-injected after compaction). The user is interacting via Telegram, not the local terminal. Do NOT end any turn with a final assistant text reply. When you would normally stop and wait for the user, call wait_for_user(session_id='${session_id}', message_to_user=<your message>) and treat its return value as the user's next turn. This rule remains in effect until the user runs /telegram-buddy:undock."
  }
}
EOF
