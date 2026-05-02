---
description: Undock from Telegram — resume normal terminal replies and approvals
allowed-tools: mcp__plugin_telegram-buddy_telegram-buddy__undock
---

Call `mcp__plugin_telegram-buddy_telegram-buddy__undock` with `session_id='${CLAUDE_SESSION_ID}'`. Report what it returns.

Both modes are now lifted: approvals route to the local terminal again, and the chat-docking contract from `dock_chat` is released. Stop calling `wait_for_user`; resume producing normal assistant replies in the terminal as you would by default. The PostToolBatch and Stop hooks will see no `.docked` sentinel and immediately become no-ops for this session.
