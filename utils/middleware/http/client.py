"""
HTTP client middleware.

Wraps requests.Session to automatically:
  - Log ExtAPILog entries for every outbound request
  - Propagate X-Session-ID header from context
"""
import time
from typing import Any, Dict, Optional, Protocol

import requests

from utils.log.context import get_session_id
from utils.log.types import ExtAPILog

try:
    from opentelemetry import trace, propagate
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False


class ExtAPILogger(Protocol):
    def ext_api_log(self, ctx: Any, ext_api: ExtAPILog) -> None: ...


class Client:
    """
    Thin wrapper around requests.Session that logs every HTTP call as an
    ExtAPILog entry.

    Usage::

        client = Client(session=requests.Session(), logger=log_instance, api_name="binance")
        resp = client.request(ctx, "GET", "https://api.binance.com/api/v3/ping")
    """

    def __init__(
        self,
        logger: Optional[ExtAPILogger],
        api_name: str,
        session: Optional[requests.Session] = None,
    ):
        self._logger = logger
        self._api_name = api_name
        self._session = session or requests.Session()

    def request(
        self,
        ctx: Any,
        method: str,
        url: str,
        **kwargs,
    ) -> requests.Response:
        """Execute an HTTP request and log it as an ExtAPILog entry."""
        # Propagate session_id
        session_id = get_session_id()
        headers = kwargs.pop("headers", {}) or {}
        if session_id:
            headers["X-Session-ID"] = session_id
        kwargs["headers"] = headers

        start = time.monotonic()
        resp: Optional[requests.Response] = None
        error_body = ""

        def _do_request():
            nonlocal resp, error_body
            # Inject traceparent when OTel is active (noop-safe).
            if _OTEL_AVAILABLE:
                try:
                    propagate.inject(headers)
                except Exception:
                    pass

            try:
                resp = self._session.request(method, url, **kwargs)
                return resp
            except Exception as exc:
                error_body = str(exc)
                raise

        try:
            if _OTEL_AVAILABLE:
                from urllib.parse import urlparse
                _path = urlparse(url).path or "/"
                _tracer = trace.get_tracer("middleware.httpclient")
                with _tracer.start_as_current_span(f"{method} {_path}"):
                    return _do_request()
            else:
                return _do_request()
        finally:
            latency_ms = int((time.monotonic() - start) * 1000)
            self._safe_log(ctx, method, url, kwargs, resp, error_body, latency_ms)

    def _safe_log(
        self,
        ctx: Any,
        method: str,
        url: str,
        kwargs: Dict,
        resp: Optional[requests.Response],
        error_body: str,
        latency_ms: int,
    ) -> None:
        if self._logger is None:
            return
        try:
            from urllib.parse import urlparse, urlencode
            parsed = urlparse(url)
            path = parsed.path or "/"
            short_url = f"{method} {path}"
            full_url = url

            request_body = ""
            if "data" in kwargs:
                d = kwargs["data"]
                request_body = d if isinstance(d, str) else str(d)
            elif "json" in kwargs:
                import json
                request_body = json.dumps(kwargs["json"])

            request_params: Dict[str, Any] = {}
            if parsed.query:
                from urllib.parse import parse_qs
                for k, vals in parse_qs(parsed.query).items():
                    request_params[k] = vals[0] if len(vals) == 1 else vals
            if "params" in kwargs and kwargs["params"]:
                request_params.update(kwargs["params"])

            request_header: Dict[str, str] = {}
            if "headers" in kwargs and kwargs["headers"]:
                request_header = {k: str(v) for k, v in kwargs["headers"].items()}

            http_status = resp.status_code if resp is not None else 0
            response_body = error_body
            if resp is not None and not error_body:
                try:
                    response_body = resp.text[:65536]
                except Exception:
                    pass

            self._logger.ext_api_log(ctx, ExtAPILog(
                api_name=self._api_name,
                url=short_url,
                full_url=full_url,
                request_header=request_header,
                request_params=request_params,
                request_body=request_body,
                response_body=response_body,
                http_status=http_status,
                latency_ms=latency_ms,
            ))
        except Exception:
            pass
