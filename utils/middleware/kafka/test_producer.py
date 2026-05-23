"""Tests for Kafka producer middleware."""
import sys
import os
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))

from utils.middleware.kafka.producer import ProducerMiddleware
from utils.log.types import KafkaSentLog


class FakeMetadata:
    def __init__(self, partition=0, offset=42):
        self.partition = partition
        self.offset = offset


class FakeFuture:
    def __init__(self, metadata=None, exc=None):
        self._metadata = metadata or FakeMetadata()
        self._exc = exc

    def get(self, timeout=None):
        if self._exc:
            raise self._exc
        return self._metadata


class FakeProducer:
    def __init__(self, future=None):
        self._future = future or FakeFuture()
        self.last_topic = None
        self.last_value = None
        self.last_headers = None

    def send(self, topic, key=None, value=None, headers=None):
        self.last_topic = topic
        self.last_value = value
        self.last_headers = headers
        return self._future


class CaptureLogger:
    def __init__(self):
        self.entries = []

    def kafka_sent_log(self, ctx, entry: KafkaSentLog):
        self.entries.append(entry)


class TestKafkaProducerMiddleware(unittest.TestCase):
    def test_logs_sent_message(self):
        raw = FakeProducer(FakeFuture(FakeMetadata(partition=1, offset=100)))
        logger = CaptureLogger()
        mw = ProducerMiddleware(raw, logger=logger)

        mw.send(None, "market-data", None, b'{"symbol":"BTCUSDT"}')

        self.assertEqual(len(logger.entries), 1)
        e = logger.entries[0]
        self.assertEqual(e.topic, "market-data")
        self.assertEqual(e.partition, 1)
        self.assertEqual(e.offset, 100)
        self.assertEqual(e.message_size, len(b'{"symbol":"BTCUSDT"}'))
        self.assertEqual(e.data["symbol"], "BTCUSDT")

    def test_logs_on_producer_error(self):
        raw = FakeProducer(FakeFuture(exc=Exception("broker down")))
        logger = CaptureLogger()
        mw = ProducerMiddleware(raw, logger=logger)

        # send() itself doesn't raise — the future does on get()
        mw.send(None, "orders", None, b'{"x":1}')

        # Log is still recorded with best-effort partition/offset = 0/-1
        self.assertEqual(len(logger.entries), 1)

    def test_no_logger_no_crash(self):
        raw = FakeProducer()
        mw = ProducerMiddleware(raw, logger=None)
        mw.send(None, "test", None, b"data")


class TestKafkaProducerOTel(unittest.TestCase):
    def test_otel_not_installed_no_crash(self):
        """当 opentelemetry 未安装时，send() 正常执行，无 span 创建。"""
        import utils.middleware.kafka.producer as producer_mod
        original = producer_mod._OTEL_AVAILABLE
        try:
            producer_mod._OTEL_AVAILABLE = False
            raw = FakeProducer()
            mw = ProducerMiddleware(raw)
            mw.send(None, "market-data", None, b"data")
            self.assertEqual(raw.last_topic, "market-data")
            self.assertIsNone(raw.last_headers)
        finally:
            producer_mod._OTEL_AVAILABLE = original

    def test_traceparent_injected_when_active_span(self):
        """存在活跃 OTel span 时，消息 headers 应包含 traceparent。"""
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.trace import TracerProvider
        except ImportError:
            self.skipTest("opentelemetry-sdk not installed")

        import utils.middleware.kafka.producer as producer_mod
        if not producer_mod._OTEL_AVAILABLE:
            self.skipTest("opentelemetry not available")

        provider = TracerProvider()
        tracer = provider.get_tracer("test")

        raw = FakeProducer()
        mw = ProducerMiddleware(raw)

        with patch.object(producer_mod.trace, "get_tracer", return_value=tracer):
            with tracer.start_as_current_span("outer"):
                mw.send(None, "market-data", None, b'{"symbol":"BTCUSDT"}')

        headers = raw.last_headers or []
        header_keys = [k for k, _ in headers]
        self.assertIn("traceparent", header_keys)

    def test_send_without_parent_span_injects_producer_traceparent(self):
        """无父 span 时，producer 自建 span 并注入 traceparent。"""
        try:
            from opentelemetry.sdk.trace import TracerProvider
        except ImportError:
            self.skipTest("opentelemetry-sdk not installed")

        import utils.middleware.kafka.producer as producer_mod
        if not producer_mod._OTEL_AVAILABLE:
            self.skipTest("opentelemetry not available")

        provider = TracerProvider()
        tracer = provider.get_tracer("test")

        raw = FakeProducer()
        mw = ProducerMiddleware(raw)
        with patch.object(producer_mod.trace, "get_tracer", return_value=tracer):
            mw.send(None, "market-data", None, b"data")

        headers = raw.last_headers or []
        header_keys = [k for k, _ in headers]
        self.assertIn("traceparent", header_keys)

    def test_span_name_contains_topic(self):
        """创建的 span 名称应为 'Kafka Produce {topic}'。"""
        try:
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        except ImportError:
            self.skipTest("opentelemetry-sdk not installed")

        import utils.middleware.kafka.producer as producer_mod
        if not producer_mod._OTEL_AVAILABLE:
            self.skipTest("opentelemetry not available")

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")

        raw = FakeProducer()
        mw = ProducerMiddleware(raw)
        with patch.object(producer_mod.trace, "get_tracer", return_value=tracer):
            mw.send(None, "market-data", None, b"data")

        spans = exporter.get_finished_spans()
        span_names = [s.name for s in spans]
        self.assertIn("Kafka Produce market-data", span_names)


if __name__ == "__main__":
    unittest.main()
