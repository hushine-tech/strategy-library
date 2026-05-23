"""gRPC server + client interceptors for the Elemental log + tracing pipeline.

These mirror ``golang-lib/middleware/grpc`` (server, ``grpc_access``) and
``golang-lib/middleware/grpcclient`` (client, ``grpc_ext``) so Python gRPC
services produce the same structured log events Go services do, AND carry
the same W3C traceparent propagation so trace IDs line up across services.

Usage — server side::

    from utils.log import ServerAccessInterceptor
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=10),
        interceptors=[ServerAccessInterceptor()],
    )

Usage — client side::

    from utils.log import ClientExtInterceptor
    channel = grpc.insecure_channel(addr)
    channel = grpc.intercept_channel(channel, ClientExtInterceptor(target_service=addr))
    stub = pb_grpc.SomeServiceStub(channel)

OTel integration is **optional**: when ``opentelemetry`` is not installed or a
TracerProvider has not been initialised, both interceptors continue to write
access / ext log entries but skip span creation and traceparent injection
gracefully — no ImportError, no raise. This keeps strategy-service bootable
in minimal containers / test environments.

Only **unary-unary** RPCs are instrumented for span + log; stream RPCs pass
through with metadata (x-session-id + traceparent) still injected.
"""

from __future__ import annotations

import time
from collections import namedtuple
from typing import Any, Callable

import grpc
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message

from .context import get_session_id
from .logger import grpc_access_log, grpc_ext_log, session as _log_session
from .types import GRPCAccessLog, GRPCExtLog


# ── Optional OTel import — graceful degrade when absent ─────────────────────

try:
    from opentelemetry import propagate as _otel_propagate
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import SpanKind as _OtelSpanKind, Status as _OtelStatus, StatusCode as _OtelStatusCode

    _OTEL_AVAILABLE = True
except Exception:  # noqa: BLE001  # ImportError or init failure
    _OTEL_AVAILABLE = False
    _otel_propagate = None  # type: ignore[assignment]
    _otel_trace = None  # type: ignore[assignment]
    _OtelSpanKind = None  # type: ignore[assignment]
    _OtelStatus = None  # type: ignore[assignment]
    _OtelStatusCode = None  # type: ignore[assignment]


def _get_tracer():
    """Return the current OTel tracer. Lookup is per-call so the tracer reflects
    whichever provider is active NOW — critical because `init_tracer` is called
    by the service AFTER this module imports, so caching the result at import
    time captures the pre-init noop provider.
    """
    if not _OTEL_AVAILABLE:
        return None
    try:
        return _otel_trace.get_tracer("strategy-library/grpc")
    except Exception:  # noqa: BLE001
        return None


_SESSION_ID_METADATA_KEY = "x-session-id"
# W3C traceparent is lowercase; inject writes lowercase; some implementations
# also use `tracestate`. We don't filter — propagate.inject writes whatever the
# configured propagators need, typically {traceparent, tracestate, baggage}.


# ── Helpers ─────────────────────────────────────────────────────────────────


def _to_dict(msg: Any) -> dict:
    """Best-effort proto → dict. Falls back to string repr; never raises."""
    if msg is None:
        return {}
    if isinstance(msg, Message):
        try:
            return MessageToDict(msg, preserving_proto_field_name=True)
        except Exception:  # noqa: BLE001
            return {"raw": str(msg)}
    if isinstance(msg, dict):
        return msg
    return {"raw": str(msg)}


def _extract_client_ip(context: grpc.ServicerContext) -> str:
    """Normalize ``peer()`` output (e.g. "ipv4:127.0.0.1:52342") to a plain host."""
    peer = ""
    try:
        peer = context.peer() or ""
    except Exception:  # noqa: BLE001
        return ""
    if not peer:
        return ""
    if peer.startswith("ipv4:"):
        body = peer[len("ipv4:"):]
    elif peer.startswith("ipv6:"):
        body = peer[len("ipv6:"):]
    else:
        body = peer
    if ":" in body:
        host, _, _ = body.rpartition(":")
        return host.strip("[]")
    return body


def _split_method(fullmethod: str) -> tuple[str, str]:
    """Split ``/pkg.Service/Method`` → (service, method). Safe on malformed."""
    s = fullmethod.lstrip("/")
    if "/" in s:
        svc, meth = s.split("/", 1)
        return svc, meth
    return s, ""


# ── Server-side: grpc_access + server span ──────────────────────────────────


class ServerAccessInterceptor(grpc.ServerInterceptor):
    """Writes one ``grpc_access`` entry per unary-unary RPC, and (when OTel is
    available) starts a SERVER span rooted at the caller's W3C trace context.

    Side effects per call:
      - ``x-session-id`` in incoming metadata → contextvar, so downstream logs
        share the session id
      - W3C traceparent / tracestate / baggage → OTel context, so downstream
        outbound client calls inherit the trace
    """

    def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], grpc.RpcMethodHandler],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler | None:
        handler = continuation(handler_call_details)
        if handler is None or not handler.unary_unary:
            return handler

        method = handler_call_details.method
        inner = handler.unary_unary

        def wrapped(request, context):
            # Metadata: dict view for extraction + session id pickup.
            try:
                md_pairs = list(context.invocation_metadata() or [])
            except Exception:  # noqa: BLE001
                md_pairs = []
            md = {k.lower(): v for k, v in md_pairs}

            incoming_sid = md.get(_SESSION_ID_METADATA_KEY, "") or md.get(
                _SESSION_ID_METADATA_KEY.replace("-", "_"), ""
            )
            with _log_session(incoming_sid or None):
                client_ip = _extract_client_ip(context)

                # If OTel is available, extract W3C parent context + start a SERVER
                # span so this RPC joins the caller's trace. Guard on a fresh
                # local `otel_on` so any mid-call issue cleanly falls back.
                otel_on = _OTEL_AVAILABLE and (_tracer_ref := _get_tracer()) is not None
                span = None
                parent_ctx = None
                if otel_on:
                    try:
                        parent_ctx = _otel_propagate.extract(md)  # type: ignore[union-attr]
                        svc, meth = _split_method(method)
                        span = _tracer_ref.start_span(
                            method,
                            context=parent_ctx,
                            kind=_OtelSpanKind.SERVER,
                            attributes={
                                "rpc.system": "grpc",
                                "rpc.service": svc,
                                "rpc.method": meth,
                                "net.peer.ip": client_ip,
                            },
                        )
                    except Exception:  # noqa: BLE001
                        # OTel init path failed — continue without span, keep log path alive.
                        otel_on = False
                        span = None

                start = time.monotonic()
                status_code = 0
                err_str = ""
                response: Any = None

                # When a span exists, mark it current so the inner handler (and
                # any outbound client interceptor inside) sees the right parent.
                if otel_on and span is not None:
                    activation = _otel_trace.use_span(span, end_on_exit=False)  # type: ignore[union-attr]
                else:
                    activation = _NullCtx()

                try:
                    with activation:
                        try:
                            response = inner(request, context)
                            try:
                                code = context.code()
                            except Exception:  # noqa: BLE001
                                code = None
                            if code is not None and code != grpc.StatusCode.OK:
                                status_code = code.value[0]
                                err_str = code.name
                            return response
                        except grpc.RpcError as e:
                            code = e.code() if hasattr(e, "code") else None
                            status_code = (
                                code.value[0] if code is not None
                                else grpc.StatusCode.UNKNOWN.value[0]
                            )
                            err_str = str(e)
                            raise
                        except Exception as e:  # noqa: BLE001
                            status_code = grpc.StatusCode.INTERNAL.value[0]
                            err_str = f"{type(e).__name__}: {e}"
                            raise
                        finally:
                            latency_ms = int((time.monotonic() - start) * 1000)
                            # Update span status while it's still current so
                            # get_trace_span_ids() inside grpc_access_log sees it.
                            if otel_on and span is not None:
                                try:
                                    span.set_attribute("rpc.grpc.status_code", int(status_code))
                                    if status_code != 0:
                                        span.set_status(_OtelStatus(_OtelStatusCode.ERROR, err_str))  # type: ignore[misc]
                                except Exception:  # noqa: BLE001
                                    pass
                            try:
                                grpc_access_log(
                                    None,
                                    GRPCAccessLog(
                                        method=method,
                                        client_ip=client_ip,
                                        latency_ms=latency_ms,
                                        status_code=status_code,
                                        error=err_str,
                                        request_params=_to_dict(request),
                                        response=_to_dict(response),
                                    ),
                                )
                            except Exception:  # noqa: BLE001
                                pass
                finally:
                    if otel_on and span is not None:
                        try:
                            span.end()
                        except Exception:  # noqa: BLE001
                            pass

        return grpc.unary_unary_rpc_method_handler(
            wrapped,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )


class _NullCtx:
    """Drop-in replacement for a context manager when OTel is absent."""

    def __enter__(self):
        return None

    def __exit__(self, *_):
        return False


# ── Client-side: grpc_ext + client span + traceparent inject ────────────────


_ClientCallDetails = namedtuple(
    "_ClientCallDetails",
    ("method", "timeout", "metadata", "credentials", "wait_for_ready"),
)


class ClientExtInterceptor(
    grpc.UnaryUnaryClientInterceptor,
    grpc.UnaryStreamClientInterceptor,
    grpc.StreamUnaryClientInterceptor,
    grpc.StreamStreamClientInterceptor,
):
    """Writes one ``grpc_ext`` entry per outbound unary-unary RPC; injects
    ``x-session-id`` and W3C traceparent into outgoing metadata.

    Streaming RPCs pass through with metadata augmented but do not emit
    per-message log entries.
    """

    def __init__(self, target_service: str = "") -> None:
        self._target = target_service

    # ── metadata augmentation ───────────────────────────────────────────

    def _augment(self, client_call_details, extra_metadata=None) -> _ClientCallDetails:
        """Return a new ClientCallDetails with x-session-id + any extra pairs
        (e.g. W3C traceparent) added to metadata."""
        md = list(client_call_details.metadata) if client_call_details.metadata else []
        sid = get_session_id()
        if sid and not any(k.lower() == _SESSION_ID_METADATA_KEY for k, _ in md):
            md.append((_SESSION_ID_METADATA_KEY, sid))
        if extra_metadata:
            for k, v in extra_metadata:
                md.append((k, v))
        return _ClientCallDetails(
            client_call_details.method,
            getattr(client_call_details, "timeout", None),
            md,
            getattr(client_call_details, "credentials", None),
            getattr(client_call_details, "wait_for_ready", None),
        )

    def _trace_pairs(self) -> list[tuple[str, str]]:
        """Build list of (key, value) metadata pairs for the current span's
        W3C context, ready for `_augment(extra_metadata=...)`. Returns empty
        list when OTel is absent or no span is active.
        """
        if not _OTEL_AVAILABLE:
            return []
        try:
            carrier: dict[str, str] = {}
            _otel_propagate.inject(carrier)  # type: ignore[union-attr]
            return [(k, v) for k, v in carrier.items()]
        except Exception:  # noqa: BLE001
            return []

    # ── unary-unary ─────────────────────────────────────────────────────

    def intercept_unary_unary(self, continuation, client_call_details, request):
        otel_on = _OTEL_AVAILABLE and (_tracer_ref := _get_tracer()) is not None
        span = None
        method = client_call_details.method

        # Start CLIENT span BEFORE inject so propagate.inject sees it as
        # current. `start_as_current_span` is safe here because the call site
        # is synchronous in the caller's thread; we move off-thread only for
        # the done_callback below, where we hold the span reference directly.
        if otel_on:
            try:
                svc, meth = _split_method(method)
                span = _tracer_ref.start_span(
                    method,
                    kind=_OtelSpanKind.CLIENT,
                    attributes={
                        "rpc.system": "grpc",
                        "rpc.service": svc,
                        "rpc.method": meth,
                        "net.peer.name": self._target,
                    },
                )
            except Exception:  # noqa: BLE001
                otel_on = False
                span = None

        # Use use_span to make span current in THIS thread so propagate.inject
        # picks it up for W3C traceparent injection.
        if otel_on and span is not None:
            with _otel_trace.use_span(span, end_on_exit=False):  # type: ignore[union-attr]
                extras = self._trace_pairs()
        else:
            extras = []

        details = self._augment(client_call_details, extra_metadata=extras)

        start = time.monotonic()
        call_future = continuation(details, request)

        def _log_done(cf):
            latency_ms = int((time.monotonic() - start) * 1000)
            status_code = 0
            err_str = ""
            response: Any = None
            try:
                response = cf.result()
            except grpc.RpcError as e:
                code = e.code() if hasattr(e, "code") else None
                status_code = (
                    code.value[0] if code is not None
                    else grpc.StatusCode.UNKNOWN.value[0]
                )
                err_str = str(e)
            except Exception as e:  # noqa: BLE001
                status_code = grpc.StatusCode.UNKNOWN.value[0]
                err_str = f"{type(e).__name__}: {e}"

            # Update + close span. use_span activates it in THIS callback
            # thread just long enough for grpc_ext_log's get_trace_span_ids
            # to pick up the right trace_id, then we explicitly end the span.
            if span is not None:
                try:
                    span.set_attribute("rpc.grpc.status_code", int(status_code))
                    if status_code != 0:
                        span.set_status(_OtelStatus(_OtelStatusCode.ERROR, err_str))  # type: ignore[misc]
                except Exception:  # noqa: BLE001
                    pass

                if _OTEL_AVAILABLE:
                    try:
                        with _otel_trace.use_span(span, end_on_exit=False):  # type: ignore[union-attr]
                            _emit_ext_log(method, self._target, latency_ms,
                                          status_code, err_str, request, response)
                    except Exception:  # noqa: BLE001
                        _emit_ext_log(method, self._target, latency_ms,
                                      status_code, err_str, request, response)
                    finally:
                        try:
                            span.end()
                        except Exception:  # noqa: BLE001
                            pass
                else:
                    _emit_ext_log(method, self._target, latency_ms,
                                  status_code, err_str, request, response)
            else:
                _emit_ext_log(method, self._target, latency_ms,
                              status_code, err_str, request, response)

        try:
            call_future.add_done_callback(_log_done)
        except Exception:  # noqa: BLE001
            _log_done(call_future)
        return call_future

    # ── streaming variants: metadata only ───────────────────────────────

    def intercept_unary_stream(self, continuation, client_call_details, request):
        extras = self._trace_pairs()
        return continuation(self._augment(client_call_details, extra_metadata=extras), request)

    def intercept_stream_unary(self, continuation, client_call_details, request_iterator):
        extras = self._trace_pairs()
        return continuation(self._augment(client_call_details, extra_metadata=extras), request_iterator)

    def intercept_stream_stream(self, continuation, client_call_details, request_iterator):
        extras = self._trace_pairs()
        return continuation(self._augment(client_call_details, extra_metadata=extras), request_iterator)


def _emit_ext_log(method, target, latency_ms, status_code, err_str, request, response):
    """Write one grpc_ext log entry. Swallows logging errors."""
    try:
        grpc_ext_log(
            None,
            GRPCExtLog(
                method=method,
                target_service=target,
                latency_ms=latency_ms,
                status_code=status_code,
                error=err_str,
                request_params=_to_dict(request),
                response=_to_dict(response),
            ),
        )
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "ServerAccessInterceptor",
    "ClientExtInterceptor",
]
