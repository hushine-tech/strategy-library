"""
market_data - Unified market data access layer for backtest and live trading.

This module provides:
- DataSource abstract interface for polymorphic market data access
- BacktestDataSource for TimescaleDB historical data queries
- LiveDataSource for Kafka real-time data subscription
- Python dataclass models for all market data types

Usage (Backtest):
    with BacktestDataSource() as ds:
        for kline in ds.get_klines("BTCUSDT", "1m", start_time, end_time):
            process(kline)

Usage (Live):
    # In this workspace, strategy-service consumes market_data directly from
    # the local strategy-library checkout rather than a separately published
    # package release.
    subscription = LiveKlineSubscription.from_symbols_with_market(
        [("BTCUSDT", "futures")],
        interval="1m",
        consumer_group="strategy-session-7-sess-123",
    )
    ds = LiveDataSource(config=KafkaConfig.for_live_kline_subscription(subscription))
    ds.on_kline(lambda k: print(k))
    ds.start()
"""
from .config import (
    TimescaleConfig,
    KafkaConfig,
    KafkaBrokerConfig,
    LiveKlineSubscription,
    resolve_live_kline_topic,
    parse_live_kline_topic,
)
from .models import (
    PriceLevel,
    MarketKline,
    MarketOI,
    MarketFunding,
    MarketOrderBook,
)
from .base import DataSource

__all__ = [
    "TimescaleConfig",
    "KafkaConfig", 
    "KafkaBrokerConfig",
    "LiveKlineSubscription",
    "resolve_live_kline_topic",
    "parse_live_kline_topic",
    "PriceLevel",
    "MarketKline",
    "MarketOI",
    "MarketFunding",
    "MarketOrderBook",
    "DataSource",
    "BacktestDataSource",
    "LiveDataSource",
]


def __getattr__(name: str):
    if name == "BacktestDataSource":
        from .backtest import BacktestDataSource

        return BacktestDataSource
    if name == "LiveDataSource":
        from .live import LiveDataSource

        return LiveDataSource
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
