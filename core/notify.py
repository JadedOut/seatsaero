"""Notification functions for seataero watchlist alerts via ntfy and email."""

import json
import os
import smtplib
import sys
import urllib.error
import urllib.request
from email.mime.text import MIMEText


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".seataero")
_CONFIG_FILE = os.path.join(_CONFIG_DIR, "config.json")


def load_notify_config() -> dict:
    """Load notification configuration from config file and env vars.

    Priority: env vars override config file values.
    Note: gmail_app_password is loaded from env var ONLY (never from config file).

    Returns:
        Dict with keys: ntfy_topic, ntfy_server, gmail_sender,
        gmail_recipient, gmail_app_password.
    """
    config = {
        "ntfy_topic": "",
        "ntfy_server": "https://ntfy.sh",
        "gmail_sender": "",
        "gmail_recipient": "",
        "gmail_app_password": "",
    }

    # Read from config file if it exists
    if os.path.isfile(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "ntfy_topic" in data:
                config["ntfy_topic"] = data["ntfy_topic"]
            if "ntfy_server" in data:
                config["ntfy_server"] = data["ntfy_server"]
            if "gmail_sender" in data:
                config["gmail_sender"] = data["gmail_sender"]
            if "gmail_recipient" in data:
                config["gmail_recipient"] = data["gmail_recipient"]
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Warning: failed to read config: {exc}", file=sys.stderr)

    # Env var overrides
    env_topic = os.getenv("SEATAERO_NTFY_TOPIC")
    if env_topic is not None:
        config["ntfy_topic"] = env_topic

    env_server = os.getenv("SEATAERO_NTFY_SERVER")
    if env_server is not None:
        config["ntfy_server"] = env_server

    env_gmail_sender = os.getenv("SEATAERO_GMAIL_SENDER")
    if env_gmail_sender is not None:
        config["gmail_sender"] = env_gmail_sender

    env_gmail_recipient = os.getenv("SEATAERO_GMAIL_RECIPIENT")
    if env_gmail_recipient is not None:
        config["gmail_recipient"] = env_gmail_recipient

    env_gmail_password = os.getenv("SEATAERO_GMAIL_APP_PASSWORD")
    if env_gmail_password is not None:
        config["gmail_app_password"] = env_gmail_password

    return config


def save_notify_config(topic: str = None, server: str = None,
                       gmail_sender: str = None, gmail_recipient: str = None):
    """Save notification configuration to config file.

    Reads existing config first (if any), merges in provided values,
    then writes back.  Only updates keys that are provided (not None).
    Never writes gmail_app_password to disk.

    Args:
        topic: The ntfy topic string.
        server: The ntfy server URL.
        gmail_sender: Gmail sender address.
        gmail_recipient: Gmail recipient address.
    """
    os.makedirs(_CONFIG_DIR, exist_ok=True)

    # Read existing config to preserve other keys
    data = {}
    if os.path.isfile(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}

    if topic is not None:
        data["ntfy_topic"] = topic
    if server is not None:
        data["ntfy_server"] = server
    if gmail_sender is not None:
        data["gmail_sender"] = gmail_sender
    if gmail_recipient is not None:
        data["gmail_recipient"] = gmail_recipient

    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


def send_ntfy(topic: str, title: str, message: str, priority: int = 3,
              tags=None, click: str = None, server: str = "https://ntfy.sh") -> bool:
    """Send a push notification via ntfy.

    Args:
        topic: The ntfy topic to publish to.
        title: Notification title.
        message: Notification body text.
        priority: Priority 1-5 (default 3 = normal).
        tags: Optional list of emoji tag strings.
        click: Optional URL to open on notification click.
        server: The ntfy server URL (default: https://ntfy.sh).

    Returns:
        True on success, False on failure.
    """
    url = f"{server.rstrip('/')}/{topic}"

    body = message.encode("utf-8")

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Title", title)
    req.add_header("Priority", str(priority))
    if tags:
        req.add_header("Tags", ",".join(tags))
    if click:
        req.add_header("Click", click)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except urllib.error.URLError as exc:
        print(f"ntfy send failed: {exc}", file=sys.stderr)
        return False
    except Exception as exc:
        print(f"ntfy send error: {exc}", file=sys.stderr)
        return False


def send_email(sender: str, password: str, recipient: str,
               subject: str, body: str) -> bool:
    """Send an email via Gmail SMTP SSL.

    Args:
        sender: Gmail sender address.
        password: Gmail app password.
        recipient: Recipient email address.
        subject: Email subject line.
        body: Email body text.

    Returns:
        True on success, False on failure.
    """
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as smtp:
            smtp.login(sender, password)
            smtp.sendmail(sender, [recipient], msg.as_string())
        return True
    except Exception as exc:
        print(f"Email send failed: {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Watch notification formatter
# ---------------------------------------------------------------------------


def notify_watch_matches(watch: dict, matches: list, config: dict) -> bool:
    """Format and send a notification for watchlist matches.

    Args:
        watch: Dict with keys: origin, destination, max_miles, cabin, etc.
        matches: List of dicts with keys: date, cabin, award_type, miles, taxes_cents.
        config: Dict from load_notify_config() with ntfy_topic and ntfy_server.

    Returns:
        True on success, False on failure.
    """
    if not matches:
        return False

    origin = watch.get("origin", "???")
    dest = watch.get("destination", "???")
    max_miles = watch.get("max_miles", 0)

    # Find the cheapest match
    cheapest = min(matches, key=lambda m: m.get("miles", 999999))

    title = f"Award Deal: {origin} -> {dest}"

    # Format body
    miles = cheapest.get("miles", 0)
    taxes_cents = cheapest.get("taxes_cents", 0) or 0
    taxes_dollars = f"${taxes_cents / 100:.2f}"
    cabin = cheapest.get("cabin", "unknown")
    award_type = cheapest.get("award_type", "")
    date = cheapest.get("date", "")

    lines = [
        f"{cabin} {award_type}: {miles:,} miles + {taxes_dollars} on {date}",
    ]

    if len(matches) > 1:
        lines.append(f"+ {len(matches) - 1} more match{'es' if len(matches) - 1 > 1 else ''}")

    lines.append(f"Threshold: \u2264{max_miles:,} miles")

    message = "\n".join(lines)

    sent_any = False

    # Channel 1: ntfy
    topic = config.get("ntfy_topic", "")
    if topic:
        server = config.get("ntfy_server", "https://ntfy.sh")
        if send_ntfy(topic=topic, title=title, message=message,
                     priority=4, tags=["airplane", "moneybag"], server=server):
            sent_any = True

    # Channel 2: email
    gmail_sender = config.get("gmail_sender", "")
    gmail_password = config.get("gmail_app_password", "")
    gmail_recipient = config.get("gmail_recipient", "")
    if gmail_sender and gmail_password and gmail_recipient:
        email_subject = f"[seataero] {title}"
        if send_email(sender=gmail_sender, password=gmail_password,
                      recipient=gmail_recipient, subject=email_subject, body=message):
            sent_any = True

    if not sent_any:
        print("No notification channels configured, skipping notification", file=sys.stderr)

    return sent_any
