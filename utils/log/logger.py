"""
Logger implementation for Python SDK.
"""
import json
import os
import queue
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .types import (
    AccessLog as AccessLogType,
    ExtAPILog as ExtAPILogType,
    GRPCAccessLog as GRPCAccessLogType,
    GRPCExtLog as GRPCExtLogType,
    KafkaRecvLog as KafkaRecvLogType,
    KafkaSentLog as KafkaSentLogType,
    Level,
    SQLLog as SQLLogType,
    Type,
    WebSocketLog as WebSocketLogType,
)
from .context import get_session_id

MAX_MESSAGE_LEN = 65536


def _get_trace_fields():
    """Return (trace_id, span_id) from the current OTel span (or empty strings)."""
    try:
        from .tracer import get_trace_span_ids
        return get_trace_span_ids()
    except Exception:
        return "", ""


def _get_host() -> str:
    """Detect host name."""
    for key in ("HOSTNAME", "HOST", "COMPUTERNAME"):
        val = os.environ.get(key)
        if val:
            return val
    return uuid.uuid4().hex[:8]


def _truncate_message(msg: str) -> str:
    if len(msg) <= MAX_MESSAGE_LEN:
        return msg
    return msg[:MAX_MESSAGE_LEN - 15] + "...(truncated)"


def _format_log_time(now: datetime) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.isoformat(timespec="microseconds")


def _get_timestamp() -> tuple:
    now = datetime.now(timezone.utc)
    return int(now.timestamp() * 1000), _format_log_time(now)


# ---------------------------------------------------------------------------
# Kafka backend
# ---------------------------------------------------------------------------

class _KafkaBackend:
    """
    Optional Kafka backend.  Sends log entries to {topic_prefix}-{log_type}
    topics matching golang-lib's naming convention.

    Failures are silently ignored so they never affect file logging or business
    logic (mirrors safeBackendCall in golang-lib).
    """

    def __init__(self, brokers: List[str], topic_prefix: str, buffer_size: int = 10_000):
        from kafka import KafkaProducer  # type: ignore
        self._producer = KafkaProducer(
            bootstrap_servers=brokers,
            value_serializer=lambda v: v if isinstance(v, bytes) else v.encode("utf-8"),
            retries=3,
        )
        self._prefix = topic_prefix
        self._queue: queue.Queue = queue.Queue(maxsize=buffer_size)
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def send(self, entry: dict) -> None:
        try:
            data = json.dumps(entry, ensure_ascii=False)
            self._queue.put_nowait(data)
        except Exception:
            pass

    def _worker(self) -> None:
        while not self._done.is_set() or not self._queue.empty():
            try:
                data = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                log_type = json.loads(data).get("type", "root")
                topic = f"{self._prefix}-{log_type}"
                self._producer.send(topic, data)
            except Exception:
                pass

    def close(self) -> None:
        self._done.set()
        self._thread.join(timeout=5)
        try:
            self._producer.flush(timeout=5)
            self._producer.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

class Logger:
    """Main logger class."""

    def __init__(
        self,
        output_dir: str,
        types: List[Type],
        kafka_backend: Optional[_KafkaBackend] = None,
    ):
        self._output_dir = output_dir
        self._files: Dict[Type, Optional[object]] = {}
        self._lock = threading.Lock()
        self._host = _get_host()
        self._kafka = kafka_backend

        os.makedirs(output_dir, exist_ok=True)

        for t in types:
            filepath = os.path.join(output_dir, t.filename())
            self._files[t] = open(filepath, "a", encoding="utf-8")

    def _write_entry(self, log_type: Type, entry: dict) -> None:
        with self._lock:
            f = self._files.get(log_type) or self._files.get(Type.ROOT)
            if f is not None:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()

        if self._kafka is not None:
            try:
                self._kafka.send(entry)
            except Exception:
                pass

    def _log(self, log_type: Type, message: str, level: Level) -> None:
        session_id = get_session_id()
        trace_id, span_id = _get_trace_fields()
        unix_ms, log_time = _get_timestamp()
        entry = {
            "timestamp": unix_ms,
            "log_time": log_time,
            "level": level.value,
            "type": log_type.value,
            "host": self._host,
            "message": _truncate_message(message),
            "session_id": session_id,
            "trace_id": trace_id,
            "span_id": span_id,
        }
        self._write_entry(log_type, entry)

    def info(self, ctx, log_type: Type, msg: str) -> None:
        self._log(log_type, msg, Level.INFO)

    def debug(self, ctx, log_type: Type, msg: str) -> None:
        self._log(log_type, msg, Level.DEBUG)

    def warn(self, ctx, log_type: Type, msg: str) -> None:
        self._log(log_type, msg, Level.WARN)

    def error(self, ctx, log_type: Type, msg: str) -> None:
        self._log(log_type, msg, Level.ERROR)

    def fatal(self, ctx, log_type: Type, msg: str) -> None:
        self._log(log_type, msg, Level.FATAL)

    def access_log(self, ctx, access: AccessLogType) -> None:
        session_id = get_session_id()
        trace_id, span_id = _get_trace_fields()
        unix_ms, log_time = _get_timestamp()
        entry = {
            "timestamp": unix_ms,
            "log_time": log_time,
            "level": Level.INFO.value,
            "type": Type.ACCESS.value,
            "host": self._host,
            "message": _truncate_message(f"{access.method} {access.path}"),
            "session_id": session_id,
            "trace_id": trace_id,
            "span_id": span_id,
            "method": access.method,
            "path": access.path,
            "request_header": access.request_header,
            "request_params": access.request_params,
            "request_body": access.request_body,
            "response_body": access.response_body,
            "http_status": access.http_status,
            "latency_ms": access.latency_ms,
        }
        self._write_entry(Type.ACCESS, entry)

    def ext_api_log(self, ctx, ext_api: ExtAPILogType) -> None:
        session_id = get_session_id()
        trace_id, span_id = _get_trace_fields()
        unix_ms, log_time = _get_timestamp()
        entry = {
            "timestamp": unix_ms,
            "log_time": log_time,
            "level": Level.INFO.value,
            "type": Type.EXT_API.value,
            "host": self._host,
            "message": _truncate_message(f"{ext_api.api_name} {ext_api.url}"),
            "session_id": session_id,
            "trace_id": trace_id,
            "span_id": span_id,
            "api_name": ext_api.api_name,
            "url": ext_api.url,
            "full_url": ext_api.full_url,
            "request_header": ext_api.request_header,
            "request_params": ext_api.request_params,
            "request_body": ext_api.request_body,
            "response_body": ext_api.response_body,
            "http_status": ext_api.http_status,
            "latency_ms": ext_api.latency_ms,
        }
        self._write_entry(Type.EXT_API, entry)

    def websocket_log(self, ctx, ws: WebSocketLogType) -> None:
        session_id = get_session_id()
        trace_id, span_id = _get_trace_fields()
        unix_ms, log_time = _get_timestamp()
        entry = {
            "timestamp": unix_ms,
            "log_time": log_time,
            "level": Level.INFO.value,
            "type": Type.WEBSOCKET.value,
            "host": self._host,
            "message": _truncate_message(f"{ws.url} {ws.event_type} {ws.direction}"),
            "session_id": session_id,
            "trace_id": trace_id,
            "span_id": span_id,
            "url": ws.url,
            "full_url": ws.full_url,
            "event_type": ws.event_type,
            "direction": ws.direction,
            "frame": _truncate_message(ws.frame),
            "latency_ms": ws.latency_ms,
        }
        self._write_entry(Type.WEBSOCKET, entry)

    def sql_log(self, ctx, sql: SQLLogType) -> None:
        session_id = get_session_id()
        trace_id, span_id = _get_trace_fields()
        unix_ms, log_time = _get_timestamp()
        entry = {
            "timestamp": unix_ms,
            "log_time": log_time,
            "level": Level.INFO.value,
            "type": Type.SQL.value,
            "host": self._host,
            "message": _truncate_message(sql.statement),
            "session_id": session_id,
            "trace_id": trace_id,
            "span_id": span_id,
            "statement": sql.statement,
            "rows_affected": sql.rows_affected,
            "latency_ms": sql.latency_ms,
        }
        self._write_entry(Type.SQL, entry)

    def grpc_access_log(self, ctx, access: GRPCAccessLogType) -> None:
        session_id = get_session_id()
        trace_id, span_id = _get_trace_fields()
        unix_ms, log_time = _get_timestamp()
        entry = {
            "timestamp": unix_ms,
            "log_time": log_time,
            "level": Level.INFO.value,
            "type": Type.GRPC_ACCESS.value,
            "host": self._host,
            "message": _truncate_message(access.method),
            "session_id": session_id,
            "trace_id": trace_id,
            "span_id": span_id,
            "method": access.method,
            "client_ip": access.client_ip,
            "latency_ms": access.latency_ms,
            "status_code": access.status_code,
            "error": access.error,
            "request_params": access.request_params,
            "response": access.response,
        }
        self._write_entry(Type.GRPC_ACCESS, entry)

    def grpc_ext_log(self, ctx, grpc_ext: GRPCExtLogType) -> None:
        session_id = get_session_id()
        trace_id, span_id = _get_trace_fields()
        unix_ms, log_time = _get_timestamp()
        entry = {
            "timestamp": unix_ms,
            "log_time": log_time,
            "level": Level.INFO.value,
            "type": Type.GRPC_EXT.value,
            "host": self._host,
            "message": _truncate_message(f"{grpc_ext.method} {grpc_ext.target_service}"),
            "session_id": session_id,
            "trace_id": trace_id,
            "span_id": span_id,
            "method": grpc_ext.method,
            "target_service": grpc_ext.target_service,
            "latency_ms": grpc_ext.latency_ms,
            "status_code": grpc_ext.status_code,
            "error": grpc_ext.error,
            "request_params": grpc_ext.request_params,
            "response": grpc_ext.response,
        }
        self._write_entry(Type.GRPC_EXT, entry)

    def kafka_sent_log(self, ctx, sent: KafkaSentLogType) -> None:
        session_id = get_session_id()
        trace_id, span_id = _get_trace_fields()
        unix_ms, log_time = _get_timestamp()
        entry = {
            "timestamp": unix_ms,
            "log_time": log_time,
            "level": Level.INFO.value,
            "type": Type.KAFKA_SENT.value,
            "host": self._host,
            "message": _truncate_message(f"{sent.topic} {sent.partition} {sent.offset}"),
            "session_id": session_id,
            "trace_id": trace_id,
            "span_id": span_id,
            "topic": sent.topic,
            "partition": sent.partition,
            "offset": sent.offset,
            "message_size": sent.message_size,
            "data": sent.data,
        }
        self._write_entry(Type.KAFKA_SENT, entry)

    def kafka_recv_log(self, ctx, kafka_recv: KafkaRecvLogType) -> None:
        session_id = get_session_id()
        trace_id, span_id = _get_trace_fields()
        unix_ms, log_time = _get_timestamp()
        entry = {
            "timestamp": unix_ms,
            "log_time": log_time,
            "level": Level.INFO.value,
            "type": Type.KAFKA_RECV.value,
            "host": self._host,
            "message": _truncate_message(f"{kafka_recv.topic} {kafka_recv.partition} {kafka_recv.offset}"),
            "session_id": session_id,
            "trace_id": trace_id,
            "span_id": span_id,
            "topic": kafka_recv.topic,
            "partition": kafka_recv.partition,
            "offset": kafka_recv.offset,
            "lag_ms": kafka_recv.lag_ms,
            "message_size": kafka_recv.message_size,
            "consumer_group": kafka_recv.consumer_group,
            "data": kafka_recv.data,
        }
        self._write_entry(Type.KAFKA_RECV, entry)

    def close(self) -> None:
        with self._lock:
            for f in self._files.values():
                if f is not None:
                    try:
                        f.close()
                    except Exception:
                        pass
            self._files.clear()
        if self._kafka is not None:
            try:
                self._kafka.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Global logger state
# ---------------------------------------------------------------------------

_default_logger: Optional[Logger] = None
_logger_lock = threading.Lock()


def init_log(output_dir: str = "") -> Logger:
    """Initialize global logger. Output dir from arg or LOG_OUTPUT_DIR env var (default ./logs)."""
    global _default_logger
    if not output_dir:
        output_dir = os.environ.get("LOG_OUTPUT_DIR", "./logs")
    with _logger_lock:
        _default_logger = Logger(output_dir, list(Type))
    return _default_logger


def init_log_with_kafka(
    output_dir: str,
    brokers: List[str],
    topic_prefix: str,
    types: Optional[List[Type]] = None,
) -> Logger:
    """Initialize global logger with both file and Kafka backends."""
    global _default_logger
    kafka = _KafkaBackend(brokers, topic_prefix)
    with _logger_lock:
        _default_logger = Logger(output_dir, types or list(Type), kafka_backend=kafka)
    return _default_logger


def init(output_dir: str, *types: Type) -> Logger:
    """Legacy init function - use init_log() instead."""
    global _default_logger
    with _logger_lock:
        _default_logger = Logger(output_dir, list(types) if types else list(Type))
    return _default_logger


def close() -> None:
    global _default_logger
    with _logger_lock:
        if _default_logger is not None:
            _default_logger.close()
            _default_logger = None


# ---------------------------------------------------------------------------
# Global convenience functions
# ---------------------------------------------------------------------------

def info(ctx, log_type: Type, msg: str) -> None:
    if _default_logger:
        _default_logger.info(ctx, log_type, msg)


def debug(ctx, log_type: Type, msg: str) -> None:
    if _default_logger:
        _default_logger.debug(ctx, log_type, msg)


def warn(ctx, log_type: Type, msg: str) -> None:
    if _default_logger:
        _default_logger.warn(ctx, log_type, msg)


def error(ctx, log_type: Type, msg: str) -> None:
    if _default_logger:
        _default_logger.error(ctx, log_type, msg)


def fatal(ctx, log_type: Type, msg: str) -> None:
    if _default_logger:
        _default_logger.fatal(ctx, log_type, msg)


def access_log(ctx, access: AccessLogType) -> None:
    if _default_logger:
        _default_logger.access_log(ctx, access)


def ext_api_log(ctx, ext_api: ExtAPILogType) -> None:
    if _default_logger:
        _default_logger.ext_api_log(ctx, ext_api)


def websocket_log(ctx, ws: WebSocketLogType) -> None:
    if _default_logger:
        _default_logger.websocket_log(ctx, ws)


def sql_log(ctx, sql: SQLLogType) -> None:
    if _default_logger:
        _default_logger.sql_log(ctx, sql)


def grpc_access_log(ctx, access: GRPCAccessLogType) -> None:
    if _default_logger:
        _default_logger.grpc_access_log(ctx, access)


def grpc_ext_log(ctx, ext: GRPCExtLogType) -> None:
    if _default_logger:
        _default_logger.grpc_ext_log(ctx, ext)


def kafka_sent_log(ctx, sent: KafkaSentLogType) -> None:
    if _default_logger:
        _default_logger.kafka_sent_log(ctx, sent)


def kafka_recv_log(ctx, recv: KafkaRecvLogType) -> None:
    if _default_logger:
        _default_logger.kafka_recv_log(ctx, recv)


@contextmanager
def session(session_id: Optional[str] = None):
    """
    Context manager for setting session ID.
    Usage:
        with log.session("my-session"):
            log.info(None, log.Type.ROOT, "message")
    """
    from .context import set_session_id, _session_id_var
    _old_session_id = _session_id_var.get()
    _session_id = session_id or str(uuid.uuid4())
    set_session_id(_session_id)
    try:
        yield _session_id
    finally:
        set_session_id(_old_session_id)
