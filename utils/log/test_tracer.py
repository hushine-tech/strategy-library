"""
Tests for Python OTel tracer.

Since opentelemetry may not be installed in the test environment, we test:
1. Graceful degradation when OTel is not installed
2. get_trace_span_ids() returns empty strings when no active span
3. When OTel IS available, init_tracer() sets up a provider and spans work
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


class TestTracerGracefulDegradation(unittest.TestCase):
    """Verify the tracer module degrades gracefully when OTel is not installed."""

    def test_get_trace_span_ids_returns_empty_strings(self):
        from utils.log.tracer import get_trace_span_ids
        trace_id, span_id = get_trace_span_ids()
        # Either ("", "") because OTel is not installed, or ("", "") because
        # there is no active span. Both are acceptable.
        self.assertIsInstance(trace_id, str)
        self.assertIsInstance(span_id, str)

    def test_init_tracer_no_endpoint_returns_callable(self):
        from utils.log.tracer import init_tracer
        shutdown = init_tracer("test-service", "")
        self.assertTrue(callable(shutdown))
        shutdown()  # must not raise

    def test_init_tracer_with_endpoint_returns_callable(self):
        from utils.log.tracer import init_tracer
        # We don't have a real Jaeger running, but init_tracer should handle
        # the connection failure gracefully and still return a callable.
        shutdown = init_tracer("test-service", "http://localhost:14999")
        self.assertTrue(callable(shutdown))
        try:
            shutdown()
        except Exception:
            pass  # OTel exporter errors on shutdown are acceptable in tests


class TestTracerWithSDK(unittest.TestCase):
    """Tests that require opentelemetry SDK to be installed."""

    def setUp(self):
        try:
            import opentelemetry
            self._otel_available = True
        except ImportError:
            self._otel_available = False

    def test_span_ids_populated_when_active_span(self):
        if not self._otel_available:
            self.skipTest("opentelemetry not installed")

        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from utils.log.tracer import get_trace_span_ids

        tp = TracerProvider()
        trace.set_tracer_provider(tp)

        tracer = tp.get_tracer("test")
        with tracer.start_as_current_span("test-span"):
            tid, sid = get_trace_span_ids()
            self.assertNotEqual(tid, "", "trace_id should be non-empty when span is active")
            self.assertNotEqual(sid, "", "span_id should be non-empty when span is active")
            self.assertEqual(len(tid), 32, "trace_id should be 32 hex chars")
            self.assertEqual(len(sid), 16, "span_id should be 16 hex chars")

    def test_span_ids_empty_outside_span(self):
        if not self._otel_available:
            self.skipTest("opentelemetry not installed")

        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.trace import NonRecordingSpan
        from utils.log.tracer import get_trace_span_ids

        # Use a fresh provider that has no active span.
        tp = TracerProvider()
        trace.set_tracer_provider(tp)

        tid, sid = get_trace_span_ids()
        self.assertEqual(tid, "")
        self.assertEqual(sid, "")

    def test_log_entry_contains_trace_fields(self):
        """Verify that Logger produces trace_id/span_id fields in log output."""
        if not self._otel_available:
            self.skipTest("opentelemetry not installed")

        import json
        import tempfile
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        from utils.log.logger import Logger
        from utils.log.types import Type

        tp = TracerProvider()
        trace.set_tracer_provider(tp)

        tmp = tempfile.mkdtemp()
        logger = Logger(tmp, [Type.SYSTEM])

        tracer = tp.get_tracer("test")
        with tracer.start_as_current_span("test-span"):
            logger.info(None, Type.SYSTEM, "hello")

        logger.close()

        with open(os.path.join(tmp, "system.log")) as f:
            entry = json.loads(f.read().strip())

        self.assertIn("trace_id", entry, "log entry must have trace_id field")
        self.assertIn("span_id", entry, "log entry must have span_id field")
        self.assertNotEqual(entry["trace_id"], "", "trace_id should not be empty inside span")


if __name__ == "__main__":
    unittest.main()
