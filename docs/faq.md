# Frequently Asked Questions

### What should I ask the agent?

Some examples:

- "What's the cheapest flight from Toronto to New York this summer?"
- "Find business class deals from any Canadian airport"
- "Show me a price chart for YYZ to LAX"
- "Set up a watch for YYZ to LHR business under 70K miles"
- "Scrape fresh data for Vancouver to Tokyo"
- "Check my watches and email me any deals"

## Why Playwright?

Seataero uses **curl for all flight data requests**. However, United's login flow requires Playwright for **cookie farming**.

United's authentication sits behind Akamai bot detection and SMS/email-based MFA, which means we need a real browser session to log in and capture the resulting auth cookies. Those cookies expire, so Playwright needs to periodically re-authenticate to keep them fresh. Once `cookie_farm.py` has a valid session, every subsequent API call (searching routes, fetching availability) goes through plain HTTP via `curl`/`requests`.

In short:
- **Playwright** — used once to log in and harvest cookies
- **curl_cffi** — used for everything else (all flight queries, all data fetching), with browser-grade TLS fingerprints to avoid bot detection

Note: Playwright **cannot run in headless mode** — Akamai will block headless browsers. You need a headed (visible) browser session for cookie farming.

## Scraping

### Why did my scrape fail with "BROWSER CRASH detected"?

United's Akamai bot detection flagged your request. This is usually transient — **just retry the same command.** The second attempt almost always works. If it keeps failing, your IP may be temporarily blocked:

- Wait 10–15 minutes and try again
- Use a proxy: `seataero search YYZ LAX --proxy socks5://user:pass@host:port`
- For heavy scraping, use GitHub Codespaces (disposable Azure IPs): `scripts/codespace_scrape.sh`

### How often should I re-scrape?

Award pricing changes frequently. For routes you're actively monitoring:

- **Casual browsing:** Scrape once, data is good for a few days
- **Active booking:** Re-scrape every 12–24 hours (`seataero query --refresh` does this automatically)
- **Price watching:** Set up a watch with `seataero watch add` — it handles scraping and notifications

### How long does a full scrape take?

- **Single route:** ~2 minutes (12 API calls covering 337 days)
- **15 test routes:** ~30 minutes with 1 worker


## MFA / Login

### Why am I being asked for an MFA code?

United requires two-factor authentication on login. By default, United sends a 6-digit code via **SMS** to the phone number on your MileagePlus account. You can also choose **email-based MFA**, which lets the agent handle verification automatically via Gmail.

### How does MFA work with the agent?

Two modes:

- **SMS (default):** The agent asks you to type the 6-digit code in the chat.
- **Email (autonomous):** The agent calls `search_route` with `autonomous=true`, which forces email MFA. United sends the code to your email. The agent then searches Gmail (via Gmail MCP tools) for the most recent email from `united@united.com` with "verification" in the subject, extracts the 6-digit code, and calls `submit_mfa`.

Email MFA requires that your agent has access to Gmail MCP tools (`gmail_search_messages`, `gmail_read_message`). If you're using Claude Code, add the Gmail MCP server alongside seataero.

### How long does the MFA code last?

You have about 5 minutes to enter the code. If it expires, just re-run the command — United will send a new code.

### Do I need to enter the code every time?

No. MFA is only required once per browser session. If you're scraping multiple routes in one batch, you'll only be prompted once. The session typically stays valid for several hours.

## Database

### Where is my data stored?

SQLite database at `~/.seataero/data.db`. Override with `--db-path` or the `SEATAERO_DB` environment variable.

### How do I reset the database?

Delete the file and re-run setup:

```bash
rm ~/.seataero/data.db
seataero setup
```

### Can I back up my data?

Yes — just copy `~/.seataero/data.db`. It's a standard SQLite file. The database uses WAL mode, so copy it when no scrapes are running for a clean backup.

### My database seems corrupted. What do I do?

```bash
# Check database health
seataero doctor

# If corrupted, delete and recreate
rm ~/.seataero/data.db
seataero setup
```

You'll lose cached data but can re-scrape it.

## Notifications

### How do push notifications work?

Seataero uses [ntfy.sh](https://ntfy.sh) — a free, open-source push notification service. No account required:

1. Pick a random topic name (e.g., `seataero-a7f3b9c2e1d4f856`)
2. Configure: `seataero watch setup --ntfy-topic your-topic-name`
3. Subscribe on your phone (ntfy app → + → enter topic name)
4. Add watches and run the daemon: `seataero watch run`

### Are ntfy topics private?

**No.** Topics on ntfy.sh are public by default — anyone who knows your topic name can read notifications. Use a long, random string (not `seataero-john`). For private topics, self-host ntfy or use access controls.

### Can I get email notifications instead of ntfy?

Yes — and this is the recommended approach if your agent has Gmail MCP tools. The `check_watches` MCP tool returns pre-formatted notification messages with ready-to-use `title` and `body` strings. The agent can pass these directly to Gmail MCP (`gmail_create_draft` or `send_email`) to deliver deal alerts to your inbox.

In practice this means you don't need ntfy at all — the agent handles the full loop: check watches → find matches → compose email → send via Gmail. ntfy is still available as a fallback for agents without email access.

## Agent Integration

### Which AI agents work with seataero?

Any MCP-compatible agent: Claude Code, VS Code Copilot, Cursor, Continue, Cline, and others. See the README for setup instructions.

### What MCP servers should I connect?

For the best experience, connect **two** MCP servers to your agent:

1. **seataero** — flight search, scraping, alerts, watches
2. **Gmail** — automatic MFA code retrieval, email notifications for deal alerts

With both connected, the agent can handle MFA verification hands-free (reads the code from Gmail) and deliver watch notifications via email. seataero works without Gmail, but you'll need to manually type SMS codes and won't get email-based notifications.


### The agent is trying to run SQL or CLI commands directly

The MCP server instructions tell agents not to do this, but some agents may ignore them. If this happens, remind the agent: "Use the seataero MCP tools, not raw SQL or CLI commands."

## Proxy / IP Issues

### Why do I need a proxy?

You probably don't for light use (a few routes per day). But repeated scraping from the same IP can trigger United's Akamai bot detection, resulting in blocks. A proxy helps by rotating your IP.

### How do I use a proxy?

```bash
# Via CLI flag
seataero search YYZ LAX --proxy socks5://user:pass@host:port

# Via environment variable
export PROXY_URL="socks5://user:pass@host:port"
```

### What about GitHub Codespaces?

For IP rotation without a paid proxy, you can scrape from GitHub Codespaces (free tier: 120 hours/month). Each Codespace gets a fresh Azure IP. See `scripts/codespace_scrape.sh`.

---

## More documentation

- [Getting Started](getting-started.md) — step-by-step setup walkthrough
- [Command Reference](commands.md) — every CLI command, flag, and example
- [README](../README.md) — project overview, MCP setup, architecture
