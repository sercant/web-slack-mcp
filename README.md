# web-slack-mcp

An [MCP](https://modelcontextprotocol.io) server that reads Slack through the
**web app**, driven by Playwright over *your own logged-in browser session*.

Built for the case where you're a **guest** in a workspace and can't install a
Slack app or mint API tokens — so instead of an app, it reuses the session in a
persistent Chromium profile that you log into once.

**Draft-only by design.** It reads channels, messages, and threads, and (later)
stages replies in the composer. There is no send code path in this server — it
never posts a message.

## How it works

- **Login** is manual and one-time: a visible Chromium window opens, you sign in
  (SSO / magic link / whatever your workspace uses), and the session is saved to
  a persistent profile so it survives restarts.
- **Reads** go through Slack's own internal JSON API (`conversations.history`,
  `conversations.replies`, `conversations.view`), replayed from *inside* the
  logged-in browser context so the session cookie + boot token authenticate
  them. This is far more robust than scraping the virtualized React DOM.
- **Navigation** uses the UI only to resolve a human channel name to its id (the
  quick switcher routes to `/client/T…/<channel id>`, which we read off the URL).
- **Names** — including Slack Connect / external members that per-id lookups
  won't return — are harvested in one `conversations.view` call per channel.

## Tools

| Tool | Description |
|------|-------------|
| `list_channels` | List channels/DMs visible in your sidebar. |
| `read_messages` | Read recent messages in a channel/DM (`channel`, `limit`). |
| `read_thread` | Read a thread by matching text in its parent message. |
| `search_messages` | Search messages workspace-wide with Slack search syntax. |

## Requirements

- Python **3.13+**
- [`uv`](https://docs.astral.sh/uv/) (provides `uvx`)

## Install & run

Run straight from GitHub with `uvx` — no clone needed:

```bash
uvx --from git+https://github.com/sercant/web-slack-mcp web-slack-mcp
```

On the first launch the server downloads the Chromium build Playwright drives
into Playwright's shared cache (`~/Library/Caches/ms-playwright` on macOS), so it
happens once and persists across runs.

### Register with Claude Code

```bash
claude mcp add web-slack -- \
  uvx --from git+https://github.com/sercant/web-slack-mcp web-slack-mcp
```

Then reload Claude Code; the tools appear as `mcp__web-slack__*`. The first time
you call a read tool while signed out, the server opens a visible Chromium and
waits for you to sign in — no separate step. The session is saved and reused on
later runs.

### Other MCP clients

Any client that speaks stdio works — point it at the same command:

```json
{
  "mcpServers": {
    "web-slack": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/sercant/web-slack-mcp",
        "web-slack-mcp"
      ]
    }
  }
}
```

## Configuration

All optional, via environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `SLACK_URL` | `https://app.slack.com/client` | Slack web client URL. |
| `SLACK_PROFILE_DIR` | `.slack-profile` | Where the persistent browser profile (your session) lives. |
| `SLACK_HEADLESS` | `0` | `1` runs headless. Login needs a visible window, so keep `0` for the initial sign-in. |
| `SLACK_LOGIN_WAIT` | `180` | Seconds a read tool waits for you to finish signing in before giving up. |

The profile directory holds your live Slack session — treat it like a
credential and keep it out of version control (it's gitignored here).

## Local development

```bash
git clone https://github.com/sercant/web-slack-mcp
cd web-slack-mcp
uv sync
uv run web-slack-mcp
```

## License

MIT
