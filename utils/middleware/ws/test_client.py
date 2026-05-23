"""Tests for WebSocket client middleware."""
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))

from utils.middleware.ws.client import Client
from utils.log.types import WebSocketLog


class FakeWS:
    def __init__(self, recv_data="{}"):
        self._recv_data = recv_data
        self.connected_to = ""
        self.sent = []
        self.closed = False

    def connect(self, url, **kwargs):
        self.connected_to = url

    def send(self, payload):
        self.sent.append(payload)

    def recv(self):
        return self._recv_data

    def close(self):
        self.closed = True


class CaptureLogger:
    def __init__(self):
        self.entries = []

    def websocket_log(self, ctx, entry: WebSocketLog):
        self.entries.append(entry)


class TestWSClientMiddleware(unittest.TestCase):
    def test_connect_logged(self):
        ws = FakeWS()
        logger = CaptureLogger()
        client = Client(ws, logger=logger)

        client.connect(None, "wss://stream.binance.com/ws/btcusdt@kline_1m")

        self.assertEqual(len(logger.entries), 1)
        e = logger.entries[0]
        self.assertEqual(e.event_type, "connect")
        self.assertEqual(e.direction, "outbound")

    def test_send_logged(self):
        ws = FakeWS()
        logger = CaptureLogger()
        client = Client(ws, logger=logger)
        client._full_url = "wss://example.com/ws"
        client._url = "GET /ws"

        client.send(None, "subscribe", '{"method":"SUBSCRIBE"}')

        entries_send = [e for e in logger.entries if e.event_type == "subscribe"]
        self.assertEqual(len(entries_send), 1)
        self.assertEqual(entries_send[0].direction, "send")

    def test_recv_logged(self):
        ws = FakeWS(recv_data='{"k":{"c":"50000"}}')
        logger = CaptureLogger()
        client = Client(ws, logger=logger)
        client._full_url = "wss://example.com/ws"
        client._url = "GET /ws"

        data = client.recv(None, "kline")

        self.assertEqual(data, '{"k":{"c":"50000"}}')
        entries = [e for e in logger.entries if e.event_type == "kline"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].direction, "recv")

    def test_close_logged(self):
        ws = FakeWS()
        logger = CaptureLogger()
        client = Client(ws, logger=logger)
        client._full_url = "wss://example.com/ws"

        client.close(None)

        self.assertTrue(ws.closed)
        self.assertEqual(logger.entries[-1].event_type, "close")

    def test_no_logger_no_crash(self):
        ws = FakeWS()
        client = Client(ws, logger=None)
        client.connect(None, "wss://example.com/ws")


if __name__ == "__main__":
    unittest.main()
