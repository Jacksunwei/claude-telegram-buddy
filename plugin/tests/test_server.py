#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp[cli]>=1.27.0",
#   "aiohttp>=3.9",
#   "python-telegram-bot>=21.0",
#   "claude-agent-sdk>=0.1.0",
# ]
# ///
# Copyright 2026 Wei (Jack) Sun
# Licensed under the Apache License, Version 2.0 (see LICENSE).
"""Unit tests for telegram-buddy server.py.

Covers pure logic that doesn't require a live Telegram bot or HTTP listener:
  - Sentinel file write/remove invariants
  - _input_key stability and format
  - _format_request HTML escaping (XSS-style payload safety)
  - TelegramBridge.on_message reply-to vs. FIFO matching
  - TelegramBridge.on_message and on_callback user_id trust filter

Test data uses realistic shapes — e.g. Claude Code session_ids are UUIDs
and Telegram user IDs are integer-strings, matching what the bridge sees
in production.

Run: ./tests/test_server.py  (uv handles deps via PEP 723 above)
"""

import asyncio
import os
import sys
import tempfile
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Make `import server` work — server.py lives one level up.
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server")
)

import server  # noqa: E402


# Real session ids look like UUIDs in production.
SID_A = "a6b2671d-8b9f-4236-bebe-f7e12f599907"
SID_B = "f3c89132-2f1a-4b3c-9b2a-1c2d3e4f5a6b"

# Telegram user IDs are integer-strings.
USER_ID = "5312662286"
STRANGER_ID = "9999999999"


def _isolated_sentinel_dir():
  """Patch SUBSCRIPTION_DIR to a fresh temp dir for each test."""
  tmpdir = tempfile.mkdtemp(prefix="tb-test-")
  return patch.object(server, "SUBSCRIPTION_DIR", tmpdir), tmpdir


def _make_message(text, from_id, *, reply_to_message_id=None):
  """Construct a minimal Telegram Update.message-shaped object.

  python-telegram-bot's Update has many fields we don't touch — SimpleNamespace
  with just the fields the on_message handler reads is sufficient.
  """
  reply_to = (
      SimpleNamespace(message_id=reply_to_message_id)
      if reply_to_message_id is not None
      else None
  )
  msg = SimpleNamespace(
      text=text,
      from_user=SimpleNamespace(id=int(from_id)),
      reply_to_message=reply_to,
  )
  return SimpleNamespace(message=msg, callback_query=None)


def _make_callback(data, from_id):
  """Construct a minimal Update.callback_query-shaped object."""
  q = SimpleNamespace(
      data=data,
      from_user=SimpleNamespace(id=int(from_id)),
      answer=AsyncMock(),
      edit_message_text=AsyncMock(),
      message=SimpleNamespace(message_id=12345, text=""),
  )
  return SimpleNamespace(message=None, callback_query=q)


def _make_bridge(user_id=USER_ID, *, with_pending_replies=None):
  """Construct a TelegramBridge in 'host' state for testing message routing."""
  bridge = server.TelegramBridge()
  bridge.user_id = user_id
  bridge.chat_id = user_id  # DM mode: chat_id == user_id
  bridge.tg_app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
  if with_pending_replies:
    for mid, sid in with_pending_replies:
      fut = asyncio.get_event_loop().create_future()
      bridge.pending_replies[mid] = server.PendingReply(
          future=fut, session_id=sid, prompt_message_id=mid
      )
  return bridge


# ============================================================
# Sentinel helpers
# ============================================================


class TestSentinels(unittest.TestCase):

  def setUp(self):
    self._patch, self.tmpdir = _isolated_sentinel_dir()
    self._patch.start()

  def tearDown(self):
    self._patch.stop()

  def test_add_subscription_creates_file(self):
    server._add_subscription(SID_A)
    self.assertTrue(server._is_subscribed(SID_A))
    self.assertTrue(os.path.exists(server._sentinel_path(SID_A)))

  def test_add_dock_creates_companion_file(self):
    server._add_subscription(SID_A)
    server._add_dock(SID_A)
    self.assertTrue(server._is_docked(SID_A))
    self.assertTrue(server._is_subscribed(SID_A))

  def test_remove_subscription_removes_both_sentinels(self):
    """undock semantics: removing subscription also removes .docked."""
    server._add_subscription(SID_A)
    server._add_dock(SID_A)
    server._remove_subscription(SID_A)
    self.assertFalse(server._is_subscribed(SID_A))
    self.assertFalse(server._is_docked(SID_A))

  def test_remove_dock_only_removes_dock_sentinel(self):
    """Independent removal: dropping dock leaves subscription intact."""
    server._add_subscription(SID_A)
    server._add_dock(SID_A)
    server._remove_dock(SID_A)
    self.assertTrue(server._is_subscribed(SID_A))
    self.assertFalse(server._is_docked(SID_A))

  def test_is_subscribed_handles_none(self):
    self.assertFalse(server._is_subscribed(None))
    self.assertFalse(server._is_subscribed(""))

  def test_is_docked_handles_none(self):
    self.assertFalse(server._is_docked(None))
    self.assertFalse(server._is_docked(""))

  def test_subscriber_count_excludes_dock_sentinels(self):
    """A session has at most one .docked companion; counting must dedupe."""
    server._add_subscription(SID_A)
    server._add_dock(SID_A)
    server._add_subscription(SID_B)
    self.assertEqual(server._subscriber_count(), 2)

  def test_subscriber_count_zero_when_dir_missing(self):
    """Fresh install has no SUBSCRIPTION_DIR yet."""
    import shutil
    shutil.rmtree(self.tmpdir)
    self.assertEqual(server._subscriber_count(), 0)

  def test_remove_idempotent(self):
    """Removing a non-existent sentinel is not an error."""
    server._remove_subscription("nonexistent-session")
    server._remove_dock("nonexistent-session")
    # No assertion; just ensure no exception.


# ============================================================
# Pure helpers
# ============================================================


class TestInputKey(unittest.TestCase):

  def test_bash_command_keys_on_command(self):
    key = server._input_key("Bash", {"command": "git push origin main"})
    self.assertIn("Bash", key)
    self.assertIn("git push", key)

  def test_edit_keys_on_file_path(self):
    key = server._input_key("Edit", {"file_path": "/tmp/x.py", "old_string": "..."})
    self.assertIn("Edit", key)
    self.assertIn("/tmp/x.py", key)

  def test_unknown_tool_falls_back_to_name(self):
    self.assertEqual(server._input_key("UnknownTool", {"foo": "bar"}), "UnknownTool")

  def test_non_dict_input(self):
    self.assertEqual(server._input_key("X", "string-input"), "X")
    self.assertEqual(server._input_key("X", None), "X")

  def test_same_command_produces_same_key(self):
    """Stable key: PermissionRequest and PostToolUse for same call match."""
    k1 = server._input_key("Bash", {"command": "ls -la"})
    k2 = server._input_key("Bash", {"command": "ls -la"})
    self.assertEqual(k1, k2)


class TestFormatRequest(unittest.TestCase):

  def test_html_escaping_command(self):
    """Critical: tool_input.command must be HTML-escaped to prevent injection."""
    payload = {
        "session_id": SID_A,
        "tool_name": "Bash",
        "tool_input": {"command": "<script>alert('xss')</script>"},
        "cwd": "/tmp",
    }
    text = server._format_request(payload, "abc123")
    self.assertNotIn("<script>", text)
    self.assertIn("&lt;script&gt;", text)

  def test_long_field_truncation(self):
    payload = {
        "session_id": SID_A,
        "tool_name": "Bash",
        "tool_input": {"command": "x" * 5000},
        "cwd": "/tmp",
    }
    text = server._format_request(payload, "abc")
    self.assertIn("…[truncated]", text)

  def test_includes_request_id(self):
    text = server._format_request(
        {"session_id": SID_A, "tool_name": "X", "tool_input": {}, "cwd": "/"},
        "rid_xyz",
    )
    self.assertIn("rid_xyz", text)

  def test_headline_shape(self):
    """Headline shape per design: `🔧 Session [abc123] <b>Bash</b> [rid]`."""
    text = server._format_request(
        {
            "session_id": SID_A,
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "cwd": "/",
        },
        "rid",
    )
    first_line = text.split("\n", 1)[0]
    # Tool icon → session tag → tool name → request id, all on one line.
    self.assertTrue(first_line.startswith("🔧 Session ["))
    self.assertIn(SID_A[: server.SESSION_TAG_LEN], first_line)
    self.assertIn("Bash", first_line)
    self.assertIn("rid", first_line)


class TestSessionTag(unittest.TestCase):

  def test_truncates_to_session_tag_len(self):
    self.assertEqual(server._session_tag(SID_A), f"Session [{SID_A[:6]}]")

  def test_handles_missing_session(self):
    self.assertEqual(server._session_tag(None), "Session [?]")
    self.assertEqual(server._session_tag(""), "Session [?]")

  def test_short_session_id_does_not_pad(self):
    self.assertEqual(server._session_tag("abc"), "Session [abc]")


# ============================================================
# Bridge message routing — reply-to and FIFO matching
# ============================================================


class TestOnMessage(unittest.IsolatedAsyncioTestCase):

  async def test_reply_to_resolves_specific_pending(self):
    bridge = _make_bridge(
        with_pending_replies=[(100, SID_A), (200, SID_B), (300, SID_A)]
    )
    msg = _make_message(
        "answer for B", from_id=USER_ID, reply_to_message_id=200
    )
    await bridge.on_message(msg, None)
    # The reply-to entry was resolved; the others remain.
    self.assertNotIn(200, bridge.pending_replies)
    self.assertIn(100, bridge.pending_replies)
    self.assertIn(300, bridge.pending_replies)
    self.assertEqual(bridge.replied, 1)

  async def test_no_reply_to_uses_fifo(self):
    """No reply-to → oldest pending entry wins (insertion-order dict)."""
    bridge = _make_bridge(with_pending_replies=[(100, SID_A), (200, SID_B)])
    msg = _make_message("plain reply", from_id=USER_ID)
    await bridge.on_message(msg, None)
    # 100 was inserted first → it wins FIFO.
    self.assertNotIn(100, bridge.pending_replies)
    self.assertIn(200, bridge.pending_replies)

  async def test_no_pending_replies_ignored(self):
    """Operator's free chat with no pending wait is silently discarded."""
    bridge = _make_bridge()
    msg = _make_message("just chatting", from_id=USER_ID)
    await bridge.on_message(msg, None)
    self.assertEqual(bridge.replied, 0)

  async def test_stranger_message_discarded(self):
    """User_id trust check: messages from any other user are ignored."""
    bridge = _make_bridge(with_pending_replies=[(100, SID_A)])
    msg = _make_message("malicious reply", from_id=STRANGER_ID)
    await bridge.on_message(msg, None)
    # Pending entry is untouched.
    self.assertIn(100, bridge.pending_replies)
    self.assertEqual(bridge.replied, 0)

  async def test_empty_text_ignored(self):
    """Photo/sticker messages have msg.text=None → skip without error."""
    bridge = _make_bridge(with_pending_replies=[(100, SID_A)])
    msg_with_no_text = SimpleNamespace(
        message=SimpleNamespace(
            text=None,
            from_user=SimpleNamespace(id=int(USER_ID)),
            reply_to_message=None,
        ),
        callback_query=None,
    )
    await bridge.on_message(msg_with_no_text, None)
    self.assertIn(100, bridge.pending_replies)

  async def test_reply_to_unknown_message_falls_to_fifo(self):
    """If reply_to.message_id isn't in our pending dict, fall back to FIFO."""
    bridge = _make_bridge(with_pending_replies=[(100, SID_A), (200, SID_B)])
    msg = _make_message(
        "reply to a different bot", from_id=USER_ID, reply_to_message_id=999
    )
    await bridge.on_message(msg, None)
    # Falls through to FIFO → 100 (oldest) wins.
    self.assertNotIn(100, bridge.pending_replies)


# ============================================================
# Bridge callback handling — Approve/Deny taps
# ============================================================


class TestOnCallback(unittest.IsolatedAsyncioTestCase):

  async def test_approve_resolves_pending(self):
    bridge = _make_bridge()
    fut = asyncio.get_event_loop().create_future()
    bridge.pending["abc"] = server.PendingApproval(
        future=fut, text="<b>fake prompt</b>", message_id=42, input_key="Bash|cmd"
    )
    cb = _make_callback("a:abc", from_id=USER_ID)
    await bridge.on_callback(cb, None)
    self.assertTrue(fut.done())
    self.assertEqual(fut.result(), "allow")

  async def test_deny_resolves_pending(self):
    bridge = _make_bridge()
    fut = asyncio.get_event_loop().create_future()
    bridge.pending["abc"] = server.PendingApproval(
        future=fut, text="x", message_id=42, input_key="x"
    )
    cb = _make_callback("d:abc", from_id=USER_ID)
    await bridge.on_callback(cb, None)
    self.assertEqual(fut.result(), "deny")

  async def test_stranger_callback_discarded(self):
    bridge = _make_bridge()
    fut = asyncio.get_event_loop().create_future()
    bridge.pending["abc"] = server.PendingApproval(
        future=fut, text="x", message_id=42, input_key="x"
    )
    cb = _make_callback("a:abc", from_id=STRANGER_ID)
    await bridge.on_callback(cb, None)
    # Trust check rejected the tap; future remains pending.
    self.assertFalse(fut.done())

  async def test_callback_for_unknown_rid_does_not_crash(self):
    """Late-arriving tap for an already-resolved/expired entry."""
    bridge = _make_bridge()
    cb = _make_callback("a:nonexistent", from_id=USER_ID)
    # Should not raise; just edits the message with EXPIRED suffix.
    await bridge.on_callback(cb, None)


# ============================================================
# Status string includes new docked field
# ============================================================


class TestStatusString(unittest.TestCase):

  def setUp(self):
    self._patch, _ = _isolated_sentinel_dir()
    self._patch.start()

  def tearDown(self):
    self._patch.stop()

  def test_status_off_session(self):
    bridge = server.TelegramBridge()
    s = bridge.status_string(SID_A)
    self.assertIn("role=off", s)
    self.assertIn("subscribed=False", s)
    self.assertIn("docked=False", s)

  def test_status_subscribed_only(self):
    server._add_subscription(SID_A)
    bridge = server.TelegramBridge()
    s = bridge.status_string(SID_A)
    self.assertIn("subscribed=True", s)
    self.assertIn("docked=False", s)

  def test_status_chat_docked(self):
    server._add_subscription(SID_A)
    server._add_dock(SID_A)
    bridge = server.TelegramBridge()
    s = bridge.status_string(SID_A)
    self.assertIn("subscribed=True", s)
    self.assertIn("docked=True", s)


if __name__ == "__main__":
  unittest.main(verbosity=2)
