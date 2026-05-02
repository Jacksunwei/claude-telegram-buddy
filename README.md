# telegram-buddy

A Claude Code plugin for **bidirectional terminal ↔ phone handoff**: start work in the Claude Code terminal, step away, continue from your phone via Telegram, and seamlessly resume back at the terminal when you return.

> **Distinct from "Claude Code as Telegram chatbot" tools.** This plugin extends an *already-running* terminal session to your phone. The terminal is the persistent home; the phone is a temporary remote control.

## Install

```text
/plugin marketplace add jacksunwei/claude-telegram-buddy
/plugin install telegram-buddy@telegram-buddy
```

Then `/plugin` → telegram-buddy → **Configure options** to paste your bot token and Telegram user ID. See [`plugin/README.md`](./plugin/README.md) for full setup, configuration, and slash-command reference.

## Two modes

- **`/telegram-buddy:dock-approvals`** — buttons-on-phone for permission prompts. Two-thumb operation; great for stepping away briefly.
- **`/telegram-buddy:dock-chat`** — full conversational handoff. Every assistant turn routes through Telegram via `wait_for_user`; replies become Claude's next user message. Hook-enforced so Claude can't accidentally drop the contract.

`/telegram-buddy:undock` lifts both. Bring the session home; scrollback is intact.

## Repository layout

- `plugin/` — the Claude Code plugin tree (manifest, MCP server, slash commands, hooks, tests).
- `.claude-plugin/marketplace.json` — single-plugin marketplace manifest (this is what `/plugin marketplace add` reads).

## License

Apache-2.0 — see [LICENSE](./LICENSE).

---

> **TODO:** rewrite this README around the killer use-case story (terminal → phone → terminal round-trip) with screenshots and a daily-loop walkthrough. Today's draft is functional but not promotional.
