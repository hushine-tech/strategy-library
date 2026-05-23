"""Tests for Kafka consumer middleware."""
from unittest.mock import MagicMock, patch


class MockMessage:
    """Mock Kafka message for testing."""

    def __init__(
        self,
        topic: str = "test-topic",
        partition: int = 0,
        offset: int = 100,
        value: str = '{"symbol":"BTCUSDT","price":50000}',
        size: int = 42,
        timestamp_ms: int = 0,
    ):
        self._topic = topic
        self._partition = partition
        self._offset = offset
        self._value = value
        self._size = size
        self._timestamp_ms = timestamp_ms

    def topic(self) -> str:
        return self._topic

    def partition(self) -> int:
        return self._partition

    def offset(self) -> int:
        return self._offset

    def value(self) -> bytes:
        return self._value.encode("utf-8")

    def size(self) -> int:
        return self._size

    def error(self):
        return None

    def timestamp(self):
        return (0, self._timestamp_ms)


class MockConsumer:
    """Mock Kafka consumer for testing."""

    def __init__(self, messages=None):
        self._messages = messages or []
        self._index = 0
        self._subscribed = False

    def poll(self, timeout_ms=1.0):
        if self._index < len(self._messages):
            msg = self._messages[self._index]
            self._index += 1
            return msg
        return None

    def subscribe(self, topics, **kwargs):
        self._subscribed = True

    def close(self):
        pass


class TestKafkaConsumerMiddleware:
    """Tests for KafkaConsumerMiddleware."""

    def test_middleware_logs_message_consumption(self):
        """Test that consuming a message triggers kafka_recv logging."""
        from utils.middleware.kafka import KafkaConsumerMiddleware

        mock_logger = MagicMock()
        raw_consumer = MockConsumer(messages=[MockMessage()])
        middleware = KafkaConsumerMiddleware(
            raw_consumer, "test-consumer-group", logger=mock_logger
        )

        message = middleware.poll(timeout_ms=1.0)

        assert message is not None
        mock_logger.kafka_recv_log.assert_called_once()
        call_args = mock_logger.kafka_recv_log.call_args
        entry = call_args[0][1]

        assert entry.topic == "test-topic"
        assert entry.partition == 0
        assert entry.offset == 100
        assert entry.consumer_group == "test-consumer-group"
        assert entry.message_size == 42

    def test_middleware_disabled_does_not_log(self):
        """Test that disabled middleware does not log."""
        from utils.middleware.kafka import KafkaConsumerMiddleware

        mock_logger = MagicMock()
        raw_consumer = MockConsumer(messages=[MockMessage()])
        middleware = KafkaConsumerMiddleware(
            raw_consumer,
            "test-consumer-group",
            logger=mock_logger,
            enabled=False,
        )

        message = middleware.poll(timeout_ms=1.0)

        assert message is not None
        mock_logger.kafka_recv_log.assert_not_called()

    def test_middleware_without_logger_does_not_crash(self):
        """Test that middleware works without a logger."""
        from utils.middleware.kafka import KafkaConsumerMiddleware

        raw_consumer = MockConsumer(messages=[MockMessage()])
        middleware = KafkaConsumerMiddleware(
            raw_consumer, "test-consumer-group", logger=None
        )

        message = middleware.poll(timeout_ms=1.0)

        assert message is not None

    def test_middleware_iteration(self):
        """Test iteration over consumer yields logged messages."""
        from utils.middleware.kafka import KafkaConsumerMiddleware

        mock_logger = MagicMock()
        raw_consumer = MockConsumer(
            messages=[MockMessage(), MockMessage(topic="topic-2")]
        )
        middleware = KafkaConsumerMiddleware(
            raw_consumer, "test-group", logger=mock_logger
        )

        count = 0
        for msg in middleware:
            count += 1
            if count >= 2:
                break

        assert count == 2
        assert mock_logger.kafka_recv_log.call_count == 2

    def test_middleware_subscribes_to_topics(self):
        """Test that subscribe is delegated to underlying consumer."""
        from utils.middleware.kafka import KafkaConsumerMiddleware

        raw_consumer = MockConsumer()
        middleware = KafkaConsumerMiddleware(
            raw_consumer, "test-group", logger=None
        )

        middleware.subscribe(["topic-a", "topic-b"])

        assert raw_consumer._subscribed is True

    def test_middleware_close(self):
        """Test that close is delegated to underlying consumer."""
        from utils.middleware.kafka import KafkaConsumerMiddleware

        raw_consumer = MockConsumer()
        middleware = KafkaConsumerMiddleware(
            raw_consumer, "test-group", logger=None
        )

        middleware.close()
        # No exception means success

    def test_kafka_recv_log_fields(self):
        """Test kafka_recv log fields are correctly extracted."""
        from utils.middleware.kafka import KafkaConsumerMiddleware

        mock_logger = MagicMock()
        msg = MockMessage(
            topic="orders",
            partition=3,
            offset=999,
            value='{"order_id":"ORD-123","qty":1.5}',
            size=128,
        )
        raw_consumer = MockConsumer(messages=[msg])
        middleware = KafkaConsumerMiddleware(
            raw_consumer, "order-processor", logger=mock_logger
        )

        middleware.poll(timeout_ms=1.0)

        call_args = mock_logger.kafka_recv_log.call_args
        entry = call_args[0][1]

        assert entry.topic == "orders"
        assert entry.partition == 3
        assert entry.offset == 999
        assert entry.message_size == 128
        assert entry.consumer_group == "order-processor"
        assert entry.data["order_id"] == "ORD-123"

    def test_lag_ms_calculated_from_message_timestamp(self):
        from utils.middleware.kafka import KafkaConsumerMiddleware

        mock_logger = MagicMock()
        message_ts_ms = 1_000_000
        raw_consumer = MockConsumer(messages=[MockMessage(timestamp_ms=message_ts_ms)])
        middleware = KafkaConsumerMiddleware(raw_consumer, "test-group", logger=mock_logger)

        with patch("utils.middleware.kafka.consumer.time.time", return_value=1005.0):
            middleware.poll(timeout_ms=1.0)

        call_args = mock_logger.kafka_recv_log.call_args
        entry = call_args[0][1]
        assert entry.lag_ms == 5000

    def test_logger_failure_does_not_break_poll(self):
        from utils.middleware.kafka import KafkaConsumerMiddleware

        mock_logger = MagicMock()
        mock_logger.kafka_recv_log.side_effect = RuntimeError("sink failed")
        raw_consumer = MockConsumer(messages=[MockMessage()])
        middleware = KafkaConsumerMiddleware(raw_consumer, "test-group", logger=mock_logger)

        message = middleware.poll(timeout_ms=1.0)

        assert message is not None
