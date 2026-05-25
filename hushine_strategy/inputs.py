from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from hushine_strategy.types import MarketData


@dataclass(frozen=True)
class StrategyInput:
    market: str
    symbol: str
    interval: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.market, self.symbol, self.interval)


def _normalize_key(market: Any, symbol: Any, interval: Any) -> tuple[str, str, str]:
    return (
        str(market).strip().lower(),
        str(symbol).strip().upper(),
        str(interval).strip(),
    )


class _IntervalAccessor:
    def __init__(self, values: dict[tuple[str, str, str], MarketData], market: str, symbol: str) -> None:
        self._values = values
        self._market = market
        self._symbol = symbol

    def __getitem__(self, interval: str) -> MarketData | None:
        return self._values.get((self._market, self._symbol, str(interval).strip()))


class _IntervalNode:
    def __init__(self, values: dict[tuple[str, str, str], MarketData], market: str, symbol: str) -> None:
        self.interval = _IntervalAccessor(values, market, symbol)


class _SymbolNode:
    def __init__(self, values: dict[tuple[str, str, str], MarketData], market: str) -> None:
        self._values = values
        self._market = market

    @property
    def symbol(self) -> "_SymbolNode":
        return self

    def __getitem__(self, symbol: str) -> _IntervalNode:
        return _IntervalNode(self._values, self._market, str(symbol).strip().upper())


class _MarketNode:
    def __init__(self, values: dict[tuple[str, str, str], MarketData]) -> None:
        self._values = values

    def __getitem__(self, market: str) -> _SymbolNode:
        return _SymbolNode(self._values, str(market).strip().lower())


class InputView:
    def __init__(self, inputs: Iterable[StrategyInput]) -> None:
        self._allowed = {_normalize_key(i.market, i.symbol, i.interval) for i in inputs}
        self._values: dict[tuple[str, str, str], MarketData] = {}
        self.market = _MarketNode(self._values)

    def update(self, tick: MarketData) -> bool:
        key = _normalize_key(tick.market, tick.symbol, tick.interval)
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
        if isinstance(item, dict):
            market = item.get("market")
            symbol = item.get("symbol")
            interval = item.get("interval")
        elif isinstance(item, (tuple, list)) and len(item) == 3:
            market, symbol, interval = item
        else:
            raise ValueError("each INPUTS item must be a dict or (market, symbol, interval)")
        if market is None or symbol is None or interval is None:
            raise ValueError("INPUTS market, symbol, and interval are required")
        normalized = StrategyInput(
            market=str(market).strip().lower(),
            symbol=str(symbol).strip().upper(),
            interval=str(interval).strip(),
        )
        if not normalized.market or not normalized.symbol or not normalized.interval:
            raise ValueError("INPUTS market, symbol, and interval are required")
        out.append(normalized)
    return out
