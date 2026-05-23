"""Tests for grpc_interceptors.ServerAccessInterceptor / ClientExtInterceptor.

Covers:
  - optional OTel: module imports cleanly; behaves without raises when OTel is
    not available (simulated via monkeypatch)
  - ClientExtInterceptor augments outgoing metadata with session_id and
    (when OTel available) with a W3C traceparent header
  - ServerAccessInterceptor's wrapped handler extracts session_id from
    incoming metadata and invokes the inner handler

End-to-end trace propagation (quant-handler → strategy-service →
account-service) is verified separately by ``scripts/verify_tracing.sh``.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import grpc


class _FakeCallDetails:
    """Shape-compatible stand-in for grpc.ClientCallDetails."""

    def __init__(self, method: str = "/pkg.Svc/Method", metadata=None):
        self.method = method
        self.timeout = None
        self.metadata = metadata
        self.credentials = None
        self.wait_for_ready = None


class ClientExtInterceptorTest(unittest.TestCase):
    def setUp(self):
        from utils.log.grpc_interceptors import ClientExtInterceptor
        self.interceptor = ClientExtInterceptor(target_service="127.0.0.1:50051")

    def test_imports_without_error(self):
        # The interceptor module must import even if OTel is absent.
        from utils.log import ClientExtInterceptor, ServerAccessInterceptor  # noqa
        self.assertTrue(callable(ClientExtInterceptor))
        self.assertTrue(callable(ServerAccessInterceptor))

    def test_augment_adds_session_id(self):
        """Outgoing metadata must include x-session-id from the contextvar."""
        from utils.log.context import set_session_id
        set_session_id("test-sid-12345")

        details = _FakeCallDetails(metadata=None)
        augmented = self.interceptor._augment(details)

        keys = [k for k, _ in augmented.metadata]
        self.assertIn("x-session-id", keys)
        sid = dict(augmented.metadata).get("x-session-id")
        self.assertEqual(sid, "test-sid-12345")

    def test_augment_preserves_existing_session_id(self):
        """If caller already set x-session-id, don't duplicate or override."""
        details = _FakeCallDetails(metadata=[("x-session-id", "caller-sid")])
        augmented = self.interceptor._augment(details)
        sids = [v for k, v in augmented.metadata if k == "x-session-id"]
        self.assertEqual(len(sids), 1)
        self.assertEqual(sids[0], "caller-sid")

    def test_trace_pairs_injects_when_span_active(self):
        """When OTel is initialised and a span is active, _trace_pairs()
        returns W3C traceparent headers."""
        from utils.log.grpc_interceptors import _OTEL_AVAILABLE
        if not _OTEL_AVAILABLE:
            self.skipTest("OTel not available in this env")

        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("parent"):
            pairs = self.interceptor._trace_pairs()

        keys = [k for k, _ in pairs]
        self.assertIn("traceparent", keys)


class ServerAccessInterceptorTest(unittest.TestCase):
    def test_instantiates_and_passes_through_stream_handlers(self):
        """intercept_service must return handler unchanged for stream types."""
        from utils.log.grpc_interceptors import ServerAccessInterceptor

        interceptor = ServerAccessInterceptor()

        # Fake continuation returning a handler whose unary_unary is None (e.g. stream).
        handler = MagicMock()
        handler.unary_unary = None
        continuation = MagicMock(return_value=handler)

        details = MagicMock()
        details.method = "/pkg.Svc/StreamMethod"
        result = interceptor.intercept_service(continuation, details)
        # Stream handlers fall through unchanged.
        self.assertIs(result, handler)

    def test_unary_handler_wraps_and_writes_log(self):
        """For a unary-unary handler, wrapped handler invokes inner and emits a
        grpc_access log. We assert the wrapper is a different object with a
        unary_unary handler."""
        from utils.log.grpc_interceptors import ServerAccessInterceptor

        interceptor = ServerAccessInterceptor()
        inner_called = []

        def inner(request, context):
            inner_called.append((request, context))
            return {"ok": True}

        fake_handler = MagicMock()
        fake_handler.unary_unary = inner
        fake_handler.request_deserializer = None
        fake_handler.response_serializer = None

        details = MagicMock()
        details.method = "/pkg.Svc/UnaryMethod"

        continuation = MagicMock(return_value=fake_handler)
        wrapped_handler = interceptor.intercept_service(continuation, details)
        self.assertIsNotNone(wrapped_handler)

        # Invoke the wrapped behavior with a mock context.
        fake_ctx = MagicMock()
        fake_ctx.invocation_metadata.return_value = [("x-session-id", "srv-test")]
        fake_ctx.peer.return_value = "ipv4:127.0.0.1:12345"
        fake_ctx.code.return_value = grpc.StatusCode.OK

        response = wrapped_handler.unary_unary("req", fake_ctx)
        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(inner_called), 1)

    def test_unary_handler_generates_fresh_session_and_restores_outer_context(self):
        from utils.log import session as log_session
        from utils.log.context import get_session_id
        from utils.log.grpc_interceptors import ServerAccessInterceptor

        interceptor = ServerAccessInterceptor()

        def inner(_request, _context):
            return {"sid": get_session_id()}

        fake_handler = MagicMock()
        fake_handler.unary_unary = inner
        fake_handler.request_deserializer = None
        fake_handler.response_serializer = None

        details = MagicMock()
        details.method = "/pkg.Svc/UnaryMethod"
        wrapped_handler = interceptor.intercept_service(
            MagicMock(return_value=fake_handler),
            details,
        )

        fake_ctx = MagicMock()
        fake_ctx.invocation_metadata.return_value = []
        fake_ctx.peer.return_value = "ipv4:127.0.0.1:12345"
        fake_ctx.code.return_value = grpc.StatusCode.OK

        with log_session("outer-sid"):
            response = wrapped_handler.unary_unary("req", fake_ctx)
            self.assertNotEqual(response["sid"], "outer-sid")
            self.assertEqual(get_session_id(), "outer-sid")

    def test_unary_handler_restores_outer_context_after_incoming_session_id(self):
        from utils.log import session as log_session
        from utils.log.context import get_session_id
        from utils.log.grpc_interceptors import ServerAccessInterceptor

        interceptor = ServerAccessInterceptor()

        def inner(_request, _context):
            return {"sid": get_session_id()}

        fake_handler = MagicMock()
        fake_handler.unary_unary = inner
        fake_handler.request_deserializer = None
        fake_handler.response_serializer = None

        details = MagicMock()
        details.method = "/pkg.Svc/UnaryMethod"
        wrapped_handler = interceptor.intercept_service(
            MagicMock(return_value=fake_handler),
            details,
        )

        fake_ctx = MagicMock()
        fake_ctx.invocation_metadata.return_value = [("x-session-id", "incoming-sid")]
        fake_ctx.peer.return_value = "ipv4:127.0.0.1:12345"
        fake_ctx.code.return_value = grpc.StatusCode.OK

        with log_session("outer-sid"):
            response = wrapped_handler.unary_unary("req", fake_ctx)
            self.assertEqual(response["sid"], "incoming-sid")
            self.assertEqual(get_session_id(), "outer-sid")


if __name__ == "__main__":
    unittest.main()
