"""Kafka consumer middleware with automatic kafka_recv logging."""
from collections import deque
from typing import Any, Dict, List, Optional
import json
import time

try:
    from opentelemetry import trace, propagate
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False


class KafkaConsumerMiddleware:
    """Wraps a Kafka consumer to automatically log message consumption.
    
    Usage:
        from kafka import KafkaConsumer
        from utils.middleware.kafka import KafkaConsumerMiddleware
        
        raw_consumer = KafkaConsumer(...)
        consumer = KafkaConsumerMiddleware(raw_consumer, "my-consumer-group", logger=logger)
        
        # All messages consumed will be auto-logged as kafka_recv
        for msg in consumer:
            process(msg)
    """

    def __init__(
        self,
        consumer: Any,
        consumer_group: str,
        logger: Optional[Any] = None,
        enabled: bool = True,
    ) -> None:
        """Initialize the Kafka consumer middleware.
        
        Args:
            consumer: Underlying Kafka consumer (e.g., kafka.KafkaConsumer or confluent_kafka.Consumer)
            consumer_group: Consumer group name for kafka_recv logging
            logger: Logger instance (must have kafka_recv_log method)
            enabled: Whether to log message consumption (can be toggled)
        """
        self._consumer = consumer
        self._consumer_group = consumer_group
        self._logger = logger
        self._enabled = enabled
        self._pending_messages = deque()

    def _log_kafka_recv(
        self,
        topic: str,
        partition: int,
        offset: int,
        lag_ms: int,
        message_size: int,
        data: Dict[str, Any],
    ) -> None:
        """Log a kafka_recv entry if enabled and logger available."""
        if not self._enabled:
            return

        if self._logger is None:
            return

        try:
            from utils.log import KafkaRecvLog
        except ImportError:
            return

        try:
            entry = KafkaRecvLog(
                topic=topic,
                partition=partition,
                offset=offset,
                lag_ms=lag_ms,
                message_size=message_size,
                consumer_group=self._consumer_group,
                data=data,
            )
            self._logger.kafka_recv_log(None, entry)
        except Exception:
            # Logging failures must not interrupt consumption.
            return

    def poll(self, *args, **kwargs) -> Optional[Any]:
        """Poll the underlying consumer and log every returned message."""
        poll_start = time.time()
        result = self._consumer.poll(*args, **kwargs)

        if result is None:
            return None

        for message in self._iter_messages(result):
            if hasattr(message, "error") and message.error() is not None:
                continue
            self._log_message(message, poll_start)
        return result

    def _iter_messages(self, poll_result: Any) -> list[Any]:
        if poll_result is None:
            return []
        if isinstance(poll_result, dict):
            messages: list[Any] = []
            for batch in poll_result.values():
                if batch:
                    messages.extend(batch)
            return messages
        return [poll_result]

    def _extract_lag_ms(self, message: Any, consumed_at: float, fallback_start: float) -> int:
        timestamp_attr = getattr(message, "timestamp", None)
        if timestamp_attr is None:
            return int((consumed_at - fallback_start) * 1000)

        try:
            raw_timestamp = timestamp_attr() if callable(timestamp_attr) else timestamp_attr
            if isinstance(raw_timestamp, (tuple, list)) and len(raw_timestamp) >= 2:
                ts_ms = raw_timestamp[1]
                if isinstance(ts_ms, (int, float)) and ts_ms > 0:
                    return max(0, int((consumed_at * 1000) - ts_ms))
            if isinstance(raw_timestamp, (int, float)) and raw_timestamp > 0:
                return max(0, int((consumed_at * 1000) - raw_timestamp))
        except Exception:
            return int((consumed_at - fallback_start) * 1000)

        return int((consumed_at - fallback_start) * 1000)

    def _log_message(self, message: Any, poll_start: float) -> None:
        """Extract fields from message and log kafka_recv."""
        consumed_at = time.time()
        topic_func = getattr(message, "topic", None)
        topic = topic_func() if callable(topic_func) else topic_func
        partition_func = getattr(message, "partition", None)
        partition = partition_func() if callable(partition_func) else partition_func
        offset_func = getattr(message, "offset", None)
        offset = offset_func() if callable(offset_func) else offset_func
        size_func = getattr(message, "size", None)
        message_size = size_func() if callable(size_func) else size_func

        lag_ms = self._extract_lag_ms(message, consumed_at, poll_start)

        data: Dict[str, Any] = {}
        try:
            if hasattr(message, "value"):
                raw_value_func = getattr(message, "value", None)
                raw_value = raw_value_func() if callable(raw_value_func) else raw_value_func
                if isinstance(raw_value, bytes):
                    raw_value = raw_value.decode("utf-8", errors="replace")
                if isinstance(raw_value, str):
                    data = json.loads(raw_value)
                elif isinstance(raw_value, dict):
                    data = raw_value
                if message_size is None and isinstance(raw_value, str):
                    message_size = len(raw_value.encode("utf-8"))
        except Exception:
            pass

        # Extract parent trace context from message headers if present.
        if _OTEL_AVAILABLE:
            try:
                raw_headers = getattr(message, "headers", None)
                if raw_headers:
                    if callable(raw_headers):
                        raw_headers = raw_headers()
                    carrier = {}
                    for h in (raw_headers or []):
                        if isinstance(h, (tuple, list)) and len(h) == 2:
                            k = h[0].decode() if isinstance(h[0], bytes) else h[0]
                            v = h[1].decode() if isinstance(h[1], bytes) else h[1]
                            carrier[k] = v
                    if carrier:
                        _ctx = propagate.extract(carrier)
                        _tracer = trace.get_tracer("middleware.kafka.consumer")
                        _span = _tracer.start_span(f"Kafka Consume {topic}", context=_ctx)
                        _span.end()
            except Exception:
                pass

        self._log_kafka_recv(
            topic=topic,
            partition=partition,
            offset=offset,
            lag_ms=lag_ms,
            message_size=message_size,
            data=data,
        )

    def __iter__(self) -> "KafkaConsumerMiddleware":
        """Allow iteration over the consumer."""
        return self

    def __next__(self) -> Any:
        """Get next message with kafka_recv logging.

        Per the Python iterator protocol, __next__ must not accept parameters.
        Delegates to next_message() with the default timeout.
        """
        return self.next_message()

    def next_message(self, timeout_ms: float = 1.0) -> Any:
        """Poll for the next message with kafka_recv logging.

        Unlike poll(), this method raises StopIteration when no message is
        available, making it suitable for use as the iterator implementation.

        Args:
            timeout_ms: Poll timeout in milliseconds.

        Returns:
            The next message.

        Raises:
            StopIteration: If no message is available within the timeout.
        """
        while True:
            if self._pending_messages:
                return self._pending_messages.popleft()

            poll_start = time.time()
            result = self._consumer.poll(timeout_ms=timeout_ms)
            messages = self._iter_messages(result)

            if not messages:
                raise StopIteration

            for message in messages:
                if hasattr(message, "error") and message.error() is not None:
                    self._pending_messages.append(message)
                    continue
                self._log_message(message, poll_start)
                self._pending_messages.append(message)

    def subscribe(self, topics: List[str], **kwargs) -> None:
        """Subscribe to topics (delegates to underlying consumer)."""
        self._consumer.subscribe(topics, **kwargs)

    def close(self) -> None:
        """Close the consumer."""
        self._consumer.close()

    def __getattr__(self, name: str) -> Any:
        """Delegate all other attributes to the underlying consumer."""
        return getattr(self._consumer, name)
