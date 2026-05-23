"""
HTTP server ASGI middleware.

Wraps an ASGI application to automatically:
  - Log AccessLog entries for every inbound HTTP request
  - Extract and propagate X-Session-ID from inbound request into context var
  - Catch handler exceptions: log FATAL and return 500
"""
import json
import time
from typing import Any, Callable, Dict, Optional, Protocol

from utils.log.context import set_session_id
from utils.log.types import AccessLog

try:
    from opentelemetry import trace, propagate
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False


class AccessLogger(Protocol):
    def access_log(self, ctx: Any, access: AccessLog) -> None: ...
    def fatal(self, ctx: Any, log_type: Any, msg: str) -> None: ...


class ASGIMiddleware:
    """
    ASGI middleware that records access logs.

    Usage (FastAPI / Starlette)::

        from fastapi import FastAPI
        app = FastAPI()
        app.add_middleware(ASGIMiddleware, logger=log_instance)
    """

    def __init__(self, app, logger: Optional[AccessLogger] = None):
        self.app = app
        self._logger = logger

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Extract session_id and set it in the context var.
        raw_headers = scope.get("headers", [])
        headers_dict = {k: v for k, v in raw_headers}
        session_id = headers_dict.get(b"x-session-id", b"").decode()
        set_session_id(session_id)

        method = scope.get("method", "")
        path = scope.get("path", "/")

        # Extract parent trace context from W3C traceparent header.
        _span_ctx = None
        if _OTEL_AVAILABLE:
            try:
                carrier = {k.decode(): v.decode() for k, v in raw_headers}
                _ctx = propagate.extract(carrier)
                _tracer = trace.get_tracer("middleware.httpserver")
                _span = _tracer.start_span(f"{method} {path}", context=_ctx)
                _span_ctx = trace.use_span(_span, end_on_exit=False)
                _span_ctx.__enter__()
            except Exception:
                _span_ctx = None
        query = scope.get("query_string", b"").decode()

        # Read request body
        request_body = ""
        body_chunks = []
        more_body = True
        while more_body:
            message = await receive()
            body_chunk = message.get("body", b"")
            body_chunks.append(body_chunk)
            more_body = message.get("more_body", False)
        raw_body = b"".join(body_chunks)
        request_body = raw_body.decode(errors="replace")[:65536]

        # Reconstruct a receive callable that replays the buffered body.
        body_iter = iter([
            {"type": "http.request", "body": raw_body, "more_body": False}
        ])

        async def replay_receive():
            return next(body_iter, {"type": "http.disconnect"})

        # Capture response status and body.
        response_status = 0
        response_chunks = []

        async def capture_send(message):
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message.get("status", 0)
            elif message["type"] == "http.response.body":
                response_chunks.append(message.get("body", b""))
            await send(message)

        start = time.monotonic()
        exc_msg = ""
        try:
            await self.app(scope, replay_receive, capture_send)
        except Exception as exc:
            exc_msg = str(exc)
            if self._logger is not None:
                try:
                    from utils.log.types import Type
                    self._logger.fatal(None, Type.SYSTEM, f"http handler panic: {exc_msg}")
                except Exception:
                    pass
            # Return 500
            error_payload = json.dumps({"error": "Internal Server Error"}).encode()
            await send({"type": "http.response.start", "status": 500,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": error_payload, "more_body": False})
        finally:
            latency_ms = int((time.monotonic() - start) * 1000)
            response_body = b"".join(response_chunks).decode(errors="replace")[:65536]

            # Parse request headers
            request_header: Dict[str, str] = {}
            for k, v in scope.get("headers", []):
                request_header[k.decode()] = v.decode(errors="replace")

            # Parse query params
            request_params: Dict[str, Any] = {}
            if query:
                from urllib.parse import parse_qs
                for k, vals in parse_qs(query).items():
                    request_params[k] = vals[0] if len(vals) == 1 else vals

            if self._logger is not None:
                try:
                    self._logger.access_log(None, AccessLog(
                        method=method,
                        path=path,
                        request_header=request_header,
                        request_params=request_params,
                        request_body=request_body,
                        response_body=response_body,
                        http_status=response_status or (500 if exc_msg else 0),
                        latency_ms=latency_ms,
                    ))
                except Exception:
                    pass

            # End the server span.
            if _span_ctx is not None:
                try:
                    _span_ctx.__exit__(None, None, None)
                except Exception:
                    pass
