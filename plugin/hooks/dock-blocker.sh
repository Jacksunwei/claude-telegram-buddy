#!/usr/bin/env bash
# Copyright 2026 Wei (Jack) Sun
# Licensed under the Apache License, Version 2.0 (see LICENSE).
#
# Claude Code Stop hook for telegram-buddy.
#
# Hard backstop for the chat-dock contract: if Claude tries to end a turn
# with a final assistant message while the session is chat-docked, this
# hook returns decision="block" with a `reason` instructing Claude to call
# wait_for_user instead. The harness feeds the reason to Claude as a
# system-level instruction Claude must address before stopping.
#
# Loop safety: input includes `stop_hook_active` after the first block in
# a chain. We honor it — if true, we let the stop through to prevent
# infinite block loops if wait_for_user itself is broken.

set -u

SENTINEL_DIR="${TMPDIR:-/tmp}/telegram-buddy/sessions"

input="$(cat <&0)"

# jq is required because we need correct JSON escaping for the
# last_assistant_message embedded in the reason string. The fallback path
# (no jq) just emits a generic redirect that doesn't echo the original
# message — safer than risking malformed JSON in the harness response.
HAVE_JQ=0
if command -v jq >/dev/null 2>&1; then
  HAVE_JQ=1
fi

if [[ "$HAVE_JQ" -eq 1 ]]; then
  session_id="$(printf '%s' "$input" | jq -r '.session_id // ""')"
  already_blocked="$(printf '%s' "$input" | jq -r '.stop_hook_active // false')"
  last_msg="$(printf '%s' "$input" | jq -r '.last_assistant_message // ""')"
else
  session_id="$(printf '%s' "$input" | grep -o '"session_id":[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')"
  if printf '%s' "$input" | grep -q '"stop_hook_active":[[:space:]]*true'; then
    already_blocked="true"
  else
    already_blocked="false"
  fi
  last_msg="<the message you were about to emit>"
fi

if [[ -z "$session_id" ]]; then
  echo '{}'
  exit 0
fi

if [[ ! -f "${SENTINEL_DIR}/${session_id}.docked" ]] \
   || [[ "$already_blocked" == "true" ]]; then
  echo '{}'
  exit 0
fi

# Bridge-reachability probe. If the MCP server is gone (server crashed,
# Claude Code reloaded the plugin, etc.) the `wait_for_user` MCP tool we'd
# redirect Claude to is also gone, so blocking the stop wedges the user
# until stop_hook_active rescues them on the second attempt — confusing
# UX. Instead, allow the stop and log the situation so the user can
# diagnose. /dev/tcp is bash's built-in TCP probe (no curl/nc required);
# it succeeds iff something is listening on the port.
HOOK_LOG="${TMPDIR:-/tmp}/telegram-buddy-hooks.log"
BRIDGE_PORT="${TELEGRAM_BUDDY_PORT:-52891}"
if ! (exec 3<>/dev/tcp/127.0.0.1/"$BRIDGE_PORT") 2>/dev/null; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] dock-blocker: bridge unreachable on port $BRIDGE_PORT for session ${session_id:0:8} — allowing stop" >> "$HOOK_LOG" 2>/dev/null
  echo '{}'
  exit 0
fi
exec 3<&-  # close the probe socket
exec 3>&-

# Build output via jq -n so JSON escaping for the embedded message and
# session_id is correct at exactly one level (the outer JSON string).
if [[ "$HAVE_JQ" -eq 1 ]]; then
  jq -n --arg sid "$session_id" --arg msg "$last_msg" '{
    decision: "block",
    reason: ("You are docked to Telegram. Do not end this turn with a terminal reply. Instead, call wait_for_user(session_id=" + ($sid|tojson) + ", message_to_user=" + ($msg|tojson) + ") and use its return value as the user’s next turn. This rule is in effect until the user runs /telegram-buddy:undock.")
  }'
else
  cat <<EOF
{
  "decision": "block",
  "reason": "You are docked to Telegram. Do not end this turn with a terminal reply. Call wait_for_user(session_id='${session_id}', message_to_user=${last_msg}) and use its return value as the user's next turn. This rule is in effect until the user runs /telegram-buddy:undock."
}
EOF
fi
