from __future__ import annotations

from dataclasses import dataclass

from hushine_strategy.types import OrderDecision, OrderFill


@dataclass
class _Position:
    qty: float = 0.0
    entry_price: float = 0.0


class FuturesWallet:
    def __init__(self, initial_balance: float = 1000.0) -> None:
        self.initial_balance = float(initial_balance)
        self.wallet_balance = float(initial_balance)
        self.available_balance = float(initial_balance)
        self._mark_prices: dict[str, float] = {}
        self._positions: dict[str, _Position] = {}

    def update_mark_price(self, symbol: str, price: float) -> None:
        self._mark_prices[str(symbol).upper()] = float(price)

    def mark_price(self, symbol: str) -> float | None:
        return self._mark_prices.get(str(symbol).upper())

    def position_qty(self, symbol: str) -> float:
        return self._positions.get(str(symbol).upper(), _Position()).qty

    def position_entry_price(self, symbol: str) -> float:
        return self._positions.get(str(symbol).upper(), _Position()).entry_price

    def fill_order(self, decision: OrderDecision, price: float) -> OrderFill:
        symbol = str(decision.symbol).upper()
        side = str(decision.side).upper()
        qty = float(decision.qty)
        if qty <= 0:
            raise ValueError("qty must be positive")
        if side not in {"LONG", "BUY", "SHORT", "SELL"}:
            raise ValueError(f"unsupported side: {decision.side}")
        signed_qty = qty
        if side in {"SHORT", "SELL"}:
            signed_qty = -abs(signed_qty)
        else:
            signed_qty = abs(signed_qty)
        current = self._positions.get(symbol, _Position())
        next_qty = current.qty + signed_qty
        if current.qty == 0 or (current.qty > 0) == (signed_qty > 0):
            notional_before = abs(current.qty) * current.entry_price
            notional_after = abs(signed_qty) * float(price)
            total_qty = abs(current.qty) + abs(signed_qty)
            entry = (notional_before + notional_after) / total_qty if total_qty else 0.0
        else:
            if next_qty == 0:
                entry = 0.0
            elif (current.qty > 0) != (next_qty > 0):
                entry = float(price)
            else:
                entry = current.entry_price
        self._positions[symbol] = _Position(qty=next_qty, entry_price=entry)
        return OrderFill(symbol=symbol, side=side, qty=qty, price=float(price))
