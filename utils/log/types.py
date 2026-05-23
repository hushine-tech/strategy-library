"""
Type definitions for log entries.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict


class Type(Enum):
    SYSTEM = "system"
    ACCESS = "access"
    EXT_API = "ext_api"
    WEBSOCKET = "websocket"
    SQL = "sql"
    ROOT = "root"
    GRPC_ACCESS = "grpc_access"
    GRPC_EXT = "grpc_ext"
    KAFKA_SENT = "kafka_sent"
    KAFKA_RECV = "kafka_recv"

    def filename(self) -> str:
        return self.value + ".log"


class Level(Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    FATAL = "FATAL"


@dataclass
class AccessLog:
    method: str
    path: str
    request_header: Dict[str, str]
    request_body: str
    response_body: str
    http_status: int
    request_params: Dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0


@dataclass
class ExtAPILog:
    api_name: str
    url: str
    request_header: Dict[str, str]
    request_body: str
    response_body: str
    http_status: int
    latency_ms: int
    full_url: str = ""
    request_params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SQLLog:
    statement: str
    rows_affected: int
    latency_ms: int


@dataclass
class WebSocketLog:
    url: str
    full_url: str
    event_type: str
    direction: str
    frame: str
    latency_ms: int = 0


@dataclass
class GRPCAccessLog:
    method: str
    client_ip: str
    latency_ms: int
    status_code: int
    error: str
    request_params: Dict[str, Any] = field(default_factory=dict)
    response: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GRPCExtLog:
    method: str
    target_service: str
    latency_ms: int
    status_code: int
    error: str
    request_params: Dict[str, Any] = field(default_factory=dict)
    response: Dict[str, Any] = field(default_factory=dict)


@dataclass
class KafkaSentLog:
    topic: str
    partition: int
    offset: int
    message_size: int
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class KafkaRecvLog:
    topic: str
    partition: int
    offset: int
    lag_ms: int
    message_size: int
    consumer_group: str
    data: Dict[str, Any] = field(default_factory=dict)
