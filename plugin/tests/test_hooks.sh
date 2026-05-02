#!/usr/bin/env bash
# Copyright 2026 Wei (Jack) Sun
# Licensed under the Apache License, Version 2.0 (see LICENSE).
#
# End-to-end test for the dock enforcement hook scripts. Drives each
# script with payload shapes that match the actual Claude Code hook
# event JSON (verified via Claude Code docs research) and asserts the
# expected output.
#
# Run from anywhere: ./tests/test_hooks.sh

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOKS_DIR="$(cd "$SCRIPT_DIR/../hooks" && pwd)"
SENTINEL_DIR="${TMPDIR:-/tmp}/telegram-buddy/sessions"

# Use a non-default port for the fake bridge so tests don't conflict with
# any real telegram-buddy bridge running on 52891. dock-blocker.sh reads
# TELEGRAM_BUDDY_PORT to override the probe target.
export TELEGRAM_BUDDY_PORT=52890

# Deterministic test session id; cleaned up on exit.
SID="hook-test-$$-$(date +%s)"
DOCKED_SENTINEL="$SENTINEL_DIR/$SID.docked"

LISTENER_PID=""

PASS=0
FAIL=0

cleanup() {
  rm -f "$DOCKED_SENTINEL"
  stop_fake_bridge
}
trap cleanup EXIT

start_fake_bridge() {
  # Spin up a minimal HTTP listener on the test port so the dock-blocker's
  # /dev/tcp probe sees the port as reachable. python3's http.server is
  # available everywhere we run.
  python3 -m http.server "$TELEGRAM_BUDDY_PORT" --bind 127.0.0.1 \
    >/dev/null 2>&1 &
  LISTENER_PID=$!
  # Give it ~300ms to bind. Loop probe instead of fixed sleep so fast
  # machines don't pay the full delay.
  for _ in 1 2 3 4 5 6; do
    if (exec 3<>/dev/tcp/127.0.0.1/"$TELEGRAM_BUDDY_PORT") 2>/dev/null; then
      exec 3<&- 3>&-
      return 0
    fi
    sleep 0.05
  done
  echo "WARN: fake bridge failed to bind on port $TELEGRAM_BUDDY_PORT" >&2
}

stop_fake_bridge() {
  if [[ -n "$LISTENER_PID" ]]; then
    kill "$LISTENER_PID" 2>/dev/null
    wait "$LISTENER_PID" 2>/dev/null
    LISTENER_PID=""
  fi
}

assert_json_eq() {
  local label="$1" expected="$2" actual="$3"
  # Normalize via jq -S (sorted keys, compact) for stable comparison.
  local exp_norm act_norm
  exp_norm="$(printf '%s' "$expected" | jq -Sc . 2>/dev/null || echo "INVALID")"
  act_norm="$(printf '%s' "$actual" | jq -Sc . 2>/dev/null || echo "INVALID")"
  if [[ "$exp_norm" == "$act_norm" ]]; then
    echo "  PASS: $label"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $label"
    echo "    expected: $exp_norm"
    echo "    actual:   $act_norm"
    FAIL=$((FAIL + 1))
  fi
}

assert_json_path() {
  local label="$1" path="$2" expected="$3" actual_json="$4"
  local actual
  actual="$(printf '%s' "$actual_json" | jq -r "$path" 2>/dev/null)"
  if [[ "$actual" == "$expected" ]]; then
    echo "  PASS: $label"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $label"
    echo "    path:     $path"
    echo "    expected: $expected"
    echo "    actual:   $actual"
    FAIL=$((FAIL + 1))
  fi
}

# Build a realistic PostToolBatch payload (per Claude Code docs).
ptb_payload() {
  jq -nc --arg sid "$1" '{
    session_id: $sid,
    transcript_path: "/Users/weisun/.claude/projects/test/session.jsonl",
    cwd: "/Users/weisun/Github/myproject",
    permission_mode: "default",
    hook_event_name: "PostToolBatch",
    tool_calls: [
      {
        tool_name: "Read",
        tool_input: {file_path: "/x.py"},
        tool_use_id: "toolu_01ABC",
        tool_response: " 1\tcontent\n"
      }
    ]
  }'
}

# Build a realistic Stop payload.
stop_payload() {
  local sid="$1" already_blocked="$2" last_msg="$3"
  jq -nc --arg sid "$sid" --argjson already "$already_blocked" --arg msg "$last_msg" '{
    session_id: $sid,
    transcript_path: "/Users/weisun/.claude/projects/test/session.jsonl",
    cwd: "/Users/weisun/Github/myproject",
    permission_mode: "default",
    hook_event_name: "Stop",
    stop_hook_active: $already,
    last_assistant_message: $msg
  }'
}

# Build a realistic SessionStart payload (matcher: compact).
ss_payload() {
  jq -nc --arg sid "$1" --arg src "$2" '{
    session_id: $sid,
    transcript_path: "/Users/weisun/.claude/projects/test/session.jsonl",
    cwd: "/Users/weisun/Github/myproject",
    hook_event_name: "SessionStart",
    source: $src,
    model: "claude-sonnet-4-6"
  }'
}

echo "=== dock-injector.sh (PostToolBatch) ==="

# 1. Not docked → emit empty {}.
rm -f "$DOCKED_SENTINEL"
out="$(ptb_payload "$SID" | "$HOOKS_DIR/dock-injector.sh")"
assert_json_eq "not docked → no-op" '{}' "$out"

# 2. Docked → emit additionalContext containing dock directive + session_id.
mkdir -p "$SENTINEL_DIR" && touch "$DOCKED_SENTINEL"
out="$(ptb_payload "$SID" | "$HOOKS_DIR/dock-injector.sh")"
assert_json_path "docked → hookEventName=PostToolBatch" \
  '.hookSpecificOutput.hookEventName' 'PostToolBatch' "$out"
ctx="$(printf '%s' "$out" | jq -r '.hookSpecificOutput.additionalContext')"
if [[ "$ctx" == *"$SID"* ]] && [[ "$ctx" == *"wait_for_user"* ]]; then
  echo "  PASS: docked → directive includes session_id and wait_for_user"
  PASS=$((PASS + 1))
else
  echo "  FAIL: docked → directive missing session_id or wait_for_user"
  echo "    ctx: $ctx"
  FAIL=$((FAIL + 1))
fi

# 3. Missing session_id → no-op.
out="$(echo '{}' | "$HOOKS_DIR/dock-injector.sh")"
assert_json_eq "missing session_id → no-op" '{}' "$out"

echo ""
echo "=== dock-blocker.sh (Stop) — bridge UP ==="
start_fake_bridge

# 4. Not docked → no-op.
rm -f "$DOCKED_SENTINEL"
out="$(stop_payload "$SID" false "Here is the answer" | "$HOOKS_DIR/dock-blocker.sh")"
assert_json_eq "not docked → no-op" '{}' "$out"

# 5. Docked, not already blocking, bridge up → block with reason.
touch "$DOCKED_SENTINEL"
out="$(stop_payload "$SID" false "Here is the answer" | "$HOOKS_DIR/dock-blocker.sh")"
assert_json_path "docked + bridge up → decision=block" '.decision' 'block' "$out"
reason="$(printf '%s' "$out" | jq -r '.reason')"
if [[ "$reason" == *"$SID"* ]] && [[ "$reason" == *"Here is the answer"* ]] \
   && [[ "$reason" == *"wait_for_user"* ]]; then
  echo "  PASS: docked + bridge up → reason includes session_id, last_msg, wait_for_user"
  PASS=$((PASS + 1))
else
  echo "  FAIL: docked + bridge up → reason missing required content"
  echo "    reason: $reason"
  FAIL=$((FAIL + 1))
fi

# 6. Docked but already_blocked → let stop through (loop safety).
out="$(stop_payload "$SID" true "x" | "$HOOKS_DIR/dock-blocker.sh")"
assert_json_eq "docked + stop_hook_active → no-op" '{}' "$out"

# 7. Docked, bridge up, last_msg with embedded quotes/newlines → valid JSON.
tricky_msg=$'Reply with "quotes" and a\nnewline and\\backslash'
out="$(stop_payload "$SID" false "$tricky_msg" | "$HOOKS_DIR/dock-blocker.sh")"
if printf '%s' "$out" | jq . > /dev/null 2>&1; then
  echo "  PASS: docked + tricky last_msg → valid JSON output"
  PASS=$((PASS + 1))
else
  echo "  FAIL: docked + tricky last_msg → invalid JSON output"
  echo "    raw: $out"
  FAIL=$((FAIL + 1))
fi

stop_fake_bridge

echo ""
echo "=== dock-blocker.sh (Stop) — bridge DOWN (graceful degradation) ==="
# Verify the wedge-prevention fix: even if the .docked sentinel is present,
# if the bridge is unreachable (MCP server gone), the hook must allow the
# stop instead of redirecting to a wait_for_user tool that doesn't exist.

# 8. Docked but bridge unreachable → no-op (allow stop).
touch "$DOCKED_SENTINEL"
out="$(stop_payload "$SID" false "tool is gone" | "$HOOKS_DIR/dock-blocker.sh")"
assert_json_eq "docked + bridge unreachable → no-op" '{}' "$out"

echo ""
echo "=== dock-reinjector.sh (SessionStart matcher: compact) ==="

# 8. Not docked → no-op.
rm -f "$DOCKED_SENTINEL"
out="$(ss_payload "$SID" "compact" | "$HOOKS_DIR/dock-reinjector.sh")"
assert_json_eq "not docked → no-op" '{}' "$out"

# 9. Docked → emit additionalContext.
touch "$DOCKED_SENTINEL"
out="$(ss_payload "$SID" "compact" | "$HOOKS_DIR/dock-reinjector.sh")"
assert_json_path "docked → hookEventName=SessionStart" \
  '.hookSpecificOutput.hookEventName' 'SessionStart' "$out"
ctx="$(printf '%s' "$out" | jq -r '.hookSpecificOutput.additionalContext')"
if [[ "$ctx" == *"$SID"* ]] && [[ "$ctx" == *"wait_for_user"* ]] \
   && [[ "$ctx" == *"compact"* ]]; then
  echo "  PASS: docked → directive mentions session_id, wait_for_user, compaction"
  PASS=$((PASS + 1))
else
  echo "  FAIL: docked → directive missing required content"
  echo "    ctx: $ctx"
  FAIL=$((FAIL + 1))
fi

echo ""
echo "==================================="
echo "Hook tests: $PASS passed, $FAIL failed"
echo "==================================="

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
