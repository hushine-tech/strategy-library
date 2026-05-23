"""
Abstract DataSource interface for market data retrieval.

Provides a unified interface for both backtest (TimescaleDB) and
live (Kafka) data sources, allowing polymorphic usage.
"""
from abc import ABC, abstractmethod
from typing import Generator, Optional

from .models import MarketKline, MarketOI, MarketFunding, MarketOrderBook


class DataSource(ABC):
    """Abstract base class for market data sources."""

    @abstractmethod
    def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        market: str = "futures",
    ) -> Generator[MarketKline, None, None]:
        """Fetch kline (candlestick) data.

        Args:
            symbol: Trading pair symbol (e.g., "BTCUSDT")
            interval: Kline interval (e.g., "1m", "5m", "1h", "4h", "1d")
            start_time: Start time as Unix timestamp in milliseconds
            end_time: End time as Unix timestamp in milliseconds
            market: Market type ("futures" or "spot")

        Yields:
            MarketKline instances
        """
        pass

    @abstractmethod
    def get_open_interest(
        self,
        symbol: str,
        start_time: int,
        end_time: int,
    ) -> Generator[MarketOI, None, None]:
        """Fetch open interest data.

        Args:
            symbol: Trading pair symbol (e.g., "BTCUSDT")
            start_time: Start time as Unix timestamp in milliseconds
            end_time: End time as Unix timestamp in milliseconds

        Yields:
            MarketOI instances
        """
        pass

    @abstractmethod
    def get_funding_rates(
        self,
        symbol: str,
        start_time: int,
        end_time: int,
    ) -> Generator[MarketFunding, None, None]:
        """Fetch funding rate data.

        Args:
            symbol: Trading pair symbol (e.g., "BTCUSDT")
            start_time: Start time as Unix timestamp in milliseconds
            end_time: End time as Unix timestamp in milliseconds

        Yields:
            MarketFunding instances
        """
        pass

    @abstractmethod
    def get_orderbook(
        self,
        symbol: str,
        start_time: int,
        end_time: int,
        market: str = "futures",
    ) -> Generator[MarketOrderBook, None, None]:
        """Fetch orderbook snapshot data.

        Args:
            symbol: Trading pair symbol (e.g., "BTCUSDT")
            start_time: Start time as Unix timestamp in milliseconds
            end_time: End time as Unix timestamp in milliseconds
            market: Market type ("futures" or "spot")

        Yields:
            MarketOrderBook instances
        """
        pass