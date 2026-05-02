---
description: Dock the full conversation to Telegram — drive Claude from your phone
allowed-tools: mcp__plugin_telegram-buddy_telegram-buddy__dock_chat, Bash(pwd), Bash(git rev-parse --abbrev-ref HEAD), Bash(basename *)
---

Compute a short human-readable label for this session — combine the repo basename (from `pwd` and `git rev-parse --show-toplevel` if in a git repo, else just `basename $(pwd)`) and the current branch (`git rev-parse --abbrev-ref HEAD` if in a git repo, otherwise omit). Format: `<repo> · <branch>` (or just `<repo>` if no git, or just `directory: <basename>` as a last resort).

Then call `mcp__plugin_telegram-buddy_telegram-buddy__dock_chat` with `session_id='${CLAUDE_SESSION_ID}'` and `label='<the label you computed>'`. Report what it returns verbatim.

From that point on, the harness enforces the docking contract via PostToolBatch / Stop hooks (per docs/05). You should still follow the contract proactively: do NOT end any turn with a final assistant message — every turn that would normally produce a user-visible reply must instead call `wait_for_user(session_id='${CLAUDE_SESSION_ID}', message_to_user=<your message>)` and treat its return value as the user's next message. The hook-based enforcement will redirect you if you forget, but it's friendlier to all sides if you just route correctly the first time.
