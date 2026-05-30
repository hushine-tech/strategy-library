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
    order_type: str | None = None
    time_in_force: str | None = None


@dataclass(frozen=True)
class OrderFill:
    symbol: str
    side: str
    qty: float
    price: float
    fee: float = 0.0
    status: str = "FILLED"


@dataclass(frozen=True)
class OrderUpdateFill:
    symbol: str
    qty: float
    fill_price: float
    fee: float = 0.0
    fee_asset: str = ""
    fee_missing: bool = False
    exchange_trade_id: str = ""
    exchange_order_id: str = ""


@dataclass(frozen=True)
class OrderUpdateEvent:
    event_id: int
    session_id: str
    account_id: int
    venue_id: int
    exchange: str
    market: str
    side: str
    position_side: str
    event_type: str
    order_status: str
    intent_id: str = ""
    attempt_id: str = ""
    order_id: str = ""
    exchange_order_id: str = ""
    exchange_trade_id: str = ""
    fill: OrderUpdateFill | None = None
    orig_qty: float = 0.0
    executed_qty: float = 0.0
    remaining_qty: float = 0.0
    avg_price: float = 0.0


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
