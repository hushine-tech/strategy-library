from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from hushine_strategy.types import MarketData


@dataclass(frozen=True)
class StrategyInput:
    exchange: str
    market: str
    symbol: str
    interval: str

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.exchange, self.market, self.symbol, self.interval)


def _normalize_exchange(value: Any) -> str:
    exchange = str(value or "").strip().lower()
    if exchange not in {"binance", "okx"}:
        raise ValueError(f"unsupported exchange: {exchange or '<empty>'}")
    return exchange


def _normalize_market(value: Any) -> str:
    market = str(value or "").strip().lower()
    aliases = {
        "futures": "perpetual_futures",
        "usdm_futures": "perpetual_futures",
        "perp": "perpetual_futures",
    }
    market = aliases.get(market, market)
    if market not in {"spot", "perpetual_futures", "delivery_futures"}:
        raise ValueError(f"unsupported market: {market or '<empty>'}")
    return market


def _normalize_key(exchange: Any, market: Any, symbol: Any, interval: Any) -> tuple[str, str, str, str]:
    return (
        _normalize_exchange(exchange),
        _normalize_market(market),
        str(symbol).strip().upper(),
        str(interval).strip(),
    )


class _IntervalAccessor:
    def __init__(self, values: dict[tuple[str, str, str, str], MarketData], exchange: str, market: str, symbol: str) -> None:
        self._values = values
        self._exchange = exchange
        self._market = market
        self._symbol = symbol

    def __getitem__(self, interval: str) -> MarketData | None:
        return self._values.get((self._exchange, self._market, self._symbol, str(interval).strip()))


class _IntervalNode:
    def __init__(self, values: dict[tuple[str, str, str, str], MarketData], exchange: str, market: str, symbol: str) -> None:
        self.interval = _IntervalAccessor(values, exchange, market, symbol)


class _SymbolNode:
    def __init__(self, values: dict[tuple[str, str, str, str], MarketData], exchange: str, market: str) -> None:
        self._values = values
        self._exchange = exchange
        self._market = market

    @property
    def symbol(self) -> "_SymbolNode":
        return self

    def __getitem__(self, symbol: str) -> _IntervalNode:
        return _IntervalNode(self._values, self._exchange, self._market, str(symbol).strip().upper())


class _MarketNode:
    def __init__(self, values: dict[tuple[str, str, str, str], MarketData], exchange: str = "binance") -> None:
        self._values = values
        self._exchange = exchange

    def __getitem__(self, market: str) -> _SymbolNode:
        return _SymbolNode(self._values, self._exchange, _normalize_market(market))


class _ExchangeNode:
    def __init__(self, values: dict[tuple[str, str, str, str], MarketData]) -> None:
        self._values = values

    def __getitem__(self, exchange: str) -> _MarketNode:
        return _MarketNode(self._values, _normalize_exchange(exchange))


class InputView:
    def __init__(self, inputs: Iterable[StrategyInput]) -> None:
        self._allowed = {_normalize_key(i.exchange, i.market, i.symbol, i.interval) for i in inputs}
        self._values: dict[tuple[str, str, str, str], MarketData] = {}
        self.exchange = _ExchangeNode(self._values)
        self.market = _MarketNode(self._values)

    def update(self, tick: MarketData) -> bool:
        key = _normalize_key(getattr(tick, "exchange", "binance"), tick.market, tick.symbol, tick.interval)
        if key not in self._allowed:
            return False
        self._values[key] = tick
        return True


def parse_declared_inputs(raw: Any) -> list[StrategyInput]:
    if raw is None:
        raise ValueError("INPUTS must declare at least one stream")
    items = list(raw)
    if not items:
        raise ValueError("INPUTS must declare at least one stream")
    out: list[StrategyInput] = []
    for item in items:
        if isinstance(item, StrategyInput):
            exchange, market, symbol, interval = item.exchange, item.market, item.symbol, item.interval
        elif isinstance(item, dict):
            exchange = item.get("exchange")
            market = item.get("market")
            symbol = item.get("symbol")
            interval = item.get("interval")
        else:
            raise ValueError("each INPUTS item must be a dict with exchange, market, symbol, and interval")
        if exchange is None or market is None or symbol is None or interval is None:
            raise ValueError("INPUTS exchange, market, symbol, and interval are required")
        exchange = str(exchange).strip()
        market = str(market).strip()
        symbol = str(symbol).strip()
        interval = str(interval).strip()
        if not exchange or not market or not symbol or not interval:
            raise ValueError("INPUTS exchange, market, symbol, and interval are required")
        normalized = StrategyInput(
            exchange=_normalize_exchange(exchange),
            market=_normalize_market(market),
            symbol=symbol.upper(),
            interval=interval,
        )
        out.append(normalized)
    return out
