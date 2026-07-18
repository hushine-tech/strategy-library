"""Offline Binance Spot wallet using canonical asset balances and exact decimals."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Mapping


ZERO = Decimal("0")
_ACTIVE_STATUSES = frozenset({"NEW", "PARTIALLY_FILLED"})
_TERMINAL_STATUSES = frozenset({"FILLED", "CANCELED", "EXPIRED", "REJECTED"})
_SUPPORTED_STATUSES = _ACTIVE_STATUSES | _TERMINAL_STATUSES
_STATUS_RANK = {
    "": 0,
    "NEW": 1,
    "PARTIALLY_FILLED": 2,
    "FILLED": 3,
    "CANCELED": 3,
    "EXPIRED": 3,
    "REJECTED": 3,
}


def _asset_code(value: Any) -> str:
    asset = str(value or "").strip().upper()
    if not asset or not asset.isalnum():
        raise ValueError(f"invalid Binance asset code: {value!r}")
    return asset


def _exchange(value: Any) -> str:
    return str(value or "").strip().lower()


def _market(value: Any) -> str:
    return str(value or "").strip().lower()


def _symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a decimal") from exc
    if not parsed.is_finite() or parsed < ZERO:
        raise ValueError(f"{field_name} must be a non-negative finite decimal")
    return parsed


def _exact(update: Any, exact_name: str, legacy_name: str = "") -> Decimal:
    raw = getattr(update, exact_name, "")
    if raw not in (None, ""):
        return _decimal(raw, exact_name)
    if legacy_name:
        return _decimal(getattr(update, legacy_name, 0) or 0, legacy_name)
    return ZERO


def _filter_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    fields = getattr(item, "__dataclass_fields__", {})
    return {name: getattr(item, name) for name in fields}


@dataclass(slots=True)
class SpotAssetBalance:
    free: Decimal = ZERO
    locked: Decimal = ZERO
    avg_entry_price: Decimal = ZERO
    price: Decimal | None = None

    def __post_init__(self) -> None:
        self.free = _decimal(self.free, "free")
        self.locked = _decimal(self.locked, "locked")
        self.avg_entry_price = _decimal(self.avg_entry_price, "avg_entry_price")
        if self.price is not None:
            self.price = _decimal(self.price, "price")

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


@dataclass(frozen=True, slots=True)
class SpotSymbolMetadata:
    venue_id: int
    exchange: str
    market: str
    symbol: str
    status: str
    base_asset: str
    quote_asset: str
    base_asset_precision: int
    quote_asset_precision: int
    spot_trading_allowed: bool
    permission_sets: tuple[tuple[str, ...], ...] = ()
    order_types: tuple[str, ...] = ()
    filters: tuple[Any, ...] = ()
    snapshot_time_ms: int = 0

    def __post_init__(self) -> None:
        exchange = _exchange(self.exchange)
        market = _market(self.market)
        symbol = _symbol(self.symbol)
        base_asset = _asset_code(self.base_asset)
        quote_asset = _asset_code(self.quote_asset)
        if exchange != "binance" or market != "spot":
            raise ValueError("Spot metadata must describe a binance/spot route")
        if quote_asset != "USDT":
            raise ValueError("only Binance USDT Spot symbols are supported")
        if base_asset in {symbol, quote_asset} or symbol != f"{base_asset}{quote_asset}":
            raise ValueError("Spot metadata must keep symbol separate from Binance asset codes")
        if int(self.venue_id) <= 0:
            raise ValueError("Spot metadata venue_id must be positive")
        if int(self.base_asset_precision) < 0 or int(self.quote_asset_precision) < 0:
            raise ValueError("Spot asset precision must be non-negative")
        object.__setattr__(self, "venue_id", int(self.venue_id))
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(self, "market", market)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "base_asset", base_asset)
        object.__setattr__(self, "quote_asset", quote_asset)
        object.__setattr__(self, "status", str(self.status or "").strip().upper())
        object.__setattr__(self, "base_asset_precision", int(self.base_asset_precision))
        object.__setattr__(self, "quote_asset_precision", int(self.quote_asset_precision))
        object.__setattr__(
            self,
            "permission_sets",
            tuple(tuple(str(value).strip().upper() for value in group) for group in self.permission_sets),
        )
        object.__setattr__(
            self,
            "order_types",
            tuple(str(value).strip().upper() for value in self.order_types),
        )
        object.__setattr__(
            self,
            "filters",
            tuple(MappingProxyType(deepcopy(_filter_dict(item))) for item in self.filters),
        )

    @property
    def route_key(self) -> tuple[int, str, str, str]:
        return (self.venue_id, self.exchange, self.market, self.symbol)

    def filter_facts(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "base_asset": self.base_asset,
            "quote_asset": self.quote_asset,
            "base_asset_precision": self.base_asset_precision,
            "quote_asset_precision": self.quote_asset_precision,
            "spot_trading_allowed": self.spot_trading_allowed,
            "permission_sets": [list(group) for group in self.permission_sets],
            "order_types": list(self.order_types),
            "filters": [_filter_dict(item) for item in self.filters],
        }


@dataclass(slots=True)
class SpotOpenOrder:
    route_key: tuple[int, str, str, str]
    order_identity: str
    side: str
    status: str
    orig_qty: Decimal = ZERO
    executed_qty: Decimal = ZERO
    remaining_qty: Decimal = ZERO
    cumulative_quote_qty: Decimal = ZERO
    price: Decimal = ZERO
    locked_quote: Decimal = ZERO
    locked_base: Decimal = ZERO

    @property
    def symbol(self) -> str:
        return self.route_key[3]


@dataclass(frozen=True, slots=True)
class _OrderState:
    executed_qty: Decimal
    cumulative_quote_qty: Decimal
    status: str


class SpotFilterViolation(ValueError):
    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(f"{self.code}: offline Spot order rejected")


@dataclass
class SpotWallet:
    assets: dict[str, SpotAssetBalance] = field(default_factory=dict)
    metadata: dict[tuple[int, str, str, str], SpotSymbolMetadata] = field(default_factory=dict)
    symbol_prices: dict[tuple[int, str, str, str], Decimal] = field(default_factory=dict)
    open_orders: dict[tuple[int, str, str, str, str], SpotOpenOrder] = field(default_factory=dict)
    order_states: dict[tuple[int, str, str, str, str], _OrderState] = field(default_factory=dict)
    applied_trade_ids: set[tuple[int, str, str, str, str, str]] = field(default_factory=set)
    recovery_pending_orders: set[tuple[int, str, str, str, str]] = field(default_factory=set)

    def __post_init__(self) -> None:
        normalized: dict[str, SpotAssetBalance] = {}
        for raw_asset, balance in self.assets.items():
            if not isinstance(balance, SpotAssetBalance):
                raise TypeError("SpotWallet assets must contain SpotAssetBalance values")
            asset = _asset_code(raw_asset)
            if asset in normalized:
                raise ValueError(f"duplicate normalized Spot asset: {asset}")
            normalized[asset] = balance
        normalized.setdefault("USDT", SpotAssetBalance())
        self.assets = normalized

    @classmethod
    def from_assets(
        cls,
        assets: Mapping[str, tuple[Any, Any] | SpotAssetBalance],
    ) -> "SpotWallet":
        normalized: dict[str, SpotAssetBalance] = {}
        for raw_asset, value in assets.items():
            asset = _asset_code(raw_asset)
            if asset in normalized:
                raise ValueError(f"duplicate normalized Spot asset: {asset}")
            if isinstance(value, SpotAssetBalance):
                normalized[asset] = value
            else:
                free, locked = value
                normalized[asset] = SpotAssetBalance(free=free, locked=locked)
        normalized.setdefault("USDT", SpotAssetBalance())
        return cls(assets=normalized)

    def register_metadata(self, metadata: SpotSymbolMetadata) -> SpotSymbolMetadata:
        if not isinstance(metadata, SpotSymbolMetadata):
            raise TypeError("SpotSymbolMetadata is required")
        if metadata.symbol in self.assets:
            raise ValueError(
                f"Spot trading symbol {metadata.symbol} cannot be stored as an account asset"
            )
        existing = self.metadata.get(metadata.route_key)
        if existing is not None and existing != metadata:
            raise ValueError(
                f"conflicting immutable Spot metadata for route {metadata.route_key!r}"
            )
        self.metadata[metadata.route_key] = metadata
        return metadata

    def metadata_for(
        self,
        symbol: str,
        metadata: SpotSymbolMetadata | None = None,
    ) -> SpotSymbolMetadata:
        normalized = _symbol(symbol)
        if metadata is not None:
            if metadata.symbol != normalized:
                raise ValueError("Spot metadata does not match the requested symbol")
            return self.register_metadata(metadata)
        matches = [item for item in self.metadata.values() if item.symbol == normalized]
        if len(matches) != 1:
            reason = "missing" if not matches else "ambiguous"
            raise ValueError(f"{reason} Spot metadata for {normalized}")
        return matches[0]

    def update_price(
        self,
        symbol: str,
        price: Any,
        metadata: SpotSymbolMetadata | None = None,
    ) -> None:
        facts = self.metadata_for(symbol, metadata)
        exact_price = _decimal(price, "price")
        if exact_price == ZERO:
            raise ValueError("Spot price must be positive")
        self.symbol_prices[facts.route_key] = exact_price
        balance = self.assets.get(facts.base_asset)
        if balance is not None:
            balance.price = exact_price

    @staticmethod
    def _debit(
        balances: dict[str, list[Decimal]],
        asset: str,
        amount: Decimal,
        *,
        prefer_locked: bool,
    ) -> None:
        if amount == ZERO:
            return
        if asset not in balances:
            raise ValueError(f"missing Spot asset balance for debit: {asset}")
        free, locked = balances[asset]
        if prefer_locked:
            from_locked = min(locked, amount)
            locked -= from_locked
            amount -= from_locked
        if free < amount:
            raise ValueError(f"insufficient Spot {asset} balance")
        balances[asset] = [free - amount, locked]

    def _apply_fill(
        self,
        *,
        metadata: SpotSymbolMetadata,
        side: str,
        qty: Decimal,
        quote_qty: Decimal,
        fee: Decimal,
        fee_asset: str,
        existing_order: SpotOpenOrder | None,
    ) -> None:
        planned = {
            asset: [balance.free, balance.locked]
            for asset, balance in self.assets.items()
        }
        planned.setdefault(metadata.base_asset, [ZERO, ZERO])
        planned.setdefault(metadata.quote_asset, [ZERO, ZERO])
        previous_base = self.assets.get(metadata.base_asset, SpotAssetBalance()).total
        if side == "BUY":
            self._debit(planned, metadata.quote_asset, quote_qty, prefer_locked=existing_order is not None)
            planned[metadata.base_asset][0] += qty
        else:
            self._debit(planned, metadata.base_asset, qty, prefer_locked=existing_order is not None)
            planned[metadata.quote_asset][0] += quote_qty
        if fee:
            self._debit(planned, fee_asset, fee, prefer_locked=False)
        for asset, (free, locked) in planned.items():
            balance = self.assets.setdefault(asset, SpotAssetBalance())
            balance.free = free
            balance.locked = locked
        base = self.assets[metadata.base_asset]
        if side == "BUY" and qty:
            gross_after = previous_base + qty
            fill_price = quote_qty / qty
            base.avg_entry_price = (
                fill_price
                if previous_base == ZERO
                else (base.avg_entry_price * previous_base + quote_qty) / gross_after
            )
        elif side == "SELL" and base.total == ZERO:
            base.avg_entry_price = ZERO

    def _release_locks(self, order: SpotOpenOrder, metadata: SpotSymbolMetadata) -> None:
        quote = self.assets[metadata.quote_asset]
        base = self.assets.setdefault(metadata.base_asset, SpotAssetBalance())
        quote_release = min(order.locked_quote, quote.locked)
        base_release = min(order.locked_base, base.locked)
        quote.locked -= quote_release
        quote.free += quote_release
        base.locked -= base_release
        base.free += base_release
        order.locked_quote -= quote_release
        order.locked_base -= base_release

    def _sync_locks(self, order: SpotOpenOrder, metadata: SpotSymbolMetadata) -> None:
        if order.status not in _ACTIVE_STATUSES:
            self._release_locks(order, metadata)
            return
        if order.side == "BUY":
            balance = self.assets[metadata.quote_asset]
            desired = order.remaining_qty * order.price
            current = order.locked_quote
        else:
            balance = self.assets.setdefault(metadata.base_asset, SpotAssetBalance())
            desired = order.remaining_qty
            current = order.locked_base
        delta = desired - current
        if delta > ZERO:
            if balance.free < delta:
                raise ValueError("insufficient Spot balance to lock open order")
            balance.free -= delta
            balance.locked += delta
        elif delta < ZERO:
            release = min(-delta, balance.locked)
            balance.locked -= release
            balance.free += release
        if order.side == "BUY":
            order.locked_quote = desired
        else:
            order.locked_base = desired

    def apply_order_update(
        self,
        update: Any,
        metadata: SpotSymbolMetadata | None = None,
    ) -> bool:
        facts = self.metadata_for(getattr(update, "symbol", ""), metadata)
        route = (
            int(getattr(update, "venue_id", 0) or facts.venue_id),
            _exchange(getattr(update, "exchange", "") or facts.exchange),
            _market(getattr(update, "market", "") or facts.market),
            _symbol(getattr(update, "symbol", "") or facts.symbol),
        )
        if route != facts.route_key:
            raise ValueError("Spot lifecycle route does not match metadata")
        status = str(getattr(update, "status", "") or "").strip().upper()
        if status not in _SUPPORTED_STATUSES:
            raise ValueError(f"unsupported Spot order status: {status!r}")
        side = str(getattr(update, "side", "") or "").strip().upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"unsupported Spot order side: {side!r}")
        order_identity = str(
            getattr(update, "exchange_order_id", "")
            or getattr(update, "order_id", "")
            or ""
        ).strip()
        if not order_identity:
            raise ValueError("Spot lifecycle order identity is required")
        order_key = (*route, order_identity)
        previous = self.order_states.get(order_key, _OrderState(ZERO, ZERO, ""))

        fill_qty = _exact(update, "qty_decimal", "qty")
        fill_price = _exact(update, "fill_price_decimal", "fill_price")
        fill_quote = _exact(update, "quote_qty_decimal")
        if fill_qty and fill_quote == ZERO:
            if fill_price == ZERO:
                raise ValueError("Spot fill requires a price or quote quantity")
            fill_quote = fill_qty * fill_price
        fee = _exact(update, "fee_decimal", "fee")
        fee_asset = _asset_code(getattr(update, "fee_asset", "") or facts.quote_asset)

        cumulative_qty = _exact(update, "executed_qty_decimal", "executed_qty")
        if cumulative_qty == ZERO and fill_qty:
            cumulative_qty = previous.executed_qty + fill_qty
        cumulative_quote_raw = getattr(update, "cumulative_quote_qty_decimal", "")
        cumulative_quote = (
            _decimal(cumulative_quote_raw, "cumulative_quote_qty_decimal")
            if cumulative_quote_raw not in (None, "")
            else previous.cumulative_quote_qty + fill_quote
        )
        if cumulative_qty < previous.executed_qty or cumulative_quote < previous.cumulative_quote_qty:
            return False
        if fill_qty == ZERO and (
            cumulative_qty > previous.executed_qty
            or cumulative_quote > previous.cumulative_quote_qty
        ):
            self.recovery_pending_orders.add(order_key)
            return False
        if _STATUS_RANK[status] < _STATUS_RANK[previous.status]:
            if fill_qty == ZERO:
                return False
            status = previous.status

        trade_id = str(getattr(update, "exchange_trade_id", "") or "").strip()
        trade_key = (*route, order_identity, trade_id)
        fill_applied = False
        if fill_qty:
            if not trade_id:
                self.recovery_pending_orders.add(order_key)
                return False
            if trade_key in self.applied_trade_ids:
                if (
                    cumulative_qty > previous.executed_qty
                    or cumulative_quote > previous.cumulative_quote_qty
                ):
                    self.recovery_pending_orders.add(order_key)
                    return False
            else:
                if (
                    cumulative_qty - previous.executed_qty != fill_qty
                    or cumulative_quote - previous.cumulative_quote_qty != fill_quote
                ):
                    self.recovery_pending_orders.add(order_key)
                    return False
                fill_applied = True

        orig_qty = _exact(update, "orig_qty_decimal", "orig_qty")
        remaining_qty = _exact(update, "remaining_qty_decimal", "remaining_qty")
        if orig_qty == ZERO:
            orig_qty = cumulative_qty + remaining_qty
        if remaining_qty == ZERO and status in _ACTIVE_STATUSES and orig_qty > cumulative_qty:
            remaining_qty = orig_qty - cumulative_qty
        price = _exact(update, "price_decimal", "price") or fill_price
        existing = self.open_orders.get(order_key)
        order = SpotOpenOrder(
            route_key=route,
            order_identity=order_identity,
            side=side,
            status=status,
            orig_qty=orig_qty,
            executed_qty=cumulative_qty,
            remaining_qty=remaining_qty,
            cumulative_quote_qty=cumulative_quote,
            price=price,
            locked_quote=existing.locked_quote if existing else ZERO,
            locked_base=existing.locked_base if existing else ZERO,
        )
        if fill_applied:
            self._apply_fill(
                metadata=facts,
                side=side,
                qty=fill_qty,
                quote_qty=fill_quote,
                fee=fee,
                fee_asset=fee_asset,
                existing_order=existing,
            )
            if existing and side == "BUY":
                order.locked_quote = max(ZERO, order.locked_quote - fill_quote)
            elif existing:
                order.locked_base = max(ZERO, order.locked_base - fill_qty)
            self.applied_trade_ids.add(trade_key)
            self.recovery_pending_orders.discard(order_key)

        self.order_states[order_key] = _OrderState(cumulative_qty, cumulative_quote, status)
        self._sync_locks(order, facts)
        if status in _TERMINAL_STATUSES or remaining_qty == ZERO:
            self._release_locks(order, facts)
            self.open_orders.pop(order_key, None)
        else:
            self.open_orders[order_key] = order
        return fill_applied


__all__ = [
    "SpotAssetBalance",
    "SpotFilterViolation",
    "SpotOpenOrder",
    "SpotSymbolMetadata",
    "SpotWallet",
]
