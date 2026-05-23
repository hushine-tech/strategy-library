"""
Kafka producer middleware.

Wraps kafka-python KafkaProducer to automatically log KafkaSentLog entries
and create OTel spans with traceparent header injection.
"""
import json
import time
from typing import Any, List, Optional, Protocol, Tuple

from utils.log.types import KafkaSentLog

try:
    from opentelemetry import trace, propagate as otel_propagate
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False


class KafkaSentLogger(Protocol):
    def kafka_sent_log(self, ctx: Any, sent: KafkaSentLog) -> None: ...


class ProducerMiddleware:
    """
    Wraps a kafka-python KafkaProducer and logs every sent message.

    Usage::

        from kafka import KafkaProducer
        raw = KafkaProducer(bootstrap_servers=["localhost:9092"])
        producer = ProducerMiddleware(raw, logger=log_instance)
        producer.send(ctx, "orders", b"key", b'{"symbol":"BTCUSDT"}')
    """

    def __init__(self, producer, logger: Optional[KafkaSentLogger] = None):
        self._producer = producer
        self._logger = logger

    def send(
        self,
        ctx: Any,
        topic: str,
        key: Optional[bytes],
        value: bytes,
        headers: Optional[List[Tuple[str, bytes]]] = None,
    ) -> Any:
        # Create OTel span — kept alive until send + metadata retrieval complete.
        carrier: dict = {}
        _span_ctx = None
        if _OTEL_AVAILABLE:
            try:
                tracer = trace.get_tracer(__name__)
                _span_ctx = tracer.start_as_current_span(
                    f"Kafka Produce {topic}",
                    attributes={"messaging.destination": topic},
                )
                _span_ctx.__enter__()
                otel_propagate.inject(carrier)
            except Exception:
                _span_ctx = None
                carrier = {}

        # Merge caller-supplied headers with OTel carrier.
        kafka_headers: List[Tuple[str, bytes]] = list(headers or [])
        for k, v in carrier.items():
            kafka_headers.append((k, v.encode("utf-8") if isinstance(v, str) else v))

        try:
            future = self._producer.send(
                topic, key=key, value=value,
                headers=kafka_headers if kafka_headers else None,
            )

            # Best-effort metadata retrieval; don't block the caller on failure.
            partition = 0
            offset = -1
            try:
                record_metadata = future.get(timeout=10)
                partition = record_metadata.partition
                offset = record_metadata.offset
            except Exception:
                pass

            if self._logger is not None:
                try:
                    data: dict = {}
                    if value:
                        try:
                            data = json.loads(value)
                        except Exception:
                            data = {"raw": value.decode(errors="replace")}
                    self._logger.kafka_sent_log(ctx, KafkaSentLog(
                        topic=topic,
                        partition=partition,
                        offset=offset,
                        message_size=len(value) if value else 0,
                        data=data,
                    ))
                except Exception:
                    pass

            return future
        finally:
            if _span_ctx is not None:
                try:
                    _span_ctx.__exit__(None, None, None)
                except Exception:
                    pass

    def __getattr__(self, name: str):
        return getattr(self._producer, name)
