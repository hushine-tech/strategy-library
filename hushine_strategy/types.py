from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OrderDecision:
    symbol: str
    side: str
    qty: float
    price: float | None = None
    market: str | None = None
    exchange: str | None = None
    position_side: str | None = None


@dataclass(frozen=True)
class OrderFill:
    symbol: str
    side: str
    qty: float
    price: float
    fee: float = 0.0
    status: str = "FILLED"


@dataclass(frozen=True)
class MarketData:
    symbol: str
    price: float
    timestamp: Any
    exchange: str = "binance"
    market: str = "futures"
    interval: str = "1m"
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    klines: Any = None
    orderbook: Any = None
    oi: float | None = None
    funding_rate: float | None = None
