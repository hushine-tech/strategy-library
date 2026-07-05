"""
Tests for the Python logging SDK.

Covers:
- All 10 log types produce correctly-keyed JSON entries
- Kafka backend switch: entries are forwarded when backend is configured
- Exception isolation: Kafka backend failure does not affect file logging
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from utils.log.logger import Logger, _KafkaBackend
from utils.log.types import (
    AccessLog,
    ExtAPILog,
    GRPCAccessLog,
    GRPCExtLog,
    KafkaRecvLog,
    KafkaSentLog,
    Level,
    SQLLog,
    Type,
    WebSocketLog,
)


def _read_log(path: str) -> list:
    """Read all JSON lines from a log file."""
    entries = []
    if not os.path.exists(path):
        return entries
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


class TestAllLogTypes(unittest.TestCase):
    """Each of the 10 log type methods must produce correct JSON output."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.logger = Logger(self.tmp, list(Type))

    def tearDown(self):
        self.logger.close()

    def _entry(self, log_type: Type) -> dict:
        entries = _read_log(os.path.join(self.tmp, log_type.filename()))
        self.assertEqual(len(entries), 1, f"expected 1 entry in {log_type.filename()}")
        return entries[0]

    def test_info_system(self):
        self.logger.info(None, Type.SYSTEM, "system start")
        e = self._entry(Type.SYSTEM)
        self.assertEqual(e["level"], "INFO")
        self.assertEqual(e["type"], "system")
        self.assertIn("message", e)
        self.assertIn("timestamp", e)
        self.assertIn("session_id", e)

    def test_debug(self):
        self.logger.debug(None, Type.ROOT, "debug msg")
        e = self._entry(Type.ROOT)
        self.assertEqual(e["level"], "DEBUG")

    def test_warn(self):
        self.logger.warn(None, Type.ROOT, "warn msg")
        e = self._entry(Type.ROOT)
        self.assertEqual(e["level"], "WARN")

    def test_error(self):
        self.logger.error(None, Type.ROOT, "error msg")
        e = self._entry(Type.ROOT)
        self.assertEqual(e["level"], "ERROR")

    def test_fatal(self):
        self.logger.fatal(None, Type.ROOT, "fatal msg")
        e = self._entry(Type.ROOT)
        self.assertEqual(e["level"], "FATAL")

    def test_access_log(self):
        self.logger.access_log(None, AccessLog(
            method="GET", path="/api/test",
            request_header={"x-session-id": "s1"},
            request_params={"q": "1"},
            request_body="", response_body='{"ok":true}',
            http_status=200, latency_ms=42,
        ))
        e = self._entry(Type.ACCESS)
        self.assertEqual(e["type"], "access")
        self.assertEqual(e["method"], "GET")
        self.assertEqual(e["path"], "/api/test")
        self.assertEqual(e["http_status"], 200)
        self.assertEqual(e["latency_ms"], 42)

    def test_ext_api_log(self):
        self.logger.ext_api_log(None, ExtAPILog(
            api_name="binance", url="GET /api/v3/klines",
            full_url="https://api.binance.com/api/v3/klines?symbol=BTCUSDT",
            request_header={}, request_params={},
            request_body="", response_body="[]",
            http_status=200, latency_ms=100,
        ))
        e = self._entry(Type.EXT_API)
        self.assertEqual(e["type"], "ext_api")
        self.assertEqual(e["api_name"], "binance")
        self.assertIn("full_url", e)

    def test_websocket_log(self):
        self.logger.websocket_log(None, WebSocketLog(
            url="GET /ws/btcusdt@kline_1m",
            full_url="wss://stream.binance.com/ws/btcusdt@kline_1m",
            event_type="kline", direction="recv",
            frame='{"k":{"c":"50000"}}', latency_ms=5,
        ))
        e = self._entry(Type.WEBSOCKET)
        self.assertEqual(e["type"], "websocket")
        self.assertEqual(e["event_type"], "kline")
        self.assertEqual(e["direction"], "recv")

    def test_sql_log(self):
        self.logger.sql_log(None, SQLLog(
            statement="INSERT INTO orders VALUES ($1)", rows_affected=1, latency_ms=3,
        ))
        e = self._entry(Type.SQL)
        self.assertEqual(e["type"], "sql")
        self.assertEqual(e["rows_affected"], 1)

    def test_grpc_access_log(self):
        self.logger.grpc_access_log(None, GRPCAccessLog(
            method="/portfolio.v1.PortfolioService/GetPortfolio",
            client_ip="127.0.0.1", latency_ms=8,
            status_code=0, error="",
            request_params={"id": "abc"}, response={"balance": 100},
        ))
        e = self._entry(Type.GRPC_ACCESS)
        self.assertEqual(e["type"], "grpc_access")
        self.assertEqual(e["method"], "/portfolio.v1.PortfolioService/GetPortfolio")
        self.assertEqual(e["status_code"], 0)

    def test_grpc_ext_log(self):
        self.logger.grpc_ext_log(None, GRPCExtLog(
            method="/portfolio.v1.PortfolioService/GetPortfolio",
            target_service="core-service:50051",
            latency_ms=12, status_code=0, error="",
        ))
        e = self._entry(Type.GRPC_EXT)
        self.assertEqual(e["type"], "grpc_ext")
        self.assertEqual(e["target_service"], "core-service:50051")

    def test_kafka_sent_log(self):
        self.logger.kafka_sent_log(None, KafkaSentLog(
            topic="market-data", partition=0, offset=123,
            message_size=256, data={"symbol": "BTCUSDT"},
        ))
        e = self._entry(Type.KAFKA_SENT)
        self.assertEqual(e["type"], "kafka_sent")
        self.assertEqual(e["topic"], "market-data")
        self.assertEqual(e["offset"], 123)

    def test_kafka_recv_log(self):
        self.logger.kafka_recv_log(None, KafkaRecvLog(
            topic="market-data", partition=0, offset=124,
            lag_ms=5, message_size=256,
            consumer_group="strategy-service",
            data={"symbol": "BTCUSDT"},
        ))
        e = self._entry(Type.KAFKA_RECV)
        self.assertEqual(e["type"], "kafka_recv")
        self.assertEqual(e["consumer_group"], "strategy-service")


class TestKafkaBackendSwitch(unittest.TestCase):
    """Logger routes to Kafka backend when configured."""

    def test_kafka_backend_receives_entries(self):
        received = []

        class FakeBackend:
            def send(self, entry):
                received.append(entry)

            def close(self):
                pass

        tmp = tempfile.mkdtemp()
        logger = Logger(tmp, list(Type), kafka_backend=FakeBackend())
        logger.info(None, Type.SYSTEM, "kafka test")
        logger.close()

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["type"], "system")
        self.assertEqual(received[0]["level"], "INFO")


class TestKafkaBackendExceptionIsolation(unittest.TestCase):
    """Kafka backend failure must not affect file logging."""

    def test_kafka_error_does_not_affect_file_log(self):
        class BrokenBackend:
            def send(self, entry):
                raise RuntimeError("kafka down")

            def close(self):
                pass

        tmp = tempfile.mkdtemp()
        logger = Logger(tmp, list(Type), kafka_backend=BrokenBackend())
        # This must not raise
        logger.info(None, Type.SYSTEM, "isolation test")
        logger.close()

        entries = _read_log(os.path.join(tmp, Type.SYSTEM.filename()))
        self.assertEqual(len(entries), 1, "file log should still have the entry")
        self.assertEqual(entries[0]["message"], "isolation test")


class TestMessageTruncation(unittest.TestCase):
    def test_long_message_truncated(self):
        from utils.log.logger import _truncate_message, MAX_MESSAGE_LEN
        long = "x" * (MAX_MESSAGE_LEN + 500)
        result = _truncate_message(long)
        self.assertTrue(result.endswith("...(truncated)"))
        self.assertLessEqual(len(result), MAX_MESSAGE_LEN)


if __name__ == "__main__":
    unittest.main()
