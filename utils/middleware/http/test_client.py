"""Tests for HTTP client middleware."""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))

from utils.middleware.http.client import Client
from utils.log.types import ExtAPILog


class FakeResponse:
    def __init__(self, status_code=200, text="{}"):
        self.status_code = status_code
        self.text = text


class CaptureSQLLogger:
    def __init__(self):
        self.entries = []

    def ext_api_log(self, ctx, entry: ExtAPILog):
        self.entries.append(entry)


class FakeSession:
    def __init__(self, resp=None):
        self._resp = resp or FakeResponse()

    def request(self, method, url, **kwargs):
        self._last_kwargs = kwargs
        return self._resp


class TestHTTPClientMiddleware(unittest.TestCase):
    def test_logs_request(self):
        logger = CaptureSQLLogger()
        fake_session = FakeSession(FakeResponse(200, '{"ok":true}'))
        client = Client(logger=logger, api_name="test-api", session=fake_session)

        resp = client.request(None, "GET", "https://api.example.com/ping")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(logger.entries), 1)
        e = logger.entries[0]
        self.assertEqual(e.api_name, "test-api")
        self.assertEqual(e.http_status, 200)

    def test_session_id_propagated(self):
        from utils.log.context import set_session_id
        set_session_id("test-session-123")

        logger = CaptureSQLLogger()
        fake_session = FakeSession()
        client = Client(logger=logger, api_name="test", session=fake_session)
        client.request(None, "GET", "https://api.example.com/test")

        # Check header was added to the request
        headers = fake_session._last_kwargs.get("headers", {})
        self.assertEqual(headers.get("X-Session-ID"), "test-session-123")

        set_session_id("")

    def test_logs_on_exception(self):
        class ErrorSession:
            def request(self, *args, **kwargs):
                raise ConnectionError("network error")

        logger = CaptureSQLLogger()
        client = Client(logger=logger, api_name="test", session=ErrorSession())

        with self.assertRaises(ConnectionError):
            client.request(None, "GET", "https://api.example.com/fail")

        # Should still log even on error
        self.assertEqual(len(logger.entries), 1)
        self.assertEqual(logger.entries[0].response_body, "network error")

    def test_no_logger_no_crash(self):
        fake_session = FakeSession()
        client = Client(logger=None, api_name="test", session=fake_session)
        resp = client.request(None, "GET", "https://api.example.com/ping")
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
