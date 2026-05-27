from __future__ import annotations

from unittest.mock import patch


class TestChannelPriority:
    def test_ntfy_tried_first(self):
        with patch("core.ntfy.send_alert", return_value=True) as mock_ntfy, \
             patch("core.alerts._try_gmail", return_value=False):
            from core import alerts
            alerts.send_alert("subject", "body")
        mock_ntfy.assert_called_once_with("subject", "body")

    def test_gmail_not_tried_when_ntfy_succeeds(self):
        with patch("core.ntfy.send_alert", return_value=True), \
             patch("core.alerts._try_gmail", return_value=False) as mock_gm:
            from core import alerts
            alerts.send_alert("subject", "body")
        mock_gm.assert_not_called()

    def test_gmail_tried_when_ntfy_fails(self):
        with patch("core.ntfy.send_alert", return_value=False), \
             patch("core.alerts._try_gmail", return_value=True) as mock_gm:
            from core import alerts
            alerts.send_alert("subject", "body")
        mock_gm.assert_called_once_with("subject", "body")

    def test_both_channels_attempted_when_ntfy_fails(self):
        with patch("core.ntfy.send_alert", return_value=False) as mock_ntfy, \
             patch("core.alerts._try_gmail", return_value=False) as mock_gm:
            from core import alerts
            alerts.send_alert("subject", "body")
        mock_ntfy.assert_called_once()
        mock_gm.assert_called_once()


class TestGmailHelper:
    def test_returns_false_when_env_not_set(self, monkeypatch):
        monkeypatch.delenv("GMAIL_USER",        raising=False)
        monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
        from core.alerts import _try_gmail
        assert _try_gmail("Test", "body") is False

    def test_returns_false_on_smtp_exception(self, monkeypatch):
        monkeypatch.setenv("GMAIL_USER",         "user@gmail.com")
        monkeypatch.setenv("GMAIL_APP_PASSWORD",  "app-pass")
        with patch("smtplib.SMTP_SSL", side_effect=Exception("SMTP error")):
            from core.alerts import _try_gmail
            assert _try_gmail("Test", "body") is False


class TestNtfyHelper:
    def test_returns_false_when_topic_not_set(self, monkeypatch):
        monkeypatch.delenv("NTFY_TOPIC_C", raising=False)
        from core.ntfy import send_alert
        assert send_alert("Test", "body") is False

    def test_returns_false_on_network_exception(self, monkeypatch):
        monkeypatch.setenv("NTFY_TOPIC_C", "test-topic")
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            from core.ntfy import send_alert
            assert send_alert("Test", "body") is False
