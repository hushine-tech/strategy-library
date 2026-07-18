from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from hushine_strategy.types import Exchange, Market, MarketData


@dataclass(frozen=True)
class StrategyInput:
    exchange: str
    market: str
    symbol: str
    interval: str
    stream_id: str = ""
    kind: str = ""

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.exchange, self.market, self.symbol, self.interval)

    @property
    def stream_identity(self) -> tuple[str, str, str, str, str, str]:
        exchange, market, symbol, interval = _normalize_key(
            self.exchange,
            self.market,
            self.symbol,
            self.interval,
        )
        return (
            str(self.stream_id or "").strip(),
            exchange,
            market,
            str(self.kind or "").strip().lower(),
            symbol,
            interval,
        )


@dataclass(frozen=True)
class StrategyOrderTarget:
    exchange: str
    market: str
    symbol: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.exchange, self.market, self.symbol)


@dataclass(frozen=True)
class StrategyRiskControls:
    max_loss_close_pct: float | None = None


def _normalize_exchange(value: Any) -> str:
    exchange = str(value or "").strip().lower()
    if exchange not in {Exchange.BINANCE, Exchange.OKX}:
        raise ValueError(f"unsupported exchange: {exchange or '<empty>'}")
    return exchange


def _normalize_market(value: Any) -> str:
    market = str(value or "").strip().lower()
    if market not in {Market.SPOT, Market.PERPETUAL_FUTURES, Market.DELIVERY_FUTURES}:
        raise ValueError(f"unsupported market: {market or '<empty>'}")
    return market


def _normalize_key(exchange: Any, market: Any, symbol: Any, interval: Any) -> tuple[str, str, str, str]:
    return (
        _normalize_exchange(exchange),
        _normalize_market(market),
        str(symbol).strip().upper(),
        str(interval).strip(),
    )


def _normalize_stream_identity(
    stream_id: Any,
    exchange: Any,
    market: Any,
    kind: Any,
    symbol: Any,
    interval: Any,
) -> tuple[str, str, str, str, str, str]:
    route = _normalize_key(exchange, market, symbol, interval)
    return (
        str(stream_id or "").strip(),
        route[0],
        route[1],
        str(kind or "kline").strip().lower(),
        route[2],
        route[3],
    )


class _IntervalAccessor:
    def __init__(self, view: "InputView", exchange: str, market: str, symbol: str) -> None:
        self._view = view
        self._exchange = exchange
        self._market = market
        self._symbol = symbol

    def __getitem__(self, interval: str) -> MarketData | None:
        return self._view._legacy_route_value(
            (self._exchange, self._market, self._symbol, str(interval).strip())
        )


class _IntervalNode:
    def __init__(self, view: "InputView", exchange: str, market: str, symbol: str) -> None:
        self.interval = _IntervalAccessor(view, exchange, market, symbol)


class _SymbolNode:
    def __init__(self, view: "InputView", exchange: str, market: str) -> None:
        self._view = view
        self._exchange = exchange
        self._market = market

    @property
    def symbol(self) -> "_SymbolNode":
        return self

    def __getitem__(self, symbol: str) -> _IntervalNode:
        return _IntervalNode(self._view, self._exchange, self._market, str(symbol).strip().upper())


class _MarketNode:
    def __init__(self, view: "InputView", exchange: str = "binance") -> None:
        self._view = view
        self._exchange = exchange

    def __getitem__(self, market: str) -> _SymbolNode:
        return _SymbolNode(self._view, self._exchange, _normalize_market(market))


class _ExchangeNode:
    def __init__(self, view: "InputView") -> None:
        self._view = view

    def __getitem__(self, exchange: str) -> _MarketNode:
        return _MarketNode(self._view, _normalize_exchange(exchange))


class InputView:
    def __init__(self, inputs: Iterable[StrategyInput]) -> None:
        self._allowed = {item.stream_identity for item in inputs}
        self._values: dict[tuple[str, str, str, str, str, str], MarketData] = {}
        self._trigger: MarketData | None = None
        self.exchange = _ExchangeNode(self)

    @staticmethod
    def _route(identity: tuple[str, str, str, str, str, str]) -> tuple[str, str, str, str]:
        _stream_id, exchange, market, _kind, symbol, interval = identity
        return (exchange, market, symbol, interval)

    @staticmethod
    def _matches(
        declared: tuple[str, str, str, str, str, str],
        actual: tuple[str, str, str, str, str, str],
    ) -> bool:
        declared_stream, exchange, market, declared_kind, symbol, interval = declared
        stream_id, actual_exchange, actual_market, kind, actual_symbol, actual_interval = actual
        return (
            (exchange, market, symbol, interval)
            == (actual_exchange, actual_market, actual_symbol, actual_interval)
            and (not declared_stream or declared_stream == stream_id)
            and (not declared_kind or declared_kind == kind)
        )

    def update(self, tick: MarketData) -> bool:
        identity = _normalize_stream_identity(
            getattr(tick, "stream_id", ""),
            getattr(tick, "exchange", "binance"),
            tick.market,
            getattr(tick, "kind", "kline"),
            tick.symbol,
            tick.interval,
        )
        if not any(self._matches(declared, identity) for declared in self._allowed):
            return False
        self._values[identity] = tick
        self._trigger = tick
        return True

    @property
    def trigger(self) -> MarketData | None:
        return self._trigger

    def get_stream(
        self,
        stream_id: str,
        exchange: str,
        market: str,
        kind: str,
        symbol: str,
        interval: str,
    ) -> MarketData | None:
        """Return one exact declared stream without route-level collapsing."""
        identity = _normalize_stream_identity(
            stream_id,
            exchange,
            market,
            kind,
            symbol,
            interval,
        )
        if not any(self._matches(declared, identity) for declared in self._allowed):
            raise KeyError(f"full market-data stream is not declared: {identity!r}")
        return self._values.get(identity)

    def _legacy_route_value(self, route: tuple[str, str, str, str]) -> MarketData | None:
        declared = {
            identity
            for identity in self._allowed
            if self._route(identity) == route
        }
        if len(declared) > 1:
            raise KeyError(
                f"ambiguous market-data route {route!r}; use get_stream() with the full identity"
            )
        matches = [
            value
            for identity, value in self._values.items()
            if self._route(identity) == route
        ]
        if len(matches) > 1:
            raise KeyError(
                f"ambiguous market-data route {route!r}; use get_stream() with the full identity"
            )
        return matches[0] if matches else None


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
            stream_id, kind = item.stream_id, item.kind
        elif isinstance(item, dict):
            exchange = item.get("exchange")
            market = item.get("market")
            symbol = item.get("symbol")
            interval = item.get("interval")
            stream_id = item.get("stream_id", "")
            kind = item.get("kind", "")
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
            stream_id=str(stream_id or "").strip(),
            kind=str(kind or "").strip().lower(),
        )
        out.append(normalized)
    return out


def parse_order_targets(raw: Any) -> list[StrategyOrderTarget]:
    if raw is None:
        raise ValueError("ORDER_TARGETS must be declared, use [] for read-only strategies")
    out: list[StrategyOrderTarget] = []
    for item in list(raw):
        if isinstance(item, StrategyOrderTarget):
            exchange, market, symbol = item.exchange, item.market, item.symbol
        elif isinstance(item, dict):
            exchange = item.get("exchange")
            market = item.get("market")
            symbol = item.get("symbol")
        else:
            raise ValueError("each ORDER_TARGETS item must be a dict with exchange, market, and symbol")
        if exchange is None or market is None or symbol is None:
            raise ValueError("ORDER_TARGETS exchange, market, and symbol are required")
        exchange = str(exchange).strip()
        market = str(market).strip()
        symbol = str(symbol).strip()
        if not exchange or not market or not symbol:
            raise ValueError("ORDER_TARGETS exchange, market, and symbol are required")
        out.append(
            StrategyOrderTarget(
                exchange=_normalize_exchange(exchange),
                market=_normalize_market(market),
                symbol=symbol.upper(),
            )
        )
    return out


def parse_risk_controls(raw: Any) -> StrategyRiskControls:
    if raw is None:
        return StrategyRiskControls()
    if not isinstance(raw, dict):
        raise ValueError("RISK_CONTROLS must be a dict")
    value = raw.get("max_loss_close_pct")
    if value is None:
        return StrategyRiskControls()
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("RISK_CONTROLS.max_loss_close_pct must be a number") from exc
    if not (0.0 < parsed <= 1.0):
        raise ValueError("RISK_CONTROLS.max_loss_close_pct must satisfy 0 < value <= 1")
    return StrategyRiskControls(max_loss_close_pct=parsed)
