"""
Unified logging SDK for Binance trading systems.
"""
from .types import (
    Type,
    Level,
    AccessLog,
    ExtAPILog,
    SQLLog,
    WebSocketLog,
    GRPCAccessLog,
    GRPCExtLog,
    KafkaSentLog,
    KafkaRecvLog,
)
from .logger import (
    Logger,
    init,
    init_log,
    init_log_with_kafka,
    close,
    session,
    info,
    debug,
    warn,
    error,
    fatal,
    access_log,
    ext_api_log,
    websocket_log,
    sql_log,
    grpc_access_log,
    grpc_ext_log,
    kafka_sent_log,
    kafka_recv_log,
)
from .context import get_session_id

__all__ = [
    "Type",
    "Level",
    "AccessLog",
    "ExtAPILog",
    "SQLLog",
    "WebSocketLog",
    "GRPCAccessLog",
    "GRPCExtLog",
    "KafkaSentLog",
    "KafkaRecvLog",
    "Logger",
    "init",
    "init_log",
    "init_log_with_kafka",
    "close",
    "session",
    "info",
    "debug",
    "warn",
    "error",
    "fatal",
    "access_log",
    "ext_api_log",
    "websocket_log",
    "sql_log",
    "grpc_access_log",
    "grpc_ext_log",
    "kafka_sent_log",
    "kafka_recv_log",
    "get_session_id",
    "ServerAccessInterceptor",
    "ClientExtInterceptor",
]


def __getattr__(name: str):
    if name in {"ServerAccessInterceptor", "ClientExtInterceptor"}:
        from .grpc_interceptors import ClientExtInterceptor, ServerAccessInterceptor

        return {
            "ServerAccessInterceptor": ServerAccessInterceptor,
            "ClientExtInterceptor": ClientExtInterceptor,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
