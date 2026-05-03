# Claude Telegram Buddy

![Telegram Buddy: hand off your Claude Code session to your phone](./docs/hero-pixel.png)

A Claude Code plugin for **driving an active Claude session from your phone when you have to step away from the desk**.

> **Not a "Claude Code as Telegram chatbot" tool.** This plugin extends an *already-running* terminal session to your
> phone. The terminal is the persistent home; the phone is a temporary remote control.

## Step away, come back — daily routine

The whole workflow is three moments, three slash commands.

**1. Before you go** — pick the mode that fits how long you'll be away:

- `/telegram-buddy:dock-approvals` — for stepping away briefly (coffee, bathroom, a quick meeting). 
  
  Phone buzzes for each permission prompt; tap ✅ Approve or ❌ Deny.
  
- `/telegram-buddy:dock-chat` — for leaving the desk entirely (lunch, errands, the rest of the afternoon). 
  
  The whole conversation routes to Telegram; your replies become Claude's next prompt.

**2. While you're gone** — Claude keeps working. Your phone is the terminal: glance, tap, reply.

**3. When you're back** — `/telegram-buddy:undock`. The terminal is exactly where you left it. Scrollback intact,
session never died.

## Install

```text
/plugin marketplace add jacksunwei/claude-telegram-buddy
/plugin install telegram-buddy@telegram-buddy
```

### First-time setup

Get two things from Telegram:

1. **Bot token** — DM [@BotFather](https://t.me/BotFather), run `/newbot`, follow the prompts, copy the HTTP API token.
1. **Your user ID** — DM [@userinfobot](https://t.me/userinfobot) and copy the `Id` it returns.

The `install` command prompts you for both — paste them in. Then **DM your bot once** (any message); Telegram requires
you to initiate before a bot can message you back.

### Re-configuring

To rotate the bot token, switch bots, or change your user ID: `/plugin` → telegram-buddy → **Configure options** →
update values → `/reload-plugins`.

## License

Apache-2.0 — see [LICENSE](./LICENSE).
