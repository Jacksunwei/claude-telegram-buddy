# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Claude Code plugin that hands off an active session to Telegram so the user can step away from the desk. The repo is a **single-plugin marketplace** — `.claude-plugin/marketplace.json` is at the repo root, the actual plugin source lives in `plugin/`. Two modes coexist on the same MCP server:

- **dock-approvals** routes only `PermissionRequest` prompts to Telegram as inline-keyboard buttons.
- **dock-chat** routes every assistant turn to Telegram; replies become Claude's next prompt. Hook-enforced (see below).

## Architecture you can't infer from one file

### Multi-tenant single-host bridge (port 52891)

Every Claude Code session that subscribes runs its own MCP server (`plugin/server/server.py`). The first MCP server to bind `127.0.0.1:52891` wins the bind race and becomes the **host** — it routes Telegram I/O for *every* subscribed session, not just its own. Other servers stand by; they retry the bind every 30s, so when the host process exits the next probe finds the port free and a standby promotes automatically.

Practical consequence: the MCP tools (`dock_approvals`, `dock_chat`, `wait_for_user`, etc.) live in every session's MCP server, but the actual `getUpdates` polling and `sendMessage` calls happen only in the one process that owns the port. Tool calls in non-host sessions HTTP-POST to `http://127.0.0.1:52891/{approve,chat-message,...}` to reach the host.

### Sentinel files as the source of truth

Under `$TMPDIR/telegram-buddy/sessions/`:

- `<session_id>` — subscription marker, written by `dock_approvals` AND `dock_chat`, removed by `undock`. Presence means "route this session's PermissionRequests to Telegram."
- `<session_id>.docked` — chat-dock marker, written ONLY by `dock_chat`. Presence means "the dock contract applies to this session" — the hooks read this file to decide whether to fire.

Sentinels survive the MCP server dying, so a newly-elected host instantly knows the full subscriber + dock state without any handoff protocol.

### Hook-enforced dock contract

When a session is chat-docked, hooks in `plugin/hooks/` enforce the contract that Claude must route every turn through `wait_for_user` instead of emitting a final assistant message:

- **`dock-blocker.sh`** (Stop hook): if `<session_id>.docked` exists AND `stop_hook_active` is false, returns `decision="block"` with a `reason` redirecting Claude to call `wait_for_user`. Two safety nets — `stop_hook_active` lets the second-attempt stop through (prevents loops when `wait_for_user` itself is broken), and a `/dev/tcp` probe to port 52891 lets stops through when the bridge is unreachable (prevents wedging when the MCP server crashed).
- **`dock-reinjector.sh`** (SessionStart matcher `compact`): re-injects the dock directive into context after compaction would strip it.

The hooks read sentinel files directly and never call the MCP server. This decoupling is intentional — hooks must keep working when the MCP server is gone. The `PermissionRequest` and `PostToolUse` hooks wired in `plugin.json` are simple `curl POST` calls to the bridge; the host-side HTTP handlers decide routing based on their own read of the sentinel dir.

### Trust anchor

`TELEGRAM_BUDDY_USER_ID` (set from `userConfig.user_id` via `plugin.json`) is the trust filter. Every incoming Telegram update is checked against this ID; updates from any other Telegram user are silently discarded. In current DM-mode deployments this also doubles as the chat target. Group mode (deferred per scope) would split these — `user_id` would stay the trust anchor, `chat_id` would become the group ID.

## Common commands

```bash
# Server tests (pure-logic unit tests; uv handles deps via PEP 723 inline metadata)
./plugin/tests/test_server.py

# Hook tests (drives each hook script with realistic Claude Code event JSON,
# spins up a fake bridge listener on port 52890 to satisfy /dev/tcp probes)
./plugin/tests/test_hooks.sh

# Pre-commit (pyink + isort + mdformat + addlicense + json/yaml/toml checks)
pre-commit install   # one-time setup
pre-commit run --all-files

# Validate marketplace + plugin manifests
claude plugin validate .
claude plugin validate ./plugin

# Install this repo as a local marketplace at user scope (for dev)
claude plugin marketplace add /Users/weisun/Github/claude-telegram-buddy
claude plugin install telegram-buddy@telegram-buddy --scope user
```

## Code style

- **Python 2-space indent**, 80-char lines (enforced by `pyink --pyink-indentation=2` and `ruff.toml`).
- **Apache headers required** on all `.py` and `.sh` files (the local `addlicense` pre-commit hook fills them in).
- **README wraps at 120** chars (mdformat with gfm dialect). Only files matching `(^|/)README\.md$` are reformatted.
- **PEP 723 inline deps** are canonical: `plugin/server/server.py` declares its runtime deps in the script header so `uv run --script server.py` works standalone. `requirements-dev.txt` mirrors them for IDE jump-to-definition only — sync manually when `server.py` adds a dep.

## Design docs (external to the repo)

The server module docstring references `docs/03 Methods.md` and `docs/05 Docking Runtime.md`. These are NOT in this repo — they live in the author's Obsidian vault at `~/Library/CloudStorage/GoogleDrive-jacksunbot@gmail.com/My Drive/ClawVault/02 Projects/TelegramBuddy/` (00 Overview, 01 DX & UX, 02 Install Flow, 03 Methods, 04 Commands, 05 Docking Runtime). When the docstrings or commit messages refer to a numbered design doc, look there.
