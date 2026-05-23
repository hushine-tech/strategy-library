"""
LiveDataSource - Kafka-backed real-time market data subscription.

Subscribes to Kafka market topics and invokes registered callbacks
when messages are received. Uses KafkaConsumerMiddleware for
automatic kafka_recv logging.
"""
import json
import logging
import threading
from typing import Callable, Dict, List, Optional, Any

from .config import KafkaConfig
from .models import MarketKline, MarketOI, MarketFunding, MarketOrderBook

logger = logging.getLogger(__name__)


class LiveDataSource:
    """Kafka-backed data source for real-time market data.

    Uses a push model (callbacks/iterator) rather than the pull model of
    DataSource. Does not inherit from DataSource.
    """

    def __init__(
        self,
        config: Optional[KafkaConfig] = None,
        logger: Optional[Any] = None,
    ):
        """Initialize LiveDataSource with Kafka config.

        Args:
            config: KafkaConfig instance. If None, loads from config file.
            logger: Logger instance for kafka_recv logging (must have kafka_recv_log method).
        """
        if config is None:
            config = self._load_config()
        self._config = config
        self._logger = logger
        self._consumer: Optional[Any] = None
        self._middleware: Optional[Any] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

        self._callbacks: Dict[str, List[Callable]] = {
            "kline": [],
            "oi": [],
            "funding": [],
            "orderbook": [],
        }

    def _should_deliver_kline(self, topic: str | None, kline: MarketKline) -> bool:
        subscription = self._config.live_kline_subscription
        if subscription is None:
            return True
        return subscription.matches(
            topic=topic,
            symbol=kline.symbol,
            market=kline.market,
            interval=kline.interval,
        )

    def _load_config(self) -> KafkaConfig:
        """Load Kafka config from config file."""
        try:
            import yaml
            with open("config.yaml", "r") as f:
                config_data = yaml.safe_load(f)
            kafka_data = config_data.get("market_data", {}).get("kafka", {})
            return KafkaConfig.from_dict(kafka_data)
        except Exception as e:
            logger.warning(f"Failed to load config from file: {e}, using defaults")
            return KafkaConfig()

    def _create_consumer(self) -> Any:
        """Create and wrap Kafka consumer with middleware."""
        from kafka import KafkaConsumer
        from utils.middleware.kafka import KafkaConsumerMiddleware

        raw_consumer = KafkaConsumer(
            bootstrap_servers=self._config.bootstrap_servers,
            group_id=self._config.consumer_group,
            auto_offset_reset=self._config.auto_offset_reset,
            enable_auto_commit=self._config.enable_auto_commit,
            value_deserializer=lambda m: json.loads(m.decode("utf-8")),
        )

        self._middleware = KafkaConsumerMiddleware(
            raw_consumer,
            consumer_group=self._config.consumer_group,
            logger=self._logger,
        )

        return self._middleware

    def on_kline(self, callback: Callable[[MarketKline], None]) -> None:
        """Register a callback for kline messages."""
        self._callbacks["kline"].append(callback)

    def on_oi(self, callback: Callable[[MarketOI], None]) -> None:
        """Register a callback for open interest messages."""
        self._callbacks["oi"].append(callback)

    def on_funding(self, callback: Callable[[MarketFunding], None]) -> None:
        """Register a callback for funding rate messages."""
        self._callbacks["funding"].append(callback)

    def on_orderbook(self, callback: Callable[[MarketOrderBook], None]) -> None:
        """Register a callback for orderbook messages."""
        self._callbacks["orderbook"].append(callback)

    def _process_message(self, message: Any) -> None:
        """Process a Kafka message and invoke appropriate callbacks."""
        try:
            topic_attr = getattr(message, "topic", None)
            topic = topic_attr() if callable(topic_attr) else topic_attr
            value_attr = getattr(message, "value", None)
            data = value_attr() if callable(value_attr) else value_attr
            message_type = self._resolve_message_type(topic, data)

            if message_type == "kline":
                model = MarketKline.from_dict(data)
                if not self._should_deliver_kline(topic, model):
                    return
                for cb in self._callbacks["kline"]:
                    cb(model)
            elif message_type == "oi":
                model = MarketOI.from_dict(data)
                for cb in self._callbacks["oi"]:
                    cb(model)
            elif message_type == "funding":
                model = MarketFunding.from_dict(data)
                for cb in self._callbacks["funding"]:
                    cb(model)
            elif message_type == "orderbook":
                model = MarketOrderBook.from_dict(data)
                for cb in self._callbacks["orderbook"]:
                    cb(model)
            else:
                logger.warning(f"Unknown topic: {topic}")

        except ValueError as e:
            logger.warning(f"Malformed message on {getattr(message, 'topic', 'unknown')}: {e}")
        except Exception as e:
            logger.warning(f"Error processing message on {getattr(message, 'topic', 'unknown')}: {e}")

    @staticmethod
    def _infer_message_type(data: Any) -> Optional[str]:
        if not isinstance(data, dict):
            return None
        if {"interval", "open_time", "close_time", "open", "high", "low", "close", "volume", "timestamp"}.issubset(data):
            return "kline"
        if {"open_interest", "period", "timestamp"}.issubset(data):
            return "oi"
        if {"funding_rate", "mark_price", "next_funding_time", "timestamp"}.issubset(data):
            return "funding"
        if {"bids", "asks", "timestamp"}.issubset(data):
            return "orderbook"
        return None

    def _resolve_message_type(self, topic: Any, data: Any) -> Optional[str]:
        topic_map = {
            "market-kline": "kline",
            "market-oi": "oi",
            "market-funding": "funding",
            "market-orderbook": "orderbook",
        }
        if topic in topic_map:
            return topic_map[topic]
        if isinstance(topic, str) and topic.startswith("md.kline."):
            return "kline"
        return self._infer_message_type(data)

    @staticmethod
    def _iter_polled_messages(result: Any) -> list[Any]:
        if result is None:
            return []
        if isinstance(result, dict):
            messages: list[Any] = []
            for batch in result.values():
                if batch:
                    messages.extend(batch)
            return messages
        return [result]

    def _consume_loop(self) -> None:
        """Main consumption loop running in background thread."""
        while self._running:
            try:
                poll_result = self._consumer.poll(timeout_ms=100)
                messages = self._iter_polled_messages(poll_result)
                if not messages:
                    continue
                for message in messages:
                    if hasattr(message, "error") and message.error() is not None:
                        logger.warning(f"Kafka error: {message.error()}")
                        continue
                    self._process_message(message)
            except Exception as e:
                logger.warning(f"Error in consume loop: {e}")

    def start(self) -> None:
        """Start consuming messages from Kafka topics."""
        if self._running:
            return

        self._consumer = self._create_consumer()
        self._consumer.subscribe(self._config.topics)
        self._running = True
        self._thread = threading.Thread(target=self._consume_loop, daemon=True)
        self._thread.start()
        logger.info(f"LiveDataSource started, subscribed to: {self._config.topics}")

    def stop(self) -> None:
        """Stop consuming messages and close the consumer."""
        if not self._running:
            return

        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

        if self._consumer is not None:
            self._consumer.close()
            self._consumer = None

        logger.info("LiveDataSource stopped")

    def get_klines(self, *args, **kwargs):
        raise NotImplementedError("LiveDataSource does not support pull-based kline queries")

    def get_open_interest(self, *args, **kwargs):
        raise NotImplementedError("LiveDataSource does not support pull-based open interest queries")

    def get_funding_rates(self, *args, **kwargs):
        raise NotImplementedError("LiveDataSource does not support pull-based funding rate queries")

    def get_orderbook(self, *args, **kwargs):
        raise NotImplementedError("LiveDataSource does not support pull-based orderbook queries")
