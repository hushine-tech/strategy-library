"""
Market data models as Python dataclasses.

These dataclasses represent market data types received from Kafka topics
or queried from TimescaleDB. Each model includes a `from_dict` class method
for parsing JSON/Kafka message payloads.
"""
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


@dataclass
class PriceLevel:
    """Represents a price level in an order book."""
    price: float
    quantity: float

    @classmethod
    def from_dict(cls, data: dict) -> "PriceLevel":
        return cls(
            price=float(data["price"]),
            quantity=float(data["quantity"]),
        )

    def to_dict(self) -> dict:
        return {"price": self.price, "quantity": self.quantity}


@dataclass
class MarketKline:
    """Represents a kline (candlestick) data point."""
    symbol: str
    interval: str
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: int
    market: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "MarketKline":
        required = ["symbol", "interval", "open_time", "close_time",
                    "open", "high", "low", "close", "volume", "timestamp"]
        for field_name in required:
            if field_name not in data:
                raise ValueError(f"Missing required field: {field_name}")
        return cls(
            symbol=str(data["symbol"]),
            interval=str(data["interval"]),
            open_time=int(data["open_time"]),
            close_time=int(data["close_time"]),
            open=float(data["open"]),
            high=float(data["high"]),
            low=float(data["low"]),
            close=float(data["close"]),
            volume=float(data["volume"]),
            timestamp=int(data["timestamp"]),
            market=(
                str(data["market"]).strip().lower()
                if data.get("market") is not None
                else None
            ),
        )

    def to_dict(self) -> dict:
        result = {
            "symbol": self.symbol,
            "interval": self.interval,
            "open_time": self.open_time,
            "close_time": self.close_time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "timestamp": self.timestamp,
        }
        if self.market is not None:
            result["market"] = self.market
        return result


@dataclass
class MarketOI:
    """Represents open interest data."""
    symbol: str
    open_interest: float
    period: str
    timestamp: int

    @classmethod
    def from_dict(cls, data: dict) -> "MarketOI":
        required = ["symbol", "open_interest", "period", "timestamp"]
        for field_name in required:
            if field_name not in data:
                raise ValueError(f"Missing required field: {field_name}")
        return cls(
            symbol=str(data["symbol"]),
            open_interest=float(data["open_interest"]),
            period=str(data["period"]),
            timestamp=int(data["timestamp"]),
        )

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "open_interest": self.open_interest,
            "period": self.period,
            "timestamp": self.timestamp,
        }


@dataclass
class MarketFunding:
    """Represents funding rate data."""
    symbol: str
    funding_rate: float
    mark_price: float
    next_funding_time: int
    timestamp: int

    @classmethod
    def from_dict(cls, data: dict) -> "MarketFunding":
        required = ["symbol", "funding_rate", "mark_price", "next_funding_time", "timestamp"]
        for field_name in required:
            if field_name not in data:
                raise ValueError(f"Missing required field: {field_name}")
        return cls(
            symbol=str(data["symbol"]),
            funding_rate=float(data["funding_rate"]),
            mark_price=float(data["mark_price"]),
            next_funding_time=int(data["next_funding_time"]),
            timestamp=int(data["timestamp"]),
        )

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "funding_rate": self.funding_rate,
            "mark_price": self.mark_price,
            "next_funding_time": self.next_funding_time,
            "timestamp": self.timestamp,
        }


@dataclass
class MarketOrderBook:
    """Represents an order book snapshot."""
    symbol: str
    bids: List[PriceLevel] = field(default_factory=list)
    asks: List[PriceLevel] = field(default_factory=list)
    timestamp: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> "MarketOrderBook":
        required = ["symbol", "bids", "asks", "timestamp"]
        for field_name in required:
            if field_name not in data:
                raise ValueError(f"Missing required field: {field_name}")

        bids = [
            PriceLevel.from_dict(b) if isinstance(b, dict) else b
            for b in data["bids"]
        ]
        asks = [
            PriceLevel.from_dict(a) if isinstance(a, dict) else a
            for a in data["asks"]
        ]

        return cls(
            symbol=str(data["symbol"]),
            bids=bids,
            asks=asks,
            timestamp=int(data["timestamp"]),
        )

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "bids": [b.to_dict() if isinstance(b, PriceLevel) else b for b in self.bids],
            "asks": [a.to_dict() if isinstance(a, PriceLevel) else a for a in self.asks],
            "timestamp": self.timestamp,
        }
