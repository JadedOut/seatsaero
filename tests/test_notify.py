"""Tests for core/notify.py notification module."""

import json
import smtplib

import pytest
from unittest.mock import patch, MagicMock

from core.notify import (
    load_notify_config,
    save_notify_config,
    send_ntfy,
    send_email,
    notify_watch_matches,
)


# ---------------------------------------------------------------------------
# load_notify_config
# ---------------------------------------------------------------------------


class TestLoadNotifyConfig:
    def test_defaults_no_file_no_env(self, tmp_path, monkeypatch):
        """Without config file or env vars, returns sensible defaults."""
        monkeypatch.setattr("core.notify._CONFIG_FILE", str(tmp_path / "missing.json"))
        monkeypatch.delenv("SEATAERO_NTFY_TOPIC", raising=False)
        monkeypatch.delenv("SEATAERO_NTFY_SERVER", raising=False)

        config = load_notify_config()
        assert config["ntfy_topic"] == ""
        assert config["ntfy_server"] == "https://ntfy.sh"

    def test_reads_from_config_file(self, tmp_path, monkeypatch):
        """Reads topic and server from config.json."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "ntfy_topic": "my-topic",
            "ntfy_server": "https://custom.ntfy.example.com",
        }))
        monkeypatch.setattr("core.notify._CONFIG_FILE", str(config_file))
        monkeypatch.delenv("SEATAERO_NTFY_TOPIC", raising=False)
        monkeypatch.delenv("SEATAERO_NTFY_SERVER", raising=False)

        config = load_notify_config()
        assert config["ntfy_topic"] == "my-topic"
        assert config["ntfy_server"] == "https://custom.ntfy.example.com"

    def test_env_vars_override_file(self, tmp_path, monkeypatch):
        """Env vars take priority over config file values."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "ntfy_topic": "file-topic",
            "ntfy_server": "https://file-server.example.com",
        }))
        monkeypatch.setattr("core.notify._CONFIG_FILE", str(config_file))
        monkeypatch.setenv("SEATAERO_NTFY_TOPIC", "env-topic")
        monkeypatch.setenv("SEATAERO_NTFY_SERVER", "https://env-server.example.com")

        config = load_notify_config()
        assert config["ntfy_topic"] == "env-topic"
        assert config["ntfy_server"] == "https://env-server.example.com"

    def test_env_topic_only(self, tmp_path, monkeypatch):
        """Only topic env var set, server falls back to default."""
        monkeypatch.setattr("core.notify._CONFIG_FILE", str(tmp_path / "missing.json"))
        monkeypatch.setenv("SEATAERO_NTFY_TOPIC", "my-alerts")
        monkeypatch.delenv("SEATAERO_NTFY_SERVER", raising=False)

        config = load_notify_config()
        assert config["ntfy_topic"] == "my-alerts"
        assert config["ntfy_server"] == "https://ntfy.sh"

    def test_gmail_keys_from_config_file(self, tmp_path, monkeypatch):
        """Gmail keys read from config file."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "gmail_sender": "me@gmail.com",
            "gmail_recipient": "alerts@gmail.com",
        }))
        monkeypatch.setattr("core.notify._CONFIG_FILE", str(config_file))
        monkeypatch.delenv("SEATAERO_NTFY_TOPIC", raising=False)
        monkeypatch.delenv("SEATAERO_NTFY_SERVER", raising=False)
        monkeypatch.delenv("SEATAERO_GMAIL_SENDER", raising=False)
        monkeypatch.delenv("SEATAERO_GMAIL_RECIPIENT", raising=False)
        monkeypatch.delenv("SEATAERO_GMAIL_APP_PASSWORD", raising=False)

        config = load_notify_config()
        assert config["gmail_sender"] == "me@gmail.com"
        assert config["gmail_recipient"] == "alerts@gmail.com"
        assert config["gmail_app_password"] == ""

    def test_gmail_env_vars_override(self, tmp_path, monkeypatch):
        """Gmail env vars override config file."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "gmail_sender": "file@gmail.com",
            "gmail_recipient": "file-rcpt@gmail.com",
        }))
        monkeypatch.setattr("core.notify._CONFIG_FILE", str(config_file))
        monkeypatch.delenv("SEATAERO_NTFY_TOPIC", raising=False)
        monkeypatch.delenv("SEATAERO_NTFY_SERVER", raising=False)
        monkeypatch.setenv("SEATAERO_GMAIL_SENDER", "env@gmail.com")
        monkeypatch.setenv("SEATAERO_GMAIL_RECIPIENT", "env-rcpt@gmail.com")
        monkeypatch.delenv("SEATAERO_GMAIL_APP_PASSWORD", raising=False)

        config = load_notify_config()
        assert config["gmail_sender"] == "env@gmail.com"
        assert config["gmail_recipient"] == "env-rcpt@gmail.com"

    def test_gmail_app_password_env_only(self, tmp_path, monkeypatch):
        """App password comes from env var only, never from config file."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "gmail_app_password": "should-be-ignored",
        }))
        monkeypatch.setattr("core.notify._CONFIG_FILE", str(config_file))
        monkeypatch.delenv("SEATAERO_NTFY_TOPIC", raising=False)
        monkeypatch.delenv("SEATAERO_NTFY_SERVER", raising=False)
        monkeypatch.delenv("SEATAERO_GMAIL_SENDER", raising=False)
        monkeypatch.delenv("SEATAERO_GMAIL_RECIPIENT", raising=False)
        monkeypatch.setenv("SEATAERO_GMAIL_APP_PASSWORD", "real-app-pass")

        config = load_notify_config()
        assert config["gmail_app_password"] == "real-app-pass"


# ---------------------------------------------------------------------------
# save_notify_config
# ---------------------------------------------------------------------------


class TestSaveNotifyConfig:
    def test_creates_config_file(self, tmp_path, monkeypatch):
        """Creates config.json with topic and server."""
        config_dir = tmp_path / ".seataero"
        config_file = config_dir / "config.json"
        monkeypatch.setattr("core.notify._CONFIG_DIR", str(config_dir))
        monkeypatch.setattr("core.notify._CONFIG_FILE", str(config_file))

        save_notify_config("test-topic", "https://my.ntfy.sh")

        assert config_file.exists()
        data = json.loads(config_file.read_text())
        assert data["ntfy_topic"] == "test-topic"
        assert data["ntfy_server"] == "https://my.ntfy.sh"

    def test_preserves_existing_keys(self, tmp_path, monkeypatch):
        """Merges into existing config without clobbering other keys."""
        config_dir = tmp_path / ".seataero"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.json"
        config_file.write_text(json.dumps({"other_key": "keep_me", "ntfy_topic": "old"}))
        monkeypatch.setattr("core.notify._CONFIG_DIR", str(config_dir))
        monkeypatch.setattr("core.notify._CONFIG_FILE", str(config_file))

        save_notify_config("new-topic")

        data = json.loads(config_file.read_text())
        assert data["ntfy_topic"] == "new-topic"
        assert data["other_key"] == "keep_me"
        # ntfy_server not written because it was not provided (partial update)
        assert "ntfy_server" not in data

    def test_partial_update_only_sets_provided_keys(self, tmp_path, monkeypatch):
        """Only keys that are explicitly passed (not None) are written."""
        config_dir = tmp_path / ".seataero"
        config_file = config_dir / "config.json"
        monkeypatch.setattr("core.notify._CONFIG_DIR", str(config_dir))
        monkeypatch.setattr("core.notify._CONFIG_FILE", str(config_file))

        save_notify_config(topic="my-topic")

        data = json.loads(config_file.read_text())
        assert data["ntfy_topic"] == "my-topic"
        # Only topic was provided; server, gmail keys should not appear
        assert "ntfy_server" not in data
        assert "gmail_sender" not in data
        assert "gmail_app_password" not in data

    def test_saves_gmail_fields(self, tmp_path, monkeypatch):
        """Gmail sender and recipient are saved to config file."""
        config_dir = tmp_path / ".seataero"
        config_file = config_dir / "config.json"
        monkeypatch.setattr("core.notify._CONFIG_DIR", str(config_dir))
        monkeypatch.setattr("core.notify._CONFIG_FILE", str(config_file))

        save_notify_config(gmail_sender="a@gmail.com", gmail_recipient="b@gmail.com")

        data = json.loads(config_file.read_text())
        assert data["gmail_sender"] == "a@gmail.com"
        assert data["gmail_recipient"] == "b@gmail.com"
        assert "gmail_app_password" not in data

    def test_saves_gmail_keys(self, tmp_path, monkeypatch):
        """Gmail sender and recipient are written to config file."""
        config_dir = tmp_path / ".seataero"
        config_file = config_dir / "config.json"
        monkeypatch.setattr("core.notify._CONFIG_DIR", str(config_dir))
        monkeypatch.setattr("core.notify._CONFIG_FILE", str(config_file))

        save_notify_config(gmail_sender="me@gmail.com", gmail_recipient="you@gmail.com")

        data = json.loads(config_file.read_text())
        assert data["gmail_sender"] == "me@gmail.com"
        assert data["gmail_recipient"] == "you@gmail.com"

    def test_never_saves_app_password(self, tmp_path, monkeypatch):
        """gmail_app_password is never written to config file."""
        config_dir = tmp_path / ".seataero"
        config_file = config_dir / "config.json"
        monkeypatch.setattr("core.notify._CONFIG_DIR", str(config_dir))
        monkeypatch.setattr("core.notify._CONFIG_FILE", str(config_file))

        save_notify_config(gmail_sender="me@gmail.com")

        data = json.loads(config_file.read_text())
        assert "gmail_app_password" not in data

    def test_partial_update_preserves_existing(self, tmp_path, monkeypatch):
        """Calling with only gmail_sender preserves existing ntfy_topic."""
        config_dir = tmp_path / ".seataero"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.json"
        config_file.write_text(json.dumps({"ntfy_topic": "my-topic"}))
        monkeypatch.setattr("core.notify._CONFIG_DIR", str(config_dir))
        monkeypatch.setattr("core.notify._CONFIG_FILE", str(config_file))

        save_notify_config(gmail_sender="me@gmail.com")

        data = json.loads(config_file.read_text())
        assert data["ntfy_topic"] == "my-topic"
        assert data["gmail_sender"] == "me@gmail.com"


# ---------------------------------------------------------------------------
# send_ntfy
# ---------------------------------------------------------------------------


class TestSendNtfy:
    @patch("core.notify.urllib.request.urlopen")
    def test_success(self, mock_urlopen):
        """Successful send returns True."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"id":"abc"}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = send_ntfy("test-topic", "Title", "Body")

        assert result is True
        mock_urlopen.assert_called_once()

        # Verify header-based format
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert req.data == b"Body"
        assert req.get_header("Title") == "Title"
        assert req.get_header("Priority") == "3"

    @patch("core.notify.urllib.request.urlopen")
    def test_with_tags_and_click(self, mock_urlopen):
        """Tags and click URL are included in payload."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = send_ntfy(
            "topic", "T", "M",
            priority=5,
            tags=["airplane"],
            click="https://example.com",
        )

        assert result is True
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Tags") == "airplane"
        assert req.get_header("Click") == "https://example.com"
        assert req.get_header("Priority") == "5"

    @patch("core.notify.urllib.request.urlopen")
    def test_url_error_returns_false(self, mock_urlopen):
        """URLError returns False without raising."""
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        result = send_ntfy("topic", "Title", "Body")

        assert result is False

    @patch("core.notify.urllib.request.urlopen")
    def test_generic_exception_returns_false(self, mock_urlopen):
        """Any other exception returns False without raising."""
        mock_urlopen.side_effect = RuntimeError("Unexpected error")

        result = send_ntfy("topic", "Title", "Body")

        assert result is False

    @patch("core.notify.urllib.request.urlopen")
    def test_custom_server(self, mock_urlopen):
        """Custom server URL is used in request."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        send_ntfy("my-topic", "T", "M", server="https://custom.ntfy.example.com")

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://custom.ntfy.example.com/my-topic"


# ---------------------------------------------------------------------------
# send_email
# ---------------------------------------------------------------------------


class TestSendEmail:
    @patch("core.notify.smtplib.SMTP_SSL")
    def test_success(self, mock_smtp_cls):
        """Successful send returns True."""
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        result = send_email("me@gmail.com", "app-pass", "you@gmail.com",
                            "Test Subject", "Test body")

        assert result is True
        mock_smtp.login.assert_called_once_with("me@gmail.com", "app-pass")
        mock_smtp.sendmail.assert_called_once()
        call_args = mock_smtp.sendmail.call_args
        assert call_args[0][0] == "me@gmail.com"
        assert call_args[0][1] == ["you@gmail.com"]

    @patch("core.notify.smtplib.SMTP_SSL")
    def test_connection_error_returns_false(self, mock_smtp_cls):
        """Connection error returns False without raising."""
        mock_smtp_cls.side_effect = ConnectionRefusedError("Connection refused")

        result = send_email("me@gmail.com", "app-pass", "you@gmail.com",
                            "Subject", "Body")

        assert result is False

    @patch("core.notify.smtplib.SMTP_SSL")
    def test_auth_error_returns_false(self, mock_smtp_cls):
        """Auth error returns False without raising."""
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Auth failed")

        result = send_email("me@gmail.com", "bad-pass", "you@gmail.com",
                            "Subject", "Body")

        assert result is False

    @patch("core.notify.smtplib.SMTP_SSL")
    def test_message_format(self, mock_smtp_cls):
        """MIMEText message has correct headers."""
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        send_email("sender@gmail.com", "pass", "rcpt@gmail.com",
                   "My Subject", "Hello world")

        msg_str = mock_smtp.sendmail.call_args[0][2]
        assert "Subject: My Subject" in msg_str
        assert "From: sender@gmail.com" in msg_str
        assert "To: rcpt@gmail.com" in msg_str
        assert "Hello world" in msg_str


# ---------------------------------------------------------------------------
# notify_watch_matches
# ---------------------------------------------------------------------------


class TestNotifyWatchMatches:
    @patch("core.notify.send_ntfy")
    def test_formats_and_sends(self, mock_send):
        """Formats notification correctly and calls send_ntfy."""
        mock_send.return_value = True

        watch = {
            "origin": "YYZ",
            "destination": "LAX",
            "max_miles": 50000,
            "cabin": "business",
        }
        matches = [
            {"date": "2026-05-15", "cabin": "business", "award_type": "Saver",
             "miles": 45000, "taxes_cents": 6851},
            {"date": "2026-06-01", "cabin": "business", "award_type": "Saver",
             "miles": 48000, "taxes_cents": 6851},
        ]
        config = {"ntfy_topic": "seataero-alerts", "ntfy_server": "https://ntfy.sh"}

        result = notify_watch_matches(watch, matches, config)

        assert result is True
        mock_send.assert_called_once()

        call_kwargs = mock_send.call_args[1]
        assert call_kwargs["topic"] == "seataero-alerts"
        assert "YYZ" in call_kwargs["title"]
        assert "LAX" in call_kwargs["title"]
        assert call_kwargs["priority"] == 4
        assert call_kwargs["tags"] == ["airplane", "moneybag"]

        # Body should mention cheapest (45000) and additional matches
        assert "45,000" in call_kwargs["message"]
        assert "$68.51" in call_kwargs["message"]
        assert "1 more match" in call_kwargs["message"]
        assert "50,000" in call_kwargs["message"]

    @patch("core.notify.send_ntfy")
    def test_single_match_no_more_line(self, mock_send):
        """Single match does not include '+ N more' line."""
        mock_send.return_value = True

        watch = {"origin": "YVR", "destination": "SFO", "max_miles": 20000}
        matches = [
            {"date": "2026-07-01", "cabin": "economy", "award_type": "Saver",
             "miles": 18000, "taxes_cents": 5200},
        ]
        config = {"ntfy_topic": "alerts", "ntfy_server": "https://ntfy.sh"}

        notify_watch_matches(watch, matches, config)

        msg = mock_send.call_args[1]["message"]
        assert "more match" not in msg
        assert "18,000" in msg

    @patch("core.notify.send_ntfy")
    def test_empty_matches_returns_false(self, mock_send):
        """Empty matches list returns False without sending."""
        watch = {"origin": "YYZ", "destination": "LAX", "max_miles": 50000}
        config = {"ntfy_topic": "alerts", "ntfy_server": "https://ntfy.sh"}

        result = notify_watch_matches(watch, [], config)

        assert result is False
        mock_send.assert_not_called()

    @patch("core.notify.send_ntfy")
    def test_no_topic_returns_false(self, mock_send):
        """Missing topic returns False without sending."""
        watch = {"origin": "YYZ", "destination": "LAX", "max_miles": 50000}
        matches = [
            {"date": "2026-05-15", "cabin": "economy", "award_type": "Saver",
             "miles": 13000, "taxes_cents": 6851},
        ]
        config = {"ntfy_topic": "", "ntfy_server": "https://ntfy.sh"}

        result = notify_watch_matches(watch, matches, config)

        assert result is False
        mock_send.assert_not_called()

    @patch("core.notify.send_ntfy")
    def test_multiple_matches_plural(self, mock_send):
        """Three+ matches shows plural 'matches'."""
        mock_send.return_value = True

        watch = {"origin": "YYZ", "destination": "LAX", "max_miles": 100000}
        matches = [
            {"date": "2026-05-15", "cabin": "economy", "award_type": "Saver",
             "miles": 13000, "taxes_cents": 6851},
            {"date": "2026-05-16", "cabin": "economy", "award_type": "Saver",
             "miles": 14000, "taxes_cents": 6851},
            {"date": "2026-05-17", "cabin": "economy", "award_type": "Saver",
             "miles": 15000, "taxes_cents": 6851},
        ]
        config = {"ntfy_topic": "alerts", "ntfy_server": "https://ntfy.sh"}

        notify_watch_matches(watch, matches, config)

        msg = mock_send.call_args[1]["message"]
        assert "2 more matches" in msg

    @patch("core.notify.send_email")
    @patch("core.notify.send_ntfy")
    def test_email_sent_when_configured(self, mock_ntfy, mock_email):
        """Email is sent when all Gmail keys are configured."""
        mock_ntfy.return_value = True
        mock_email.return_value = True

        watch = {"origin": "YYZ", "destination": "LAX", "max_miles": 50000}
        matches = [
            {"date": "2026-05-15", "cabin": "economy", "award_type": "Saver",
             "miles": 13000, "taxes_cents": 6851},
        ]
        config = {
            "ntfy_topic": "alerts",
            "ntfy_server": "https://ntfy.sh",
            "gmail_sender": "me@gmail.com",
            "gmail_app_password": "app-pass",
            "gmail_recipient": "you@gmail.com",
        }

        result = notify_watch_matches(watch, matches, config)

        assert result is True
        mock_ntfy.assert_called_once()
        mock_email.assert_called_once()
        email_kwargs = mock_email.call_args[1]
        assert email_kwargs["subject"].startswith("[seataero]")
        assert email_kwargs["sender"] == "me@gmail.com"
        assert email_kwargs["recipient"] == "you@gmail.com"

    @patch("core.notify.send_email")
    @patch("core.notify.send_ntfy")
    def test_ntfy_only_when_no_gmail(self, mock_ntfy, mock_email):
        """Only ntfy is called when Gmail is not configured."""
        mock_ntfy.return_value = True

        watch = {"origin": "YYZ", "destination": "LAX", "max_miles": 50000}
        matches = [
            {"date": "2026-05-15", "cabin": "economy", "award_type": "Saver",
             "miles": 13000, "taxes_cents": 6851},
        ]
        config = {"ntfy_topic": "alerts", "ntfy_server": "https://ntfy.sh"}

        result = notify_watch_matches(watch, matches, config)

        assert result is True
        mock_ntfy.assert_called_once()
        mock_email.assert_not_called()

    @patch("core.notify.send_email")
    @patch("core.notify.send_ntfy")
    def test_email_only_when_no_ntfy(self, mock_ntfy, mock_email):
        """Only email is sent when ntfy is not configured."""
        mock_email.return_value = True

        watch = {"origin": "YYZ", "destination": "LAX", "max_miles": 50000}
        matches = [
            {"date": "2026-05-15", "cabin": "economy", "award_type": "Saver",
             "miles": 13000, "taxes_cents": 6851},
        ]
        config = {
            "ntfy_topic": "",
            "ntfy_server": "https://ntfy.sh",
            "gmail_sender": "me@gmail.com",
            "gmail_app_password": "app-pass",
            "gmail_recipient": "you@gmail.com",
        }

        result = notify_watch_matches(watch, matches, config)

        assert result is True
        mock_ntfy.assert_not_called()
        mock_email.assert_called_once()

    @patch("core.notify.send_email")
    @patch("core.notify.send_ntfy")
    def test_both_channels_one_fails(self, mock_ntfy, mock_email):
        """Returns True if one channel fails but the other succeeds."""
        mock_ntfy.return_value = True
        mock_email.return_value = False

        watch = {"origin": "YYZ", "destination": "LAX", "max_miles": 50000}
        matches = [
            {"date": "2026-05-15", "cabin": "economy", "award_type": "Saver",
             "miles": 13000, "taxes_cents": 6851},
        ]
        config = {
            "ntfy_topic": "alerts",
            "ntfy_server": "https://ntfy.sh",
            "gmail_sender": "me@gmail.com",
            "gmail_app_password": "app-pass",
            "gmail_recipient": "you@gmail.com",
        }

        result = notify_watch_matches(watch, matches, config)

        assert result is True

    @patch("core.notify.send_email")
    @patch("core.notify.send_ntfy")
    def test_no_channels_returns_false(self, mock_ntfy, mock_email):
        """Returns False when neither channel is configured."""
        watch = {"origin": "YYZ", "destination": "LAX", "max_miles": 50000}
        matches = [
            {"date": "2026-05-15", "cabin": "economy", "award_type": "Saver",
             "miles": 13000, "taxes_cents": 6851},
        ]
        config = {"ntfy_topic": "", "ntfy_server": "https://ntfy.sh"}

        result = notify_watch_matches(watch, matches, config)

        assert result is False
        mock_ntfy.assert_not_called()
        mock_email.assert_not_called()
