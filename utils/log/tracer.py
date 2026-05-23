"""
OpenTelemetry tracer initialisation for strategy-library.

Gracefully degrades when opentelemetry packages are not installed:
  - init_tracer() becomes a no-op that returns a noop shutdown function
  - get_trace_span_ids() always returns ("", "")

Usage::

    from utils.log.tracer import init_tracer, get_trace_span_ids

    shutdown = init_tracer("my-service", "http://localhost:4318")
    # ... run application ...
    shutdown()
"""
from typing import Callable, Tuple

_OTEL_AVAILABLE = False
try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.propagate import set_global_textmap
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
    from opentelemetry.baggage.propagation import W3CBaggagePropagator

    # The composite propagator class was named `CompositeTextMapPropagator` in
    # older OTel (<1.15) and renamed to `CompositePropagator` in newer releases
    # (1.20+). Try both so the library works across the SDK versions that ship
    # in our services today.
    try:
        from opentelemetry.propagators.composite import CompositePropagator as _CompositePropagator
    except ImportError:
        from opentelemetry.propagators.composite import (  # type: ignore[no-redef]
            CompositeTextMapPropagator as _CompositePropagator,
        )
    _OTEL_AVAILABLE = True
except ImportError:
    pass


def init_tracer(service_name: str, endpoint: str = "") -> Callable[[], None]:
    """
    Initialise OpenTelemetry tracing.

    When tracing is disabled (endpoint is empty or OTel not installed), a noop
    provider is used so get_trace_span_ids() always returns ("", "").

    Args:
        service_name: Service name attached to all spans.
        endpoint: OTLP HTTP endpoint, e.g. "http://localhost:4318".
                  Empty string disables OTLP export.

    Returns:
        A shutdown callable that flushes and closes the exporter.
    """
    if not _OTEL_AVAILABLE:
        return lambda: None

    # Always register W3C TraceContext propagator.
    set_global_textmap(_CompositePropagator([
        TraceContextTextMapPropagator(),
        W3CBaggagePropagator(),
    ]))

    if not endpoint:
        # Noop provider — spans are created but not exported.
        trace.set_tracer_provider(TracerProvider(
            resource=Resource.create({"service.name": service_name}),
        ))
        return lambda: None

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name}),
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        def shutdown():
            try:
                provider.shutdown()
            except Exception:
                pass

        return shutdown
    except Exception:
        return lambda: None


def get_trace_span_ids() -> Tuple[str, str]:
    """
    Return (trace_id, span_id) from the current OTel span context.

    Returns ("", "") when no active span exists or OTel is not installed.
    """
    if not _OTEL_AVAILABLE:
        return "", ""

    try:
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx is None or not ctx.is_valid:
            return "", ""
        trace_id = format(ctx.trace_id, "032x")
        span_id = format(ctx.span_id, "016x")
        return trace_id, span_id
    except Exception:
        return "", ""
