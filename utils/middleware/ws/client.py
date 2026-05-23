"""
WebSocket client middleware.

Wraps websocket-client (websocket.WebSocket) to automatically log
connect/send/recv/close events as WebSocketLog entries.
"""
import time
from typing import Any, Optional, Protocol
from urllib.parse import urlparse

from utils.log.types import WebSocketLog


class WSLogger(Protocol):
    def websocket_log(self, ctx: Any, ws: WebSocketLog) -> None: ...


def _method_and_path(url: str) -> str:
    try:
        parsed = urlparse(url)
        path = parsed.path or "/"
        return f"GET {path}"
    except Exception:
        return "GET /"


class Client:
    """
    Thin wrapper around a websocket connection that logs every event.

    Usage::

        import websocket
        raw = websocket.WebSocket()
        client = Client(raw, logger=log_instance)
        client.connect(ctx, "wss://stream.binance.com/ws/btcusdt@kline_1m")
        _, data = client.recv(ctx, "kline")
        client.close(ctx)
    """

    def __init__(self, ws, logger: Optional[WSLogger] = None):
        self._ws = ws
        self._logger = logger
        self._full_url = ""
        self._url = ""

    def connect(self, ctx: Any, url: str, **kwargs) -> None:
        self._full_url = url
        self._url = _method_and_path(url)
        start = time.monotonic()
        try:
            self._ws.connect(url, **kwargs)
            self._log(ctx, "connect", "outbound", "", time.monotonic() - start)
        except Exception as exc:
            self._log(ctx, "connect_error", "outbound", str(exc), time.monotonic() - start)
            raise

    def send(self, ctx: Any, event_type: str, payload: str) -> None:
        start = time.monotonic()
        try:
            self._ws.send(payload)
            self._log(ctx, event_type or "send", "send", payload, time.monotonic() - start)
        except Exception as exc:
            self._log(ctx, f"{event_type or 'send'}_error", "send", str(exc), time.monotonic() - start)
            raise

    def recv(self, ctx: Any, event_type: str = "") -> tuple:
        start = time.monotonic()
        try:
            data = self._ws.recv()
            self._log(ctx, event_type or "recv", "recv", data or "", time.monotonic() - start)
            return data
        except Exception as exc:
            self._log(ctx, f"{event_type or 'recv'}_error", "recv", str(exc), time.monotonic() - start)
            raise

    def close(self, ctx: Any) -> None:
        try:
            self._ws.close()
            self._log(ctx, "close", "outbound", "", 0)
        except Exception as exc:
            self._log(ctx, "close_error", "outbound", str(exc), 0)
            raise

    def _log(self, ctx: Any, event_type: str, direction: str, frame: str, elapsed: float) -> None:
        if self._logger is None:
            return
        try:
            self._logger.websocket_log(ctx, WebSocketLog(
                url=self._url,
                full_url=self._full_url,
                event_type=event_type,
                direction=direction,
                frame=frame[:65536],
                latency_ms=int(elapsed * 1000),
            ))
        except Exception:
            pass

    def __getattr__(self, name: str):
        return getattr(self._ws, name)
