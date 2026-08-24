"""
Configuration schema for market_data module.

Defines the structure for TimescaleDB and Kafka configuration
as used in the system's YAML config file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional

DEFAULT_KAFKA_TOPICS: List[str] = [
    "market-kline",
    "market-oi",
    "market-funding",
    "market-orderbook",
]
LIVE_KLINE_TOPIC_PREFIX = "md.kline"

_TIMESCALE_DEFAULTS = {
    "host": "localhost",
    "port": 5432,
    "database": "market_data",
    "user": "postgres",
    "password": "",
    "pool_size": 5,
    "max_overflow": 10,
}


def _normalize_symbol(symbol: str) -> str:
    return str(symbol).strip().upper()


def _normalize_market(market: str) -> str:
    return str(market).strip().lower()


def _normalize_interval(interval: str) -> str:
    interval = str(interval).strip().lower()
    return interval or "1m"


def _normalize_exchange(exchange: str) -> str:
    exchange = str(exchange).strip().lower()
    return exchange or "binance"


def resolve_live_kline_topic(exchange: str, market: str, interval: str) -> str:
    return ".".join(
        [
            LIVE_KLINE_TOPIC_PREFIX,
            _normalize_exchange(exchange),
            _normalize_market(market),
            _normalize_interval(interval),
        ]
    )


def parse_live_kline_topic(topic: str | None) -> tuple[str, str, str] | None:
    if not isinstance(topic, str):
        return None
    parts = topic.strip().split(".")
    if len(parts) != 5 or parts[0] != "md" or parts[1] != "kline":
        return None
    _, _, exchange, market, interval = parts
    return (
        _normalize_exchange(exchange),
        _normalize_market(market),
        _normalize_interval(interval),
    )


@dataclass(frozen=True)
class LiveKlineSubscription:
    allowed_inputs: frozenset[tuple[str, str, str]]
    exchange: str = "binance"
    consumer_group: str = "market-data-consumer"

    def __post_init__(self) -> None:
        normalized_inputs = frozenset(
            (
                _normalize_symbol(sym),
                _normalize_market(mk),
                _normalize_interval(iv),
            )
            for sym, mk, iv in self.allowed_inputs
        )
        if not normalized_inputs:
            raise ValueError("live subscription requires at least one declared input")

        consumer_group = str(self.consumer_group).strip() or "market-data-consumer"

        object.__setattr__(self, "exchange", _normalize_exchange(self.exchange))
        object.__setattr__(self, "consumer_group", consumer_group)
        object.__setattr__(self, "allowed_inputs", normalized_inputs)

    @classmethod
    def from_declared_inputs(
        cls,
        declared: Iterable[tuple[str, str, str] | object],
        *,
        consumer_group: str,
        exchange: str = "binance",
    ) -> "LiveKlineSubscription":
        """Build a multi-interval subscription from declared strategy inputs.

        Accepts any iterable whose items are either ``(market, symbol, interval)``
        tuples OR objects with ``.market`` / ``.symbol`` / ``.interval``
        attributes (``strategy_service.inputs.StrategyInput``). Each declared
        input becomes a single ``(symbol, market, interval)`` key in
        ``allowed_inputs``; ``topics`` automatically covers every distinct
        ``(market, interval)`` pair.
        """
        triples: set[tuple[str, str, str]] = set()
        for item in declared:
            if hasattr(item, "market") and hasattr(item, "symbol") and hasattr(item, "interval"):
                market = getattr(item, "market")
                symbol = getattr(item, "symbol")
                interval = getattr(item, "interval")
            else:
                try:
                    market, symbol, interval = item  # type: ignore[misc]
                except (TypeError, ValueError) as e:
                    raise ValueError(
                        f"from_declared_inputs: unsupported item shape {item!r}"
                    ) from e
            triples.add(
                (
                    _normalize_symbol(symbol),
                    _normalize_market(market),
                    _normalize_interval(interval),
                )
            )
        if not triples:
            raise ValueError(
                "from_declared_inputs: at least one declared input is required"
            )
        sorted_triples = sorted(triples)
        return cls(
            exchange=exchange,
            consumer_group=consumer_group,
            allowed_inputs=frozenset(sorted_triples),
        )

    @property
    def topics(self) -> list[str]:
        seen: set[tuple[str, str]] = set()
        topics: list[str] = []
        for _, market, interval in sorted(self.allowed_inputs):
            key = (market, interval)
            if key in seen:
                continue
            seen.add(key)
            topics.append(resolve_live_kline_topic(self.exchange, market, interval))
        return topics

    def matches(
        self,
        *,
        topic: str | None,
        symbol: str,
        market: str | None,
        interval: str | None,
    ) -> bool:
        parsed_topic = parse_live_kline_topic(topic)

        if parsed_topic is not None:
            topic_exchange, topic_market, topic_interval = parsed_topic
            if topic_exchange != self.exchange:
                return False
            resolved_market = topic_market
            resolved_interval = topic_interval
        else:
            if market is None or interval is None:
                return False
            resolved_market = _normalize_market(market)
            resolved_interval = _normalize_interval(interval)
        return (
            _normalize_symbol(symbol),
            resolved_market,
            resolved_interval,
        ) in self.allowed_inputs


@dataclass
class TimescaleConfig:
    """Configuration for TimescaleDB connection (market_data.timescale).

    database 字段支持年份模板：如 "binance_{year}" 会在连接时替换为实际年份。
    """
    host: str = _TIMESCALE_DEFAULTS["host"]
    port: int = _TIMESCALE_DEFAULTS["port"]
    database: str = _TIMESCALE_DEFAULTS["database"]
    user: str = _TIMESCALE_DEFAULTS["user"]
    password: str = _TIMESCALE_DEFAULTS["password"]
    pool_size: int = _TIMESCALE_DEFAULTS["pool_size"]
    max_overflow: int = _TIMESCALE_DEFAULTS["max_overflow"]

    def database_for_year(self, year: int) -> str:
        """返回特定年份的数据库名。如果 database 包含 {year} 模板则替换，否则原样返回。"""
        if "{year}" in self.database:
            return self.database.replace("{year}", str(year))
        return self.database

    @classmethod
    def from_dict(cls, data: dict) -> "TimescaleConfig":
        if not data:
            return cls()
        return cls(
            host=data.get("host", _TIMESCALE_DEFAULTS["host"]),
            port=data.get("port", _TIMESCALE_DEFAULTS["port"]),
            database=data.get("database", _TIMESCALE_DEFAULTS["database"]),
            user=data.get("user", _TIMESCALE_DEFAULTS["user"]),
            password=data.get("password", _TIMESCALE_DEFAULTS["password"]),
            pool_size=data.get("pool_size", _TIMESCALE_DEFAULTS["pool_size"]),
            max_overflow=data.get("max_overflow", _TIMESCALE_DEFAULTS["max_overflow"]),
        )


@dataclass
class KafkaBrokerConfig:
    """Configuration for a single Kafka broker."""
    host: str
    port: int

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @classmethod
    def from_dict(cls, data: dict) -> "KafkaBrokerConfig":
        return cls(
            host=data.get("host", "localhost"),
            port=data.get("port", 9092),
        )


@dataclass
class KafkaConfig:
    """Configuration for Kafka connection (market_data.kafka)."""
    brokers: List[KafkaBrokerConfig] = field(default_factory=list)
    consumer_group: str = "market-data-consumer"
    auto_offset_reset: str = "latest"
    enable_auto_commit: bool = True
    session_timeout_ms: int = 30000
    topics: List[str] = field(default_factory=lambda: list(DEFAULT_KAFKA_TOPICS))
    live_kline_subscription: Optional[LiveKlineSubscription] = None

    @classmethod
    def from_dict(cls, data: dict) -> "KafkaConfig":
        if not data:
            return cls()
        brokers = [
            KafkaBrokerConfig.from_dict(b)
            for b in data.get("brokers", [])
        ]
        if not brokers:
            brokers = [KafkaBrokerConfig(host="localhost", port=9092)]
        return cls(
            brokers=brokers,
            consumer_group=data.get("consumer_group", "market-data-consumer"),
            auto_offset_reset=data.get("auto_offset_reset", "latest"),
            enable_auto_commit=data.get("enable_auto_commit", True),
            session_timeout_ms=data.get("session_timeout_ms", 30000),
            topics=data.get("topics", list(DEFAULT_KAFKA_TOPICS)),
        )

    @classmethod
    def for_live_kline_subscription(
        cls,
        subscription: LiveKlineSubscription,
        *,
        brokers: Optional[Iterable[KafkaBrokerConfig | dict]] = None,
        auto_offset_reset: str = "latest",
        enable_auto_commit: bool = True,
        session_timeout_ms: int = 30000,
    ) -> "KafkaConfig":
        normalized_brokers: list[KafkaBrokerConfig] = []
        if brokers is not None:
            for broker in brokers:
                if isinstance(broker, KafkaBrokerConfig):
                    normalized_brokers.append(broker)
                else:
                    normalized_brokers.append(KafkaBrokerConfig.from_dict(broker))
        if not normalized_brokers:
            normalized_brokers = [KafkaBrokerConfig(host="localhost", port=9092)]
        return cls(
            brokers=normalized_brokers,
            consumer_group=subscription.consumer_group,
            auto_offset_reset=auto_offset_reset,
            enable_auto_commit=enable_auto_commit,
            session_timeout_ms=session_timeout_ms,
            topics=subscription.topics,
            live_kline_subscription=subscription,
        )

    @property
    def bootstrap_servers(self) -> str:
        return ",".join(b.address for b in self.brokers)
