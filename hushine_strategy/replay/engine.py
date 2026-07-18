from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from itertools import count
import sys
import types
from dataclasses import dataclass, field
from inspect import getmodule
from types import SimpleNamespace
from typing import Any, Iterable, Mapping

from hushine_strategy.inputs import (
    InputView,
    StrategyInput,
    StrategyOrderTarget,
    _normalize_exchange,
    _normalize_market,
    parse_declared_inputs,
    parse_order_targets,
)
from hushine_strategy.notifier import LocalNotifier
from hushine_strategy.types import Exchange, Market, MarketData, OrderDecision
from hushine_strategy.validator import ALLOWED_IMPORT_ROOTS, FORBIDDEN_IMPORT_ROOTS, validate_strategy_code
from hushine_strategy.wallet.futures import FuturesWallet
from hushine_strategy.wallet.portfolio import PortfolioWallet
from hushine_strategy.wallet.spot import (
    SpotFilterViolation,
    SpotSymbolMetadata,
    SpotWallet,
)
from hushine_strategy.replay.spot_filters import evaluate


_BLOCKED_SAFE_MODULE_NAMES = frozenset(
    {"__builtins__", "__dict__", "_logical_name", "_module"}
)


def _root(name: str) -> str:
    return str(name).split(".", 1)[0]


def _is_registered_logical_alias(
    module: types.ModuleType,
    logical_name: str,
) -> bool:
    logical_root = _root(logical_name)
    return (
        bool(logical_name)
        and logical_root in ALLOWED_IMPORT_ROOTS
        and logical_root not in FORBIDDEN_IMPORT_ROOTS
        and sys.modules.get(logical_name) is module
    )


def _is_forbidden_module(
    module: types.ModuleType,
    logical_name: str = "",
) -> bool:
    root = _root(module.__name__)
    if root in FORBIDDEN_IMPORT_ROOTS:
        return True
    return (
        root not in ALLOWED_IMPORT_ROOTS
        and not _is_registered_logical_alias(module, logical_name)
    )


def _value_module_root(value) -> str:
    module_name = getattr(value, "__module__", "") or ""
    if not module_name:
        module = getmodule(value)
        module_name = getattr(module, "__name__", "") if module else ""
    return _root(module_name) if module_name else ""


def _is_forbidden_export(value, logical_name: str = "") -> bool:
    if isinstance(value, types.ModuleType):
        return _is_forbidden_module(value, logical_name)
    root = _value_module_root(value)
    return bool(root) and root in FORBIDDEN_IMPORT_ROOTS


def _safe_star_names(
    module: types.ModuleType,
    logical_name: str,
) -> tuple[str, ...]:
    try:
        declared_names = getattr(module, "__all__")
    except AttributeError:
        has_declared_names = False
        candidate_names = sorted(
            name for name in vars(module) if not name.startswith("_")
        )
    else:
        has_declared_names = True
        candidate_names = tuple(declared_names)

    safe_names: list[str] = []
    for name in candidate_names:
        if (
            not isinstance(name, str)
            or name in _BLOCKED_SAFE_MODULE_NAMES
            or (not has_declared_names and name.startswith("_"))
        ):
            continue
        try:
            value = getattr(module, name)
            _safe_import_value(
                value,
                logical_name=f"{logical_name}.{name}",
            )
        except (AttributeError, ImportError):
            continue
        safe_names.append(name)
    return tuple(safe_names)


class _SafeModule:
    __slots__ = ("_logical_name", "_module")

    def __init__(
        self,
        module: types.ModuleType,
        *,
        logical_name: str,
    ) -> None:
        object.__setattr__(self, "_logical_name", logical_name)
        object.__setattr__(self, "_module", module)

    def __getattribute__(self, name: str):
        if name in {"__name__", "__package__", "__doc__"}:
            module = object.__getattribute__(self, "_module")
            return getattr(module, name)
        if name == "__all__":
            module = object.__getattribute__(self, "_module")
            logical_name = object.__getattribute__(self, "_logical_name")
            return _safe_star_names(module, logical_name)
        if name in _BLOCKED_SAFE_MODULE_NAMES:
            raise AttributeError(f"module attribute {name} is not available in replay strategy code")
        module = object.__getattribute__(self, "_module")
        logical_name = object.__getattribute__(self, "_logical_name")
        if (
            name.startswith("_")
            and name not in _safe_star_names(module, logical_name)
        ):
            raise AttributeError(f"module attribute {name} is not available in replay strategy code")
        child_logical_name = f"{logical_name}.{name}"
        try:
            value = getattr(module, name)
        except AttributeError:
            value = sys.modules.get(child_logical_name)
            if not isinstance(value, types.ModuleType):
                raise
        return _safe_import_value(
            value,
            logical_name=child_logical_name,
        )

    def __repr__(self) -> str:
        module = object.__getattribute__(self, "_module")
        return f"<safe module {module.__name__!r}>"


def _safe_import_value(value, *, logical_name: str = ""):
    if isinstance(value, types.ModuleType):
        if _is_forbidden_module(value, logical_name):
            raise ImportError(f"import {value.__name__} is not allowed in replay strategy code")
        return _SafeModule(
            value,
            logical_name=logical_name or value.__name__,
        )
    if _is_forbidden_export(value, logical_name):
        module_name = getattr(value, "__module__", "")
        raise ImportError(f"import from {module_name} is not allowed in replay strategy code")
    return value


def _strategy_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = _root(name)
    if level != 0 or root in FORBIDDEN_IMPORT_ROOTS or root not in ALLOWED_IMPORT_ROOTS:
        raise ImportError(f"import {name} is not allowed in replay strategy code")
    module = __import__(name, globals, locals, fromlist, level)
    for item in fromlist or ():
        if item == "*":
            continue
        if _root(item) in FORBIDDEN_IMPORT_ROOTS:
            raise ImportError(f"from {name} import {item} is not allowed in replay strategy code")
        value = getattr(module, item, None)
        if _is_forbidden_export(value, f"{name}.{item}"):
            raise ImportError(f"from {name} import {item} is not allowed in replay strategy code")
    logical_name = name if fromlist else module.__name__
    return _safe_import_value(module, logical_name=logical_name)


_SAFE_BUILTINS = {
    "__build_class__": __build_class__,
    "__import__": _strategy_import,
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "float": float,
    "getattr": getattr,
    "int": int,
    "isinstance": isinstance,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "range": range,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
}


@dataclass
class ReplayConfig:
    strategy_code: str
    ticks: Iterable[MarketData]
    wallet: FuturesWallet | PortfolioWallet
    strategy_path: str = "strategy.py"
    notifier: LocalNotifier | None = None
    metadata: Mapping[tuple[int, str, str, str], SpotSymbolMetadata] = field(default_factory=dict)
    risk_facts: Mapping[tuple[int, str, str, str], Mapping[str, Any]] = field(default_factory=dict)
    default_fee_rate: Any = "0.0004"
    slippage_bps: Any = "0"


@dataclass(frozen=True)
class ReplayResult:
    bars_processed: int
    orders_filled: int


class ReplayEngine:
    """Offline dispatcher and fill engine for declared mixed-market routes."""

    def __init__(
        self,
        *,
        wallet: PortfolioWallet,
        metadata: Mapping[tuple[int, str, str, str], SpotSymbolMetadata] | None = None,
        declared_inputs: Iterable[StrategyInput] = (),
        order_targets: Iterable[StrategyOrderTarget] = (),
        risk_facts: Mapping[tuple[int, str, str, str], Mapping[str, Any]] | None = None,
        default_fee_rate: Any = "0.0004",
        slippage_bps: Any = "0",
    ) -> None:
        if not isinstance(wallet, PortfolioWallet):
            raise TypeError("ReplayEngine requires a PortfolioWallet")
        self.wallet = wallet
        self.declared_inputs = tuple(declared_inputs)
        self.order_target_keys = {
            (
                _normalize_exchange(item.exchange),
                _normalize_market(item.market),
                str(item.symbol).strip().upper(),
            )
            for item in order_targets
        }
        self.metadata: dict[tuple[int, str, str, str], SpotSymbolMetadata] = {}
        for raw_key, item in (metadata or {}).items():
            if not isinstance(item, SpotSymbolMetadata):
                raise TypeError("ReplayEngine metadata must contain SpotSymbolMetadata values")
            if self._normalize_metadata_key(raw_key) != item.route_key:
                raise ValueError("Spot metadata key does not match its immutable route")
            if item.route_key in self.metadata:
                raise ValueError(f"duplicate Spot metadata route: {item.route_key!r}")
            self.metadata[item.route_key] = item
        self.risk_facts = {
            self._normalize_metadata_key(key): deepcopy(dict(value))
            for key, value in (risk_facts or {}).items()
        }
        self.default_fee_rate = self._nonnegative_decimal(
            default_fee_rate,
            "default fee rate",
        )
        self.slippage_bps = self._nonnegative_decimal(slippage_bps, "slippage bps")
        if self.slippage_bps >= Decimal("10000"):
            raise ValueError("slippage bps must be less than 10000")
        self._prices: dict[tuple[str, str, str, str, str, str], Decimal] = {}
        self._route_prices: dict[tuple[str, str, str], Decimal] = {}
        self._order_ids = count(1)
        for item in self.metadata.values():
            route_wallet = self.wallet.get(
                item.exchange,
                item.market,
                venue_id=item.venue_id,
            )
            if not isinstance(route_wallet, SpotWallet):
                raise TypeError("Spot metadata route must reference a SpotWallet")
            route_wallet.register_metadata(item)
        required_spot_symbols = {
            (_normalize_exchange(item.exchange), str(item.symbol).strip().upper())
            for item in self.declared_inputs
            if _normalize_market(item.market) == Market.SPOT
        }
        required_spot_symbols.update(
            (exchange, symbol)
            for exchange, market, symbol in self.order_target_keys
            if market == Market.SPOT
        )
        for exchange, symbol in sorted(required_spot_symbols):
            self._spot_metadata(exchange, symbol)

    @staticmethod
    def _normalize_metadata_key(key: Any) -> tuple[int, str, str, str]:
        if not isinstance(key, tuple) or len(key) != 4:
            raise ValueError("Spot metadata/risk key must be (venue_id, exchange, market, symbol)")
        venue_id, exchange, market, symbol = key
        return (
            int(venue_id),
            _normalize_exchange(exchange),
            _normalize_market(market),
            str(symbol).strip().upper(),
        )

    @staticmethod
    def stream_identity(tick: MarketData) -> tuple[str, str, str, str, str, str]:
        return (
            str(getattr(tick, "stream_id", "") or "").strip(),
            _normalize_exchange(getattr(tick, "exchange", "binance")),
            _normalize_market(getattr(tick, "market", "")),
            str(getattr(tick, "kind", "kline") or "kline").strip().lower(),
            str(getattr(tick, "symbol", "") or "").strip().upper(),
            str(getattr(tick, "interval", "") or "").strip(),
        )

    @staticmethod
    def _decimal(value: Any, field_name: str) -> Decimal:
        try:
            parsed = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be a decimal") from exc
        if not parsed.is_finite() or parsed <= 0:
            raise ValueError(f"{field_name} must be a positive finite decimal")
        return parsed

    @staticmethod
    def _nonnegative_decimal(value: Any, field_name: str) -> Decimal:
        try:
            parsed = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be a decimal") from exc
        if not parsed.is_finite() or parsed < 0:
            raise ValueError(f"{field_name} must be a non-negative finite decimal")
        return parsed

    def _input_is_declared(self, identity: tuple[str, str, str, str, str, str]) -> bool:
        stream_id, exchange, market, kind, symbol, interval = identity
        for item in self.declared_inputs:
            if (
                _normalize_exchange(item.exchange),
                _normalize_market(item.market),
                str(item.symbol).strip().upper(),
                str(item.interval).strip(),
            ) != (exchange, market, symbol, interval):
                continue
            declared_stream = str(getattr(item, "stream_id", "") or "").strip()
            declared_kind = str(getattr(item, "kind", "") or "").strip().lower()
            if declared_stream and declared_stream != stream_id:
                continue
            if declared_kind and declared_kind != kind:
                continue
            return True
        return False

    def _spot_metadata(self, exchange: str, symbol: str) -> SpotSymbolMetadata:
        matches = [
            item
            for item in self.metadata.values()
            if item.exchange == exchange and item.market == "spot" and item.symbol == symbol
        ]
        if len(matches) != 1:
            reason = "missing" if not matches else "ambiguous"
            raise ValueError(f"{reason} immutable Spot metadata for {exchange}/spot/{symbol}")
        return matches[0]

    def push_market_data(self, tick: MarketData) -> bool:
        identity = self.stream_identity(tick)
        if not self._input_is_declared(identity):
            return False
        _stream_id, exchange, market, _kind, symbol, _interval = identity
        price = self._decimal(getattr(tick, "price", None), "market price")
        if market == Market.SPOT:
            metadata = self._spot_metadata(exchange, symbol)
            route_wallet = self.wallet.get(exchange, market, venue_id=metadata.venue_id)
            if not isinstance(route_wallet, SpotWallet):
                raise TypeError("Spot market data route must reference a SpotWallet")
            route_wallet.update_price(symbol, price, metadata)
        elif market == Market.PERPETUAL_FUTURES:
            route_wallet = self.wallet.get(exchange, market)
            if not isinstance(route_wallet, FuturesWallet):
                raise TypeError("Futures market data route must reference a FuturesWallet")
            route_wallet.update_mark_price(symbol, float(price))
        else:
            raise ValueError(f"offline replay does not support market {market}")
        self._prices[identity] = price
        self._route_prices[(exchange, market, symbol)] = price
        return True

    def last_price(self, identity: tuple[str, str, str, str, str, str]) -> Decimal | None:
        return self._prices.get(identity)

    @staticmethod
    def _open_order_facts(wallet: SpotWallet) -> list[dict[str, str]]:
        return [
            {
                "symbol": order.symbol,
                "side": order.side,
                "orig_qty_decimal": str(order.orig_qty),
                "executed_qty_decimal": str(order.executed_qty),
            }
            for order in wallet.open_orders.values()
        ]

    def execute_order(
        self,
        decision: OrderDecision,
        *,
        mark_price: Any | None = None,
    ) -> bool:
        try:
            exchange = _normalize_exchange(decision.exchange)
            market = _normalize_market(decision.market)
        except ValueError as exc:
            raise ValueError(f"offline replay does not support order market {decision.market}") from exc
        symbol = str(decision.symbol or "").strip().upper()
        if exchange != Exchange.BINANCE:
            raise ValueError(f"offline replay does not support order exchange {exchange}")
        target_key = (exchange, market, symbol)
        if target_key not in self.order_target_keys:
            raise ValueError(f"order target {target_key} is not declared in ORDER_TARGETS")

        resolved_mark_price = mark_price
        if resolved_mark_price is None:
            resolved_mark_price = self._route_prices.get(target_key)

        if market == Market.PERPETUAL_FUTURES:
            route_wallet = self.wallet.get(exchange, market)
            if not isinstance(route_wallet, FuturesWallet):
                raise TypeError("Futures order route must reference a FuturesWallet")
            if decision.price is None and resolved_mark_price is None:
                raise ValueError(f"missing replay price for order target {target_key}")
            fill_price = self._decimal(decision.price or resolved_mark_price, "fill price")
            route_wallet.fill_order(decision, float(fill_price))
            return True
        if market != Market.SPOT:
            raise ValueError(f"offline replay does not support order market {market}")

        metadata = self._spot_metadata(exchange, symbol)
        route_wallet = self.wallet.get(exchange, market, venue_id=metadata.venue_id)
        if not isinstance(route_wallet, SpotWallet):
            raise TypeError("Spot order route must reference a SpotWallet")
        order_type = str(decision.order_type or "").strip().upper()
        if metadata.status != "TRADING":
            raise SpotFilterViolation("SPOT_SYMBOL_NOT_TRADING")
        if not metadata.spot_trading_allowed:
            raise SpotFilterViolation("SPOT_TRADING_DISABLED")
        if order_type not in {"LIMIT", "MARKET"} or (
            metadata.order_types and order_type not in metadata.order_types
        ):
            raise SpotFilterViolation("SPOT_ORDER_TYPE_UNSUPPORTED")
        immutable_facts = deepcopy(self.risk_facts.get(metadata.route_key) or {})
        if not immutable_facts:
            raise SpotFilterViolation("SPOT_RISK_FACTS_UNAVAILABLE")
        if resolved_mark_price is None:
            resolved_mark_price = immutable_facts.get("reference_price_decimal")
        if resolved_mark_price is None:
            raise SpotFilterViolation("SPOT_REFERENCE_PRICE_UNAVAILABLE")
        if immutable_facts.get("reference_price_source") == "replay_event_close":
            immutable_facts["reference_price_decimal"] = str(resolved_mark_price)
        immutable_facts["metadata"] = metadata.filter_facts()
        code = evaluate(
            decision,
            immutable_facts,
            route_wallet,
            self._open_order_facts(route_wallet),
        )
        if code:
            raise SpotFilterViolation(code)

        fill_price = self._decimal(decision.price or resolved_mark_price, "fill price")
        if order_type == "MARKET":
            slippage = self.slippage_bps / Decimal("10000")
            if str(decision.side).strip().upper() == "BUY":
                fill_price *= Decimal("1") + slippage
            else:
                fill_price *= Decimal("1") - slippage
            fill_price = fill_price.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
        qty = self._decimal(decision.qty, "qty")
        quote_qty = qty * fill_price
        fee = quote_qty * self.default_fee_rate
        sequence = next(self._order_ids)
        identity = f"offline-order-{sequence}"
        route_wallet.apply_order_update(SimpleNamespace(
            venue_id=metadata.venue_id,
            exchange=metadata.exchange,
            market=metadata.market,
            symbol=metadata.symbol,
            side=str(decision.side).strip().upper(),
            status="FILLED",
            order_id=identity,
            exchange_order_id=identity,
            exchange_trade_id=f"offline-trade-{sequence}",
            qty_decimal=str(qty),
            fill_price_decimal=str(fill_price),
            quote_qty_decimal=str(quote_qty),
            fee_decimal=str(fee),
            fee_asset=metadata.quote_asset,
            orig_qty_decimal=str(qty),
            executed_qty_decimal=str(qty),
            remaining_qty_decimal="0",
            price_decimal=str(fill_price),
            cumulative_quote_qty_decimal=str(quote_qty),
        ), metadata)
        return True


def _load_strategy(code: str, strategy_path: str):
    result = validate_strategy_code(code)
    if not result.ok:
        first = result.issues[0]
        raise ValueError(f"strategy validation failed: {first.code}: {first.message}")
    ns: dict = {"__builtins__": _SAFE_BUILTINS, "__name__": "__hushine_replay_strategy__"}
    compiled = compile(code, strategy_path, "exec")
    try:
        exec(compiled, ns)
    except ImportError as exc:
        raise ValueError(f"strategy import failed: {exc}") from exc
    strategy_cls = ns["MyStrategy"]
    return strategy_cls()


def run_replay(config: ReplayConfig) -> ReplayResult:
    strategy = _load_strategy(config.strategy_code, config.strategy_path)
    setattr(strategy, "notify", config.notifier or LocalNotifier())
    inputs = parse_declared_inputs(getattr(strategy, "INPUTS", None))
    order_targets = parse_order_targets(getattr(strategy, "ORDER_TARGETS", None))
    if isinstance(config.wallet, PortfolioWallet):
        portfolio_wallet = config.wallet
    elif isinstance(config.wallet, FuturesWallet):
        routes = {
            (_normalize_exchange(item.exchange), _normalize_market(item.market))
            for item in (*inputs, *order_targets)
        }
        if any(market != Market.PERPETUAL_FUTURES for _exchange, market in routes):
            raise ValueError("PortfolioWallet is required for Spot replay")
        portfolio_wallet = PortfolioWallet(
            allowed_routes=routes,
            wallets={
                (exchange, market, index): config.wallet
                for index, (exchange, market) in enumerate(sorted(routes), start=1)
            },
        )
    else:
        raise TypeError("ReplayConfig.wallet must be FuturesWallet or PortfolioWallet")
    engine = ReplayEngine(
        wallet=portfolio_wallet,
        metadata=config.metadata,
        declared_inputs=inputs,
        order_targets=order_targets,
        risk_facts=config.risk_facts,
        default_fee_rate=config.default_fee_rate,
        slippage_bps=config.slippage_bps,
    )
    view = InputView(inputs)
    bars = 0
    orders = 0
    for tick in config.ticks:
        if not engine.push_market_data(tick):
            continue
        if not view.update(tick):
            raise RuntimeError("replay input dispatch disagreed with strategy InputView")
        decision = strategy.on_market_data(view, config.wallet)
        bars += 1
        if isinstance(decision, OrderDecision):
            if not engine.order_target_keys:
                raise ValueError("ORDER_TARGETS is empty; strategy cannot return OrderDecision")
            engine.execute_order(decision)
            orders += 1
    return ReplayResult(bars_processed=bars, orders_filled=orders)
