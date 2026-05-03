#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp[cli]>=1.27.0",
#   "aiohttp>=3.9",
#   "python-telegram-bot>=21.0",
#   "claude-agent-sdk>=0.1.0",
#   "telegramify-markdown>=1.0.0",
# ]
# ///
# Copyright 2026 Wei (Jack) Sun
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""MCP server for telegram-buddy: bidirectional terminal↔phone handoff.

Tools (see docs/03 Methods.md for full reference):
  - dock_approvals(session_id):     subscribe; route permission prompts to phone
  - dock_chat(session_id, label?):  full conversational dock; auto-subscribes
  - undock(session_id):             unsubscribe + lift dock contract
  - wait_for_user(session_id, message_to_user):  send message, await reply
  - status(session_id):             diagnostics

Two sentinel files per session under tempfile.gettempdir()/telegram-buddy/sessions/:
  <session_id>          subscription marker — set by dock_approvals AND dock_chat
  <session_id>.docked   chat-dock marker   — set ONLY by dock_chat
Hooks (see docs/05 Docking Runtime.md) read .docked to decide whether to inject
the dock directive (PostToolBatch) or block the stop (Stop).

Multi-tenant model: any number of sessions can subscribe simultaneously.
Whichever session's MCP server first wins the bind race for 127.0.0.1:52891
becomes the "host" and routes for every subscribed session. Standbys retry
the bind on a 30s heartbeat — failover is automatic when the host process
exits.

Trust anchor: TELEGRAM_BUDDY_USER_ID env var (set by plugin.json from userConfig).
Used both as the chat target for sendMessage in DM mode AND as the trust
filter — incoming Telegram updates from any other Telegram user are silently
discarded. (Group mode would split these: user_id stays the trust anchor,
chat_id becomes the group ID. Group mode is deferred per scope.)
"""

import asyncio
import html
import json
import os
import secrets
import tempfile
from dataclasses import dataclass

import telegramify_markdown
from aiohttp import ClientSession, web
from claude_agent_sdk.types import HookJSONOutput, PermissionRequestHookInput
from mcp.server.fastmcp import FastMCP
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

PORT = 52891
HOOK_TIMEOUT_S = (
    28700  # leaves headroom under the 28800s (8h) hook timeout in plugin.json
)
# Bound how long wait_for_user blocks. Matches the approval-hook ceiling so
# the operator-facing latency story is consistent across flows.
WAIT_TIMEOUT_S = 28800
# Standby retry-bind cadence. Trades failover latency vs. wakeup cost; ~30s
# means a dead host is replaced within 30s on average for any opted-in
# session that was already standby.
HEARTBEAT_INTERVAL_S = 30

# Crude file logging for diagnosing hook deliveries when MCP server stderr
# is not easily reachable. Per-PID so concurrent MCP servers don't trample.
LOG_PATH = os.path.join(tempfile.gettempdir(), f"telegram-buddy.{os.getpid()}.log")

# Sentinel directory. One file per opted-in session (subscription marker)
# plus an optional `.docked` companion when chat-dock mode is active.
# Source of truth for "should the host route this session's PermissionRequest?"
# and "should the dock hooks fire for this session?". Survives the host MCP
# server dying so a new host elected via bind-race instantly knows the full
# subscriber + dock state.
SUBSCRIPTION_DIR = os.path.join(tempfile.gettempdir(), "telegram-buddy", "sessions")


def _log(msg: str) -> None:
  try:
    with open(LOG_PATH, "a") as f:
      f.write(msg.rstrip() + "\n")
  except Exception:
    pass


# ---------- Sentinel files ----------


def _sentinel_path(session_id: str) -> str:
  return os.path.join(SUBSCRIPTION_DIR, session_id)


def _dock_sentinel_path(session_id: str) -> str:
  return os.path.join(SUBSCRIPTION_DIR, f"{session_id}.docked")


def _add_subscription(session_id: str) -> None:
  os.makedirs(SUBSCRIPTION_DIR, exist_ok=True)
  open(_sentinel_path(session_id), "w").close()


def _remove_subscription(session_id: str) -> None:
  for path in (_sentinel_path(session_id), _dock_sentinel_path(session_id)):
    try:
      os.remove(path)
    except FileNotFoundError:
      pass
    except OSError as e:
      _log(f"sentinel: remove({path}) failed: {e}")


def _add_dock(session_id: str) -> None:
  os.makedirs(SUBSCRIPTION_DIR, exist_ok=True)
  open(_dock_sentinel_path(session_id), "w").close()


def _remove_dock(session_id: str) -> None:
  try:
    os.remove(_dock_sentinel_path(session_id))
  except FileNotFoundError:
    pass
  except OSError as e:
    _log(f"sentinel: remove dock({session_id}) failed: {e}")


def _is_subscribed(session_id: str | None) -> bool:
  if not session_id:
    return False
  return os.path.exists(_sentinel_path(session_id))


def _is_docked(session_id: str | None) -> bool:
  if not session_id:
    return False
  return os.path.exists(_dock_sentinel_path(session_id))


def _subscriber_count() -> int:
  try:
    # `<session>.docked` sentinels are companions, not separate subscribers.
    return sum(1 for n in os.listdir(SUBSCRIPTION_DIR) if not n.endswith(".docked"))
  except FileNotFoundError:
    return 0


# ---------- Message rendering ----------

# Cap each interpolated field. Long attacker payloads otherwise push the
# Approve/Deny buttons off-screen on mobile.
MAX_FIELD_LEN = 1024

# Suffix labels appended to the original Telegram message body once a request
# resolves. All literal-safe (✅/❌/⏰/🤝 + ASCII).
SUFFIX_APPROVED = "✅ Approved"
SUFFIX_DENIED = "❌ Denied"
SUFFIX_EXPIRED = "⏰ Expired"
SUFFIX_RESOLVED_LOCALLY = "🤝 Resolved without Telegram"


def _esc(value, max_len: int = MAX_FIELD_LEN) -> str:
  """HTML-escape a value, truncating to bound message length.

  Critical: every interpolated field MUST go through this. Telegram parses
  the message body as HTML, and an unescaped attacker-controlled field
  could close a tag, inject formatting, and spoof a different request.
  """
  s = str(value)
  if len(s) > max_len:
    s = s[:max_len] + "…[truncated]"
  return html.escape(s)


# Length of the session-id prefix shown in Telegram message headlines.
# Six hex chars is enough to disambiguate at a glance across the small
# number of concurrently-docked sessions a single user is realistically
# juggling, while staying narrow enough to leave room for the rest of
# the headline on a phone screen.
SESSION_TAG_LEN = 6


def _session_tag(session_id) -> str:
  """Compact session identifier for the headline of every Telegram message.

  session_ids are UUIDs from Claude Code (no escaping needed for HTML).
  Falls back to '?' for missing/empty input so the tag shape stays
  consistent across all messages.
  """
  if not session_id:
    return "Session [?]"
  return f"Session [{str(session_id)[:SESSION_TAG_LEN]}]"


def _session_tag_md(session_id) -> str:
  """MarkdownV2-escaped variant of `_session_tag`.

  Used by `handle_chat_message`, which sends with parse_mode="MarkdownV2"
  because the body is rendered by `telegramify_markdown.markdownify`. The
  `[` and `]` in the tag are MarkdownV2 link delimiters and must be
  backslash-escaped; the rest of the tag is alphanumeric (UUID prefix)
  so no other escaping is needed.
  """
  if not session_id:
    return r"Session \[?\]"
  return rf"Session \[{str(session_id)[:SESSION_TAG_LEN]}\]"


def _format_request(payload: PermissionRequestHookInput, request_id: str) -> str:
  session_id = payload.get("session_id", "")
  tool = payload.get("tool_name", "?")
  inp = payload.get("tool_input") or {}
  cwd = payload.get("cwd", "?")
  preview = ""
  if isinstance(inp, dict):
    if "command" in inp:
      preview = f"\n<pre>{_esc(inp['command'])}</pre>"
    elif "file_path" in inp:
      preview = f"\n<code>{_esc(inp['file_path'])}</code>"
    elif "url" in inp:
      preview = f"\n{_esc(inp['url'])}"
  # Headline: 🔧 Session [abc123] Bash [d8bb3c]
  # Tool icon leads the headline to mirror the 💬 prefix on chat messages.
  # Body: command preview + cwd, separated from the headline by a blank line.
  return (
      f"🔧 {_session_tag(session_id)} <b>{_esc(tool)}</b> <code>[{request_id}]</code>\n"
      f"{preview}\n"
      f"<i>cwd</i>: <code>{_esc(cwd)}</code>"
  )


def _input_key(tool_name: str, tool_input) -> str:
  """Stable key for matching a PermissionRequest to its later PostToolUse.

  Claude Code does not surface a stable tool_use_id in the PermissionRequest
  payload, so we key on (tool_name, an identifying slice of tool_input).
  Whole-input JSON would be brittle — PostToolUse can carry extra fields
  PermissionRequest doesn't. Pick one identifying field per known tool.
  """
  if not isinstance(tool_input, dict):
    return tool_name
  for field in ("command", "file_path", "url"):
    if field in tool_input:
      try:
        return f"{tool_name}|{field}={json.dumps(tool_input[field], default=str)}"
      except Exception:
        return f"{tool_name}|{field}={tool_input[field]!r}"
  return tool_name


def _hook_response(decision: str) -> HookJSONOutput | dict:
  """Shape a PermissionRequest hook response.

  - 'allow' / 'deny' → structured decision Claude Code applies directly.
  - anything else → empty object → Claude Code falls back to its local prompt.
  """
  if decision == "allow":
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }
  if decision == "deny":
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "deny", "message": "Denied via Telegram"},
        }
    }
  return {}


# ---------- Bridge state ----------


@dataclass
class PendingReply:
  """A wait_for_user call awaiting a free-text Telegram reply.

  Keyed in the bridge by the Telegram message_id of the prompt we sent so an
  operator using Telegram's reply-to feature routes back to the correct
  pending entry. Plain (non-reply) text messages from the trusted user fall
  through to FIFO match against the oldest pending entry.
  """

  future: asyncio.Future
  session_id: str
  prompt_message_id: int


@dataclass
class PendingApproval:
  """A PermissionRequest awaiting resolution.

  - future: resolved by the Telegram callback (allow/deny), the PostToolUse
    cleanup (ask), or the 8h timeout.
  - text: original HTML body, kept around so we can re-render it with a
    status suffix on edit.
  - message_id: Telegram message we sent for this request.
  - input_key: stable matching key for cross-referencing against the later
    PostToolUse event for the same tool call.
  """

  future: asyncio.Future
  text: str
  message_id: int
  input_key: str


class TelegramBridge:
  """Owns the listener + Telegram poller + pending-request map.

  Single instance per process. MCP tools and HTTP handlers are thin wrappers
  that delegate here so state and lifecycle stay in one place.
  """

  def __init__(self) -> None:
    # `enabled` means "this process holds the listener" (i.e. is host).
    # A standby is opted-in (own_session_id set) but not enabled.
    self.enabled: bool = False
    # Telegram user ID — the trust anchor. Set when this process becomes
    # host. Validated against incoming update.from.id; mismatched updates
    # are silently discarded.
    self.user_id: str | None = None
    # Telegram chat ID for sendMessage routing. In DM mode this equals
    # user_id. (Group mode would set this to the group's chat_id while
    # user_id stays the human's ID; group mode is deferred.)
    self.chat_id: str | None = None
    # The Claude Code session_id this MCP process is serving. Set in
    # dock_approvals/dock_chat, cleared in undock. Each MCP server is
    # per-session, so this is at most one value per process.
    self.own_session_id: str | None = None
    self.http_runner: web.AppRunner | None = None
    self.tg_app: Application | None = None
    self.pending: dict[str, PendingApproval] = {}
    # Keyed by Telegram message_id of the prompt we sent. Insertion order
    # is the FIFO arrival order for the fallback (no reply-to) match.
    self.pending_replies: dict[int, PendingReply] = {}
    self.decided: int = 0
    self.replied: int = 0
    # Telegram polling lifecycle. Distinct from `enabled` because the
    # Telegram bot can take seconds to start polling — Telegram allows
    # one getUpdates consumer per token, and after a host swap the
    # previous host's long-poll can hold the slot for ~30s.
    # Values: "idle" | "starting" | "active" | "failed".
    self.polling_state: str = "idle"
    self.polling_error: str | None = None
    # Background task that retries bind() while we're standby.
    self.heartbeat_task: asyncio.Task | None = None

  # ---- Telegram callback ----

  async def on_callback(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data:
      return
    await q.answer()
    # Trust check: only the configured user is allowed to decide.
    if self.user_id and q.from_user and str(q.from_user.id) != str(self.user_id):
      _log(f"on_callback: discarding update from untrusted user {q.from_user.id}")
      return
    try:
      action, rid = q.data.split(":", 1)
    except ValueError:
      return
    entry = self.pending.get(rid)
    if entry and not entry.future.done():
      decision = "allow" if action == "a" else "deny"
      entry.future.set_result(decision)
      suffix = SUFFIX_APPROVED if decision == "allow" else SUFFIX_DENIED
    else:
      suffix = SUFFIX_EXPIRED
    # Re-send the original HTML source so formatting persists on edit.
    prior = entry.text if entry else None
    if prior is not None:
      try:
        await q.edit_message_text(text=f"{prior}\n\n{suffix}", parse_mode="HTML")
      except Exception:
        pass

  async def on_message(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Route a free-text Telegram message to the matching wait_for_user call.

    Match priority:
      1. Telegram reply-to one of our prompts → resolve that specific entry.
      2. FIFO: resolve the oldest pending entry. Works fine for one docked
         session; lossy if multiple are docked without using reply-to.

    Messages with no pending entry are ignored — we don't want to swallow
    chat the operator may be using for their own notes.
    """
    msg = update.message
    if not msg or not msg.text:
      return
    # Trust check against user_id (the human), NOT chat_id (which would be
    # the group in group mode).
    if self.user_id and msg.from_user and str(msg.from_user.id) != str(self.user_id):
      _log(f"on_message: discarding update from untrusted user {msg.from_user.id}")
      return

    target_id: int | None = None
    if msg.reply_to_message and msg.reply_to_message.message_id in self.pending_replies:
      target_id = msg.reply_to_message.message_id
    elif self.pending_replies:
      target_id = next(iter(self.pending_replies))

    if target_id is None:
      return

    entry = self.pending_replies.pop(target_id, None)
    if entry and not entry.future.done():
      entry.future.set_result(msg.text)
      self.replied += 1

  # ---- HTTP handlers ----

  async def handle_approve(self, request: web.Request) -> web.Response:
    payload: PermissionRequestHookInput = await request.json()
    caller = payload.get("session_id")
    if not _is_subscribed(caller):
      return web.json_response({})
    if self.tg_app is None or self.chat_id is None:
      return web.json_response({})
    rid = secrets.token_hex(3)
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    text = _format_request(payload, rid)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"a:{rid}"),
                InlineKeyboardButton("❌ Deny", callback_data=f"d:{rid}"),
            ]
        ]
    )
    try:
      sent = await self.tg_app.bot.send_message(
          chat_id=self.chat_id,
          text=text,
          reply_markup=keyboard,
          parse_mode="HTML",
      )
    except Exception:
      return web.json_response({})

    self.pending[rid] = PendingApproval(
        future=fut,
        text=text,
        message_id=sent.message_id,
        input_key=_input_key(
            payload.get("tool_name", "?"), payload.get("tool_input") or {}
        ),
    )

    try:
      decision = await asyncio.wait_for(fut, timeout=HOOK_TIMEOUT_S)
    except asyncio.TimeoutError:
      decision = "ask"
    finally:
      self.pending.pop(rid, None)

    self.decided += 1
    return web.json_response(_hook_response(decision))

  async def handle_posttooluse(self, request: web.Request) -> web.Response:
    """Cleanup endpoint for PostToolUse events.

    Fires after a tool actually runs. For each matching pending entry, edits
    the Telegram message to 'Resolved without Telegram' and resolves the
    still-open PermissionRequest hook so it stops blocking.
    """
    try:
      payload = await request.json()
    except Exception as e:
      _log(f"posttooluse: failed to parse json: {e}")
      return web.json_response({})
    caller = payload.get("session_id")
    if not _is_subscribed(caller):
      return web.json_response({})
    tool_name = payload.get("tool_name", "?")
    key = _input_key(tool_name, payload.get("tool_input") or {})
    for rid, entry in list(self.pending.items()):
      if entry.input_key != key:
        continue
      _log(f"posttooluse: matched rid={rid}")
      await self._edit_message(rid, SUFFIX_RESOLVED_LOCALLY)
      if not entry.future.done():
        entry.future.set_result("ask")
      break
    return web.json_response({})

  async def handle_chat_message(self, request: web.Request) -> web.Response:
    """Send a free-text message to Telegram and block until the operator replies.

    Body: {"session_id": str, "message_to_user": str}
    Returns: {"reply": str} on success, {"error": str} on timeout / failure.
    """
    try:
      payload = await request.json()
    except Exception as e:
      return web.json_response({"error": f"bad json: {e}"}, status=400)
    caller = payload.get("session_id")
    message_to_user = payload.get("message_to_user")
    if not isinstance(message_to_user, str) or not message_to_user.strip():
      return web.json_response({"error": "missing message_to_user"}, status=400)
    if not _is_subscribed(caller):
      return web.json_response(
          {"error": "session not subscribed; call dock_chat first"},
          status=409,
      )
    if self.tg_app is None or self.chat_id is None:
      return web.json_response({"error": "bridge not ready"}, status=503)

    # Truncate the raw message FIRST so we don't cut a code fence or link
    # in half during markdown conversion. The trailing marker shows the
    # operator that the original message was longer than this preview.
    truncated = message_to_user
    if len(truncated) > 3500:
      truncated = truncated[:3500] + "\n\n…[truncated]"

    # Convert Claude's markdown to Telegram-compatible MarkdownV2.
    # telegramify_markdown handles escaping of MarkdownV2 special chars
    # in the body and downgrades unsupported features (headers→bold,
    # lists→indented). Headline is hand-escaped via `_session_tag_md`.
    body_md = telegramify_markdown.markdownify(truncated)
    text = f"💬 {_session_tag_md(caller)}\n\n{body_md}"

    try:
      sent = await self.tg_app.bot.send_message(
          chat_id=self.chat_id,
          text=text,
          parse_mode="MarkdownV2",
      )
    except Exception as e:
      return web.json_response({"error": f"telegram send failed: {e}"}, status=502)

    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    self.pending_replies[sent.message_id] = PendingReply(
        future=fut,
        session_id=caller,
        prompt_message_id=sent.message_id,
    )
    try:
      reply = await asyncio.wait_for(fut, timeout=WAIT_TIMEOUT_S)
    except asyncio.TimeoutError:
      self.pending_replies.pop(sent.message_id, None)
      return web.json_response({"error": "timeout waiting for reply"}, status=504)
    return web.json_response({"reply": reply})

  async def handle_health(self, _request: web.Request) -> web.Response:
    """Liveness probe used by standby MCP processes for failover.

    Future extension (per docs/03 Methods.md): include a `state` field with
    the in-memory bridge state (pending approvals, pending replies, dock
    map) so promoted standbys can rehydrate routing.
    """
    return web.json_response(
        {
            "host_session_id": self.own_session_id,
            "pid": os.getpid(),
            "subscribers": _subscriber_count(),
        }
    )

  # ---- Lifecycle (called by MCP tools) ----

  async def subscribe(self, session_id: str) -> str:
    """Subscribe this session for Telegram approval routing.

    Common path for both dock_approvals and dock_chat. Adds the subscription
    sentinel and (if not already host) tries to win the bind race for PORT.
    """
    user_id = os.environ.get("TELEGRAM_BUDDY_USER_ID")
    if not user_id:
      return (
          "No user_id. Reconfigure the plugin (`/plugin` → telegram-buddy → "
          "Configure options) to set the Telegram User ID."
      )

    token = os.environ.get("TELEGRAM_BUDDY_BOT_TOKEN")
    if not token:
      return (
          "No bot token. Reconfigure the plugin (`/plugin` → telegram-buddy → "
          "Configure options) to set the Telegram Bot Token, or set the "
          "TELEGRAM_BUDDY_BOT_TOKEN env var for standalone testing."
      )

    _add_subscription(session_id)
    self.own_session_id = session_id

    if self.enabled:
      return (
          f"Already enabled (host). user_id={self.user_id} port={PORT} "
          f"subscribers={_subscriber_count()}."
      )

    if await self._try_become_host(token):
      return (
          f"Enabled (host). Approvals route to user {user_id}. "
          f"Listener on 127.0.0.1:{PORT}. Telegram polling starting in "
          f"background — check status."
      )

    self._ensure_heartbeat(token)
    return (
        f"Enabled (standby). Existing host on 127.0.0.1:{PORT} routes our "
        f"prompts; we'll take over within ~{HEARTBEAT_INTERVAL_S}s if it exits."
    )

  async def _try_become_host(self, token: str) -> bool:
    """Try to bind PORT and become the host. Returns True on success."""
    user_id = os.environ.get("TELEGRAM_BUDDY_USER_ID")
    if not user_id:
      return False
    app = web.Application()
    app.router.add_post("/approve", self.handle_approve)
    app.router.add_post("/posttooluse", self.handle_posttooluse)
    app.router.add_post("/chat-message", self.handle_chat_message)
    app.router.add_get("/health", self.handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    try:
      await site.start()
    except OSError as e:
      await runner.cleanup()
      _log(f"bind: port held: {e}")
      return False

    self.enabled = True
    self.user_id = str(user_id)
    # In DM mode, chat_id == user_id. Group mode (deferred) would set chat_id
    # to the group's ID while user_id stays the human's ID.
    self.chat_id = str(user_id)
    self.http_runner = runner
    self.polling_state = "starting"
    self.polling_error = None
    asyncio.create_task(self._start_polling_with_retry(token))
    self._stop_heartbeat()
    return True

  def _ensure_heartbeat(self, token: str) -> None:
    if self.heartbeat_task and not self.heartbeat_task.done():
      return
    self.heartbeat_task = asyncio.create_task(self._heartbeat_loop(token))

  def _stop_heartbeat(self) -> None:
    if self.heartbeat_task and not self.heartbeat_task.done():
      self.heartbeat_task.cancel()
    self.heartbeat_task = None

  async def _heartbeat_loop(self, token: str) -> None:
    """Probe /health on a tick; if the listener is gone, race to bind it.

    OS-level bind() is the election mechanism — exactly one process can
    hold the port. The standby that races first wins; the rest see
    EADDRINUSE and stay standby for the next round.
    """
    base = f"http://127.0.0.1:{PORT}"
    while self.own_session_id is not None and not self.enabled:
      try:
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)
      except asyncio.CancelledError:
        return
      if self.own_session_id is None or self.enabled:
        return
      try:
        async with ClientSession() as client:
          async with client.get(f"{base}/health", timeout=2) as resp:
            await resp.read()
        continue
      except Exception:
        pass
      if await self._try_become_host(token):
        _log("heartbeat: promoted to host")
        return

  async def _start_polling_with_retry(self, token: str, max_attempts: int = 12) -> None:
    """Start the Telegram bot, retrying on 409 Conflict.

    Telegram permits exactly one getUpdates consumer per token. After a host
    swap the previous host's long-poll can hold the slot for up to ~30s
    before its task notices the stop signal. Retry with backoff.
    """
    tg_app = Application.builder().token(token).build()
    tg_app.add_handler(CallbackQueryHandler(self.on_callback))
    # filters.TEXT & ~filters.COMMAND: free-text replies to wait_for_user.
    # Bot commands are excluded so they don't get consumed as replies.
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_message))
    try:
      await tg_app.initialize()
      await tg_app.start()
    except Exception as e:
      _log(f"polling: bot bootstrap failed: {e}")
      self.polling_state = "failed"
      self.polling_error = str(e)
      return
    self.tg_app = tg_app

    updater = tg_app.updater
    if updater is None:
      self.polling_state = "failed"
      self.polling_error = "Telegram Application has no updater"
      return

    for attempt in range(1, max_attempts + 1):
      try:
        await updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["callback_query", "message"],
        )
        self.polling_state = "active"
        self.polling_error = None
        _log(f"polling: started on attempt {attempt}")
        return
      except Exception as e:
        msg = str(e)
        is_conflict = "409" in msg or "Conflict" in msg
        if not is_conflict:
          _log(f"polling: non-409 failure on attempt {attempt}: {e}")
          self.polling_state = "failed"
          self.polling_error = msg
          return
        delay = min(0.5 + 0.5 * attempt, 5.0)
        _log(f"polling: 409 on attempt {attempt}, retrying in {delay:.1f}s")
        await asyncio.sleep(delay)

    self.polling_state = "failed"
    self.polling_error = (
        f"409 Conflict persisted after {max_attempts} attempts — another "
        "instance is still polling on this token."
    )
    _log(self.polling_error)

  async def unsubscribe(self, session_id: str) -> str:
    """Unsubscribe this session (lifts both approvals + chat dock).

    Removes BOTH sentinels (subscription + .docked). The dock hooks see no
    `.docked` sentinel and immediately return no-op for this session, which
    means the harness allows Claude to emit normal terminal replies again.
    """
    _remove_subscription(session_id)  # also removes .docked sentinel
    if self.own_session_id != session_id:
      return f"Sentinel for {session_id[:8]}… removed."

    self.own_session_id = None
    self._stop_heartbeat()

    if self.enabled and _subscriber_count() == 0:
      await self._shutdown()
      return "Disabled. Listener shut down (no remaining subscribers)."
    if self.enabled:
      return (
          f"Disabled for this session. Listener stays up serving "
          f"{_subscriber_count()} other subscriber(s)."
      )
    return "Disabled."

  def status_string(self, current_session_id: str | None = None) -> str:
    """Local-only status — what THIS MCP server's bridge instance knows."""
    if self.enabled:
      role = "host"
    elif self.own_session_id is not None:
      role = "standby"
    else:
      role = "off"
    sid = current_session_id or self.own_session_id
    parts = [
        f"role={role}",
        f"subscribed={_is_subscribed(sid)}",
        f"docked={_is_docked(sid)}",
        f"polling={self.polling_state}",
        f"user_id={self.user_id}",
        f"port={PORT}",
        f"subscribers={_subscriber_count()}",
        f"pending={len(self.pending)}",
        f"decided={self.decided}",
        f"pending_replies={len(self.pending_replies)}",
        f"replied={self.replied}",
    ]
    if self.polling_error:
      parts.append(f"polling_error={self.polling_error!r}")
    return " ".join(parts)

  async def status_with_listener(self, current_session_id: str | None) -> str:
    """Local status + a probe of /health to report the actual listener host."""
    base = self.status_string(current_session_id)
    listener: str
    try:
      async with ClientSession() as client:
        async with client.get(f"http://127.0.0.1:{PORT}/health", timeout=2) as resp:
          info = await resp.json()
      host = info.get("host_session_id")
      pid = info.get("pid")
      if not host:
        mine = "no-host"
      elif current_session_id and host == current_session_id:
        mine = "yes"
      else:
        mine = "no"
      preview = (host[:8] + "…") if host else "?"
      listener = f"listener=up listener_pid={pid} listener_host={preview} mine={mine}"
    except Exception:
      listener = "listener=down"
    return f"{base} | {listener}"

  # ---- Internal helpers ----

  async def _edit_message(self, rid: str, suffix: str) -> None:
    """Append a status suffix to the pending Telegram message for `rid`."""
    entry = self.pending.get(rid)
    if not entry or self.tg_app is None or self.chat_id is None:
      return
    try:
      await self.tg_app.bot.edit_message_text(
          chat_id=self.chat_id,
          message_id=entry.message_id,
          text=f"{entry.text}\n\n{suffix}",
          parse_mode="HTML",
      )
    except Exception:
      pass

  async def _shutdown(self) -> None:
    """Free the port first, then tear down Telegram in the background.

    The Telegram updater's long-poll can hold the connection for ~30s
    waiting for getUpdates to return. Closing the listener first frees the
    port so a standby's heartbeat can promote without waiting on the bot
    teardown, which trails asynchronously.
    """
    if not self.enabled:
      return
    for entry in list(self.pending.values()):
      if not entry.future.done():
        entry.future.set_result("ask")
    self.pending.clear()
    for reply in list(self.pending_replies.values()):
      if not reply.future.done():
        reply.future.cancel()
    self.pending_replies.clear()

    runner = self.http_runner
    tg = self.tg_app
    self._clear()

    if runner is not None:
      try:
        await runner.cleanup()
      except Exception:
        pass

    if tg is not None:

      async def _tg_shutdown():
        try:
          if tg.updater is not None:
            await tg.updater.stop()
          await tg.stop()
          await tg.shutdown()
        except Exception:
          pass

      asyncio.create_task(_tg_shutdown())

  def _clear(self) -> None:
    """Reset listener-related fields. Does NOT touch own_session_id /
    heartbeat — those reflect this process's *subscription*, which is
    independent of whether we currently host.
    """
    self.enabled = False
    self.user_id = None
    self.chat_id = None
    self.http_runner = None
    self.tg_app = None
    self.polling_state = "idle"
    self.polling_error = None


# ---------- MCP wiring ----------

mcp = FastMCP("telegram-buddy")
_bridge = TelegramBridge()


# Soft directive included in dock_chat / wait_for_user return values as
# belt-and-suspenders for the hook-enforced docking (see docs/05). The
# hooks are the primary enforcement; this string is the fallback when the
# user runs the plugin without hooks (e.g., older Claude Code versions).
_DOCK_DIRECTIVE_SHORT = (
    "DOCKED TO TELEGRAM. End every turn that would produce a user-visible "
    "reply by calling wait_for_user(session_id=..., message_to_user=...) "
    "instead of stopping. Rule remains until /telegram-buddy:undock."
)


@mcp.tool()
async def dock_approvals(session_id: str) -> str:
  """Subscribe this Claude Code session to Telegram approval routing.

  Permission prompts (PermissionRequest hooks) for this session route to
  Telegram with Approve/Deny inline buttons. Claude Code falls back to its
  local prompt for any session NOT in the subscription set.

  Args:
    session_id: Current Claude Code session_id. Used as the sentinel
      filename and matched against hook payloads at routing time.
  """
  return await _bridge.subscribe(session_id)


@mcp.tool()
async def dock_chat(session_id: str, label: str | None = None) -> str:
  """Dock this session to Telegram for full conversational handoff.

  Every assistant turn that would normally end with a terminal reply
  instead routes through wait_for_user → user replies on Telegram → the
  reply becomes Claude's next user message. Approvals continue to route as
  buttons in the same chat (subscription is auto-added).

  Writes a `.docked` sentinel that the Stop / SessionStart hooks read to
  enforce the routing contract: any attempt to end a turn with a final
  text reply gets blocked and redirected to wait_for_user.

  Args:
    session_id: Current Claude Code session_id.
    label: Optional human-readable name for the topic in group mode (e.g.
      "jacksunwei-plugins · main"). Currently a no-op in DM mode; reserved
      for future group/topic support.
  """
  result = await _bridge.subscribe(session_id)
  _add_dock(session_id)
  label_note = f" (label={label!r})" if label else ""
  return f"{result}{label_note}\n\n{_DOCK_DIRECTIVE_SHORT}"


@mcp.tool()
async def undock(session_id: str) -> str:
  """Lift dock status — both approval routing and chat docking.

  Removes both sentinels. The dock hooks immediately stop firing for this
  session (no `.docked` sentinel = no-op), so Claude can emit normal terminal
  replies again. If we're the host AND no other subscribers remain, the
  listener shuts down.

  Args:
    session_id: Current Claude Code session_id.
  """
  return await _bridge.unsubscribe(session_id)


@mcp.tool()
async def wait_for_user(session_id: str, message_to_user: str) -> str:
  """Send `message_to_user` to the docked Telegram chat; block until the operator replies.

  Use this in place of producing a final assistant message while docked.
  The return value IS the user's next message — act on it directly and
  continue the loop.

  Args:
    session_id: Current Claude Code session_id. Must already be docked
      (call dock_chat first; subscription is checked server-side).
    message_to_user: Text shown to the operator on Telegram. Phrase exactly
      as you would phrase a normal assistant turn — questions, status
      updates, partial answers; the operator sees this verbatim.

  Returns:
    The operator's reply text, with the dock directive re-appended for
    drift mitigation. Treat the reply as the user's next turn.
  """
  url = f"http://127.0.0.1:{PORT}/chat-message"
  body = {"session_id": session_id, "message_to_user": message_to_user}
  try:
    async with ClientSession() as client:
      async with client.post(url, json=body, timeout=WAIT_TIMEOUT_S + 60) as resp:
        data = await resp.json()
        if resp.status != 200:
          err = data.get("error", f"http {resp.status}")
          return f"[wait_for_user error: {err}]\n\n{_DOCK_DIRECTIVE_SHORT}"
        reply = data.get("reply", "")
  except Exception as e:
    return f"[wait_for_user transport error: {e}]\n\n{_DOCK_DIRECTIVE_SHORT}"
  return f"{reply}\n\n---\n[Reminder] {_DOCK_DIRECTIVE_SHORT}"


@mcp.tool()
async def status(session_id: str | None = None) -> str:
  """Report local bridge state plus a probe of the actual listener host.

  Local fields reflect THIS MCP server. The trailing `listener=...` segment
  is a live GET /health against 127.0.0.1:PORT, so it shows the actual
  current host even if we're a standby.
  """
  return await _bridge.status_with_listener(session_id)


if __name__ == "__main__":
  mcp.run()
