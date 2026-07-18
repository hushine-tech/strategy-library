"""Route-aware offline wallet container for mixed-market replay."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hushine_strategy.inputs import _normalize_exchange, _normalize_market


RouteKey = tuple[str, str]
VenueWalletKey = tuple[str, str, int]


@dataclass
class PortfolioWallet:
    allowed_routes: set[RouteKey]
    wallets: dict[VenueWalletKey, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.allowed_routes = {
            (_normalize_exchange(exchange), _normalize_market(market))
            for exchange, market in self.allowed_routes
        }
        normalized_wallets: dict[VenueWalletKey, Any] = {}
        for (exchange, market, venue_id), wallet in self.wallets.items():
            key = (
                _normalize_exchange(exchange),
                _normalize_market(market),
                int(venue_id),
            )
            if key in normalized_wallets:
                raise ValueError(f"duplicate normalized venue wallet route: {key!r}")
            normalized_wallets[key] = wallet
        self.wallets = normalized_wallets
        for exchange, market, _venue_id in self.wallets:
            if (exchange, market) not in self.allowed_routes:
                raise ValueError(f"wallet route {exchange}/{market} is not declared")

    @classmethod
    def spot(
        cls,
        wallet: Any,
        *,
        venue_id: int = 1,
        exchange: str = "binance",
    ) -> "PortfolioWallet":
        route = (_normalize_exchange(exchange), "spot")
        return cls(
            allowed_routes={route},
            wallets={(route[0], route[1], int(venue_id)): wallet},
        )

    def get(self, exchange: str, market: str, *, venue_id: int | None = None) -> Any:
        route = (_normalize_exchange(exchange), _normalize_market(market))
        if route not in self.allowed_routes:
            raise ValueError(f"wallet route {route[0]}/{route[1]} is not declared")
        if venue_id is not None:
            wallet = self.wallets.get((*route, int(venue_id)))
            if wallet is None:
                raise ValueError(
                    f"missing wallet for route {route[0]}/{route[1]} venue {venue_id}"
                )
            return wallet
        matches = [
            (candidate_venue_id, wallet)
            for (exchange_name, market_name, candidate_venue_id), wallet in self.wallets.items()
            if (exchange_name, market_name) == route
        ]
        if not matches:
            raise ValueError(f"missing wallet for route {route[0]}/{route[1]}")
        if len(matches) != 1:
            venues = ", ".join(str(item[0]) for item in sorted(matches))
            raise ValueError(f"ambiguous wallet route {route[0]}/{route[1]}: {venues}")
        return matches[0][1]


__all__ = ["PortfolioWallet", "RouteKey", "VenueWalletKey"]
