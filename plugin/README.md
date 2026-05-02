# telegram-buddy

**Step away from the terminal. Tap Approve on your phone. Claude keeps going.**

A Claude Code plugin that diverts permission prompts to a Telegram chat. When Claude hits a `git push`, an unfamiliar
`rm`, or any other prompt-worthy call, your phone buzzes — one tap and the run resumes from wherever you are. Calls
already in your `permissions.allow` keep running silently; only the things that would actually stall Claude reach your
phone.

![Telegram prompt with Approve/Deny buttons](./docs/prompt.png)

## When you'd want this

**Step away mid-task.** The dishwasher's beeping, the kid needs pickup, the coffee run can't wait — and Claude is
halfway through a long refactor. Flip on Telegram approvals, walk out the door. When Claude hits the next `git push` or
`rm`, your phone buzzes; tap **Approve** from the sidewalk and the run keeps moving instead of stalling on an empty
terminal until you're back.

**Read without breaking flow.** Claude is mid-stream printing a wall of analysis you actually want to absorb. The moment
it hits a permission prompt, the terminal yanks your focus into a modal and your reading flow shatters. With Telegram
on, the prompt diverts to your phone — thumb-tap **Approve**, eyes never leave the scrollback.

## The daily loop

Two commands. That's the whole UX.

```
/telegram-buddy:on    # before you step away
/telegram-buddy:off   # when you're back
```

Plain language works too: *"Enable Telegram approvals"* / *"Disable Telegram approvals"*. Check current state with
`/telegram-buddy:status`.

## What it looks like

The three states a permission request goes through on Telegram:

**1. Prompt arrives.** Inline-keyboard buttons attached to the request:

![Telegram prompt with Approve/Deny buttons](./docs/prompt.png)

**2. You tap Approve.** Buttons disappear, the bubble shows ✅ Approved (or ❌ Denied), and Claude continues:

![Telegram message after tapping Approve](./docs/approved.png)

**3. You answered locally instead.** The local terminal prompt fires in parallel; if you answer there first, Claude runs
the tool and the Telegram bubble auto-clears to 🤝 Resolved without Telegram so you know the message is stale:

![Telegram message after the local prompt was answered](./docs/resolved-locally.png)

## Why this and not the official `telegram` plugin

**Works on any backend.** The official plugin's `claude/channel/permission` capability (and Claude Code's `auto` mode)
require the direct Anthropic API — on Vertex AI / Bedrock / Foundry / 3rd-party models, Claude Code drops them silently.
telegram-buddy uses a `PermissionRequest` HTTP hook instead, so it works regardless of which backend you've configured.

**Different session shape.** The official plugin extends the whole session to Telegram — input, output, and approvals
all reachable from the phone in parallel with the terminal. telegram-buddy mirrors only the permission modal; your
scrollback and editor stay the sole I/O surface, and your phone stays quiet except when Claude actually needs you.

## Setup

### 1. Create a Telegram bot

Message [@BotFather](https://t.me/BotFather) → `/newbot` → follow the prompts → save the HTTP API token.

### 2. Install the plugin

From the [`jacksunwei-plugins`](../..) marketplace:

```bash
/plugin marketplace add jacksunwei/jacksunwei-plugins
/plugin install telegram-buddy@jacksunwei-plugins
```

Install at **user scope**, not project scope. The bridge binds a fixed localhost port and uses your personal bot token,
so installing per-project would collide between collaborators on the same machine and bake your token expectation into
the project.

Claude Code prompts for two values during install:

- **Telegram Bot Token** — paste the token from BotFather. Stored in your macOS Keychain (or
  `~/.claude/.credentials.json` on Linux/Windows); never written to disk in plain text.
- **Telegram Chat ID** — your numeric Telegram user ID. Get it by messaging [@userinfobot](https://t.me/userinfobot) —
  copy the `Id` value it returns.

Reconfigure later: `/plugin list` → telegram-buddy → Configure options.

> Heads-up: Claude Code v2.1.84 has a known bug where the install prompts can be skipped. If you don't see them, use the
> `Configure options` flow above to set the values.

### 3. Open the bot's chat

DM your bot once (any message) — Telegram requires you to initiate before a bot can message you back.

## Tool reference

| Slash command            | MCP tool           | What it does                                                                                   |
| ------------------------ | ------------------ | ---------------------------------------------------------------------------------------------- |
| `/telegram-buddy:on`     | `enable_telegram`  | Subscribe this session. First subscriber binds `127.0.0.1:52891` and hosts; the rest stand by. |
| `/telegram-buddy:off`    | `disable_telegram` | Unsubscribe this session. Listener shuts down once the last subscriber leaves.                 |
| `/telegram-buddy:status` | `status`           | Show local role (host/standby/off), polling state, subscriber count, pending/decided counters. |

Tip: add the three MCP names (`mcp__plugin_telegram-buddy_telegram-buddy__*`) to `permissions.allow` in
`~/.claude/settings.json` so the slash commands don't themselves trigger a prompt.

## How it works

1. **Permission-only hook.** A `PermissionRequest` HTTP hook (declared in `plugin.json`) fires *only* when Claude Code
   is about to prompt you — calls covered by `permissions.allow` are auto-approved upstream and never reach the bridge.
1. **File-based subscriptions.** `enable_telegram` writes a sentinel file under
   `$TMPDIR/telegram-buddy/sessions/<session_id>`. The host reads this dir per request to decide whether to route, so
   non-subscribed sessions (or sessions that disabled) silently fall back to the local prompt.
1. **One host, many subscribers.** The first opted-in MCP server to bind `127.0.0.1:52891` becomes the host and serves
   every subscribed session's prompts to Telegram. The rest stand by. All subscribers share the same chat ID — there's
   only one user per install.
1. **Auto-failover.** Standbys run a 30-second heartbeat that probes the host's `/who` endpoint; when the host's process
   exits, the next probe finds the port free and standbys race for `bind()`. The OS picks one winner; the rest stay
   standby for the next round. No coordination protocol needed.
1. **Telegram round-trip.** While a host is up, the server forwards each prompt as an inline-keyboard message; the
   button callback resolves the pending request and the hook returns the decision to Claude Code.

## Configuration

Bot token and chat ID are set via the install prompts (above). Reconfigure later: `/plugin list` → telegram-buddy →
Configure options.

For standalone testing outside Claude Code, the server honors these env vars as fallbacks:

| Variable                   | Notes                                                              |
| -------------------------- | ------------------------------------------------------------------ |
| `TELEGRAM_BUDDY_BOT_TOKEN` | Bot token.                                                         |
| `TELEGRAM_BUDDY_USER_ID`   | Your Telegram user ID — trust anchor and DM target for the bot.    |

## Edge cases & limitations

- **Approval timeout** (no tap within 8h) → hook returns no decision, Claude falls back to the local prompt. The
  originating session is blocked the whole time.
- **Failover lag.** When the host's MCP process exits, standbys notice on their next heartbeat tick (default 30s) and
  one binds the port. Hooks that fire during the gap get connection-refused and fall back to the local prompt.
- **409 Conflict on promotion.** Telegram allows one `getUpdates` consumer per token. After a host swap the previous
  host's long-poll can hold the slot for up to ~30s; the new host's polling sits in `starting` and retries until it
  clears. Sends work during this window; tap callbacks queue at Telegram until polling becomes `active`.
- **Forgot to disable before closing Claude Code.** The MCP server is killed on exit, the port frees automatically, but
  any in-flight Telegram messages with active buttons become dead-ends until something else (a new host, a future tap)
  forces them to expire. Stale sentinel files in `$TMPDIR/telegram-buddy/sessions/` are harmless: they only matter while
  a host is alive to read them, and the host dies with its session.
- **Inline-keyboard buttons in DMs only** — don't add the bot to groups.

## License

Apache 2.0 — see [LICENSE](../../LICENSE).
