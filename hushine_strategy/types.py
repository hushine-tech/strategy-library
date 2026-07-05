from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class Exchange:
    BINANCE = "binance"
    OKX = "okx"


class Market:
    SPOT = "spot"
    PERPETUAL_FUTURES = "perpetual_futures"
    DELIVERY_FUTURES = "delivery_futures"


class OrderSide:
    BUY = "BUY"
    SELL = "SELL"


class OrderType:
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class PositionSide:
    BOTH = "BOTH"
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True)
class OrderDecision:
    exchange: str
    market: str
    symbol: str
    side: str
    qty: str
    order_type: str
    price: str | None = None
    position_side: str | None = None
    time_in_force: str | None = None
    post_only: bool = False
    good_till_date: Any | None = None
    reduce_only: bool = False


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
    portfolio_id: int
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
    event_source: str = ""
    symbol: str = ""


@dataclass(frozen=True)
class MarketData:
    symbol: str
    price: float
    timestamp: Any
    exchange: str = Exchange.BINANCE
    market: str = Market.PERPETUAL_FUTURES
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
