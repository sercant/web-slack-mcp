"""web-slack-mcp — read Slack via Playwright over your own logged-in session.

Draft-only: there is no send code path. Reads go through Slack's internal JSON
API (conversations.history / .replies), replayed from inside the logged-in
browser context so the session cookie + boot token authenticate them — more
robust than scraping the virtualized DOM. The UI is used only to navigate (the
quick switcher resolves a channel name to its id in the URL).
"""

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from playwright.async_api import BrowserContext, Page, async_playwright

# --- config (env-overridable) ------------------------------------------------
# Reuse your own browser session: a guest can't install an app or mint tokens.
SLACK_URL = os.environ.get("SLACK_URL", "https://app.slack.com/client")
PROFILE_DIR = Path(os.environ.get("SLACK_PROFILE_DIR", ".slack-profile")).expanduser().resolve()
# Headful by default: login needs a visible window.
HEADLESS = os.environ.get("SLACK_HEADLESS", "0") == "1"
# How long a read tool waits for you to finish signing in before giving up.
LOGIN_WAIT_SECONDS = int(os.environ.get("SLACK_LOGIN_WAIT", "180"))

# Slack's app shell renders one of these when logged in; absence => logged out.
LOGGED_IN_SELECTORS = [
    '[data-qa="channel-sidebar"]',
    '[data-qa="workspace_actions_button"]',
    ".p-workspace",
]

# Slack DOM hooks for navigation/sidebar reads.
SIDEBAR_CHANNEL = '[data-qa^="channel_sidebar_name_"]'  # sidebar channel labels
MESSAGE_ITEM = '[data-qa="message_container"]'          # a rendered message
QUICK_SWITCH = "Meta+k" if sys.platform == "darwin" else "Control+k"

# Pull the workspace API token + host from Slack's boot data (the token plus the
# session cookie is all the internal API needs). Prefer the team matching the
# current /client/T… URL, else the first team that has a token.
_CREDS_JS = """
() => {
  const cfg = JSON.parse(localStorage.getItem('localConfig_v2'));
  const teams = Object.values(cfg.teams || {});
  const m = location.pathname.match(/\\/client\\/(T[A-Z0-9]+)/);
  const t = (m && teams.find(x => x && x.id === m[1]))
    || teams.find(x => x && x.token) || teams[0];
  return t ? { token: t.token, url: t.url } : null;
}
"""

mcp = FastMCP("web-slack-mcp")

# Single shared browser context — Slack dislikes many concurrent sessions.
_playwright = None
_context: BrowserContext | None = None
# user id -> display name, cached across tool calls.
_user_cache: dict[str, str] = {}


async def _get_page() -> Page:
    """Return a live page in the persistent context, booting it on first use."""
    global _playwright, _context
    if _context is None:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        _playwright = await async_playwright().start()
        launch = {
            "user_data_dir": str(PROFILE_DIR),
            "headless": HEADLESS,
            "viewport": {"width": 1280, "height": 900},
        }
        try:
            _context = await _playwright.chromium.launch_persistent_context(**launch)
        except Exception as e:
            # First run: the Chromium build isn't downloaded yet. Fetch it once
            # (it lands in Playwright's shared cache) and retry.
            if "Executable doesn't exist" not in str(e):
                raise
            _ensure_chromium()
            _context = await _playwright.chromium.launch_persistent_context(**launch)
    return _context.pages[0] if _context.pages else await _context.new_page()


def _ensure_chromium() -> None:
    """Download Playwright's Chromium build via the bundled CLI."""
    print("Installing Playwright Chromium (first run)…", file=sys.stderr)
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)


async def _is_logged_in(page: Page) -> bool:
    """True if the Slack app shell is present within a short wait."""
    # Fast path: the signed-in client lives at /client/T<team>.
    if "/client/T" in page.url:
        return True
    for selector in LOGGED_IN_SELECTORS:
        try:
            await page.wait_for_selector(selector, timeout=4000, state="attached")
            return True
        except Exception:
            continue
    return False


async def _await_login(page: Page, wait_seconds: int) -> bool:
    """Poll until the Slack app shell appears or the timeout elapses."""
    remaining = wait_seconds
    while remaining > 0:
        if await _is_logged_in(page):
            return True
        await page.wait_for_timeout(3000)
        remaining -= 3
    return False


async def _require_login(page: Page) -> None:
    """Ensure a logged-in session, opening the login window on first use.

    Already signed in: returns at once. Headful and signed out: the visible
    window is sitting on Slack's sign-in page, so we wait for you to complete it.
    Headless and signed out: raise, since there's no window to sign in through.
    """
    await page.goto(SLACK_URL, wait_until="domcontentloaded")
    if await _is_logged_in(page):
        return
    if HEADLESS:
        raise RuntimeError(
            f"Not logged into Slack (at {page.url}) and SLACK_HEADLESS=1. "
            "Restart with SLACK_HEADLESS=0 and sign in when the window opens."
        )
    if not await _await_login(page, LOGIN_WAIT_SECONDS):
        raise RuntimeError(f"Timed out waiting for Slack login (at {page.url}).")


async def _open_channel(page: Page, channel: str) -> str:
    """Jump to a channel/DM by name via the quick switcher; return its id.

    The switcher autofocuses its input, so we type blind, let results filter,
    then Enter the top hit and read the resulting /client/T…/<channel id> URL.
    """
    await page.keyboard.press(QUICK_SWITCH)
    await page.wait_for_timeout(500)
    await page.keyboard.type(channel, delay=20)
    await page.wait_for_timeout(900)  # let the result list filter to the top hit
    await page.keyboard.press("Enter")
    await page.wait_for_selector(MESSAGE_ITEM, timeout=8000)
    match = re.search(r"/client/T[A-Z0-9]+/([CDG][A-Z0-9]+)", page.url)
    if not match:
        raise RuntimeError(f"Could not resolve {channel!r} to a channel (at {page.url}).")
    return match.group(1)


async def _api(page: Page, method: str, **params: str) -> dict:
    """Call a Slack internal API method from the logged-in browser context.

    page.request shares the context's cookies, so the session authenticates
    automatically; we only add the boot token. Raises on a non-ok response.
    """
    creds = await page.evaluate(_CREDS_JS)
    if not creds or not creds.get("token"):
        raise RuntimeError("Could not find the Slack API token in boot data (localConfig_v2).")
    host = creds["url"].rstrip("/")
    resp = await page.request.post(
        f"{host}/api/{method}", multipart={"token": creds["token"], **params}
    )
    data = await resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API {method} failed: {data.get('error', 'unknown error')}")
    return data


def _name_of(user: dict) -> str:
    """Best readable name from a Slack user object, falling back to its id."""
    profile = user.get("profile") or {}
    return profile.get("display_name") or profile.get("real_name") or user.get("name") or user["id"]


async def _prime_members(page: Page, channel_id: str) -> None:
    """Prime the name cache from one conversations.view call.

    Per-id users.info is restricted for Slack Connect / external members, but the
    channel view returns their profiles inline, resolving the whole membership at
    once. Mention-only ids still fall back to users.info in _resolve_users.
    """
    try:
        users = (await _api(page, "conversations.view", channel=channel_id)).get("users", [])
    except Exception:
        return  # a failed harvest just means we lean on per-id lookups
    for user in users:
        _user_cache[user["id"]] = _name_of(user)


async def _resolve_users(page: Page, ids: set[str]) -> dict[str, str]:
    """Map user ids to display names via users.info, caching across calls."""
    for uid in [i for i in ids if i and i not in _user_cache]:
        try:
            _user_cache[uid] = _name_of((await _api(page, "users.info", user=uid))["user"])
        except Exception:
            _user_cache[uid] = uid  # fall back to the raw id rather than failing the read
    return {i: _user_cache.get(i, i) for i in ids}


def _decode(text: str, names: dict[str, str]) -> str:
    """Turn Slack mrkdwn encodings into readable text using resolved names."""
    text = re.sub(
        r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>", lambda m: "@" + names.get(m.group(1), m.group(1)), text
    )
    text = re.sub(r"<#[CG][A-Z0-9]+\|([^>]+)>", r"#\1", text)  # <#C123|name>
    text = re.sub(r"<#[CG][A-Z0-9]+>", "#channel", text)
    text = re.sub(r"<!subteam\^[A-Z0-9]+(?:\|([^>]+))?>", lambda m: m.group(1) or "@group", text)
    text = re.sub(r"<!(here|channel|everyone)>", r"@\1", text)
    text = re.sub(r"<(https?:[^>|]+)\|([^>]+)>", r"\2", text)  # <url|label>
    text = re.sub(r"<(https?:[^>]+)>", r"\1", text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _ts(ts: str | None) -> str:
    """Format a Slack epoch ts (e.g. '1783320917.618769') as local time."""
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return ts or "?"


async def _render(page: Page, messages: list[dict]) -> str:
    """Resolve names + decode text, rendering messages oldest-first."""
    if not messages:
        return "(no messages found)"
    ids: set[str] = set()
    for m in messages:
        if m.get("user"):
            ids.add(m["user"])
        ids.update(re.findall(r"<@([UW][A-Z0-9]+)", m.get("text") or ""))
    names = await _resolve_users(page, ids)
    lines = []
    for m in messages:
        who = names.get(m.get("user")) or m.get("username") or m.get("bot_id") or "system"
        lines.append(f"[{_ts(m.get('ts'))}] {who}: {_decode(m.get('text') or '', names)}")
    return "\n".join(lines)


@mcp.tool()
async def check_login() -> str:
    """Report whether the persistent browser session is logged into Slack.

    Navigates to the Slack web client and checks for the app shell. Does not
    modify anything. Run `login` first if this reports logged out.
    """
    page = await _get_page()
    await page.goto(SLACK_URL, wait_until="domcontentloaded")
    if await _is_logged_in(page):
        return f"Logged in. Current URL: {page.url}"
    return f"Not logged in (no Slack app shell at {page.url}). Run the `login` tool."


@mcp.tool()
async def login(wait_seconds: int = LOGIN_WAIT_SECONDS) -> str:
    """Open Slack in a visible window so you can log in manually.

    Optional — the read tools open this window on their own when you're signed
    out. Polls up to `wait_seconds` for the app shell after you finish SSO /
    magic-link sign-in; the session is saved to the persistent profile.
    """
    if HEADLESS:
        return "SLACK_HEADLESS=1 is set; login needs a visible window. Restart with SLACK_HEADLESS=0."
    page = await _get_page()
    await page.goto(SLACK_URL, wait_until="domcontentloaded")
    if await _await_login(page, wait_seconds):
        return f"Login detected and saved. Current URL: {page.url}"
    return f"Timed out after {wait_seconds}s waiting for login. Current URL: {page.url}"


@mcp.tool()
async def list_channels() -> str:
    """List the channels and DMs visible in your Slack sidebar.

    Read-only. Note the sidebar is virtualized, so only currently-rendered
    entries appear; collapsed or scrolled-off sections may be omitted.
    """
    page = await _get_page()
    await _require_login(page)
    # The sidebar renders lazily after the shell; wait for the first entry.
    try:
        await page.wait_for_selector(SIDEBAR_CHANNEL, timeout=8000, state="attached")
    except Exception:
        return "(no channels found in sidebar)"
    names = await page.eval_on_selector_all(
        SIDEBAR_CHANNEL, "els => els.map(e => e.innerText.trim()).filter(Boolean)"
    )
    if not names:
        return "(no channels found in sidebar)"
    # De-dupe while preserving sidebar order.
    return "\n".join(dict.fromkeys(names))


@mcp.tool()
async def read_messages(channel: str, limit: int = 20) -> str:
    """Read the most recent messages in a channel or DM.

    `channel` is matched via the quick switcher (name or partial name). Fetches
    up to `limit` most-recent messages from Slack's API and returns them
    oldest-first as `[time] Name: text`. Read-only.
    """
    page = await _get_page()
    await _require_login(page)
    channel_id = await _open_channel(page, channel)
    await _prime_members(page, channel_id)
    data = await _api(page, "conversations.history", channel=channel_id, limit=str(limit))
    # The API returns newest-first; reverse so reading top-to-bottom is chronological.
    return await _render(page, list(reversed(data.get("messages", []))))


@mcp.tool()
async def read_thread(channel: str, message_text: str, limit: int = 50) -> str:
    """Read the thread hanging off a message (parent + replies).

    Finds the most recent message in `channel` whose text contains
    `message_text`, then fetches its thread via Slack's API. Returns up to
    `limit` messages, oldest-first. Read-only.
    """
    page = await _get_page()
    await _require_login(page)
    channel_id = await _open_channel(page, channel)
    await _prime_members(page, channel_id)

    # Find the thread parent by text within a recent window, then pull replies.
    history = await _api(page, "conversations.history", channel=channel_id, limit="50")
    parent = next(
        (m for m in history.get("messages", []) if message_text in (m.get("text") or "")), None
    )
    if parent is None:
        return f"No message containing {message_text!r} found in {channel!r}."
    thread = await _api(
        page, "conversations.replies", channel=channel_id, ts=parent["ts"], limit=str(limit)
    )
    return await _render(page, thread.get("messages", []))


def main():
    mcp.run()


if __name__ == "__main__":
    main()
