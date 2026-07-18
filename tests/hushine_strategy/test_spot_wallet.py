from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from hushine_strategy.wallet.spot import (
    SpotAssetBalance,
    SpotSymbolMetadata,
    SpotWallet,
)


def _metadata(symbol: str = "BTCUSDT", base_asset: str = "BTC") -> SpotSymbolMetadata:
    return SpotSymbolMetadata(
        venue_id=10,
        exchange="binance",
        market="spot",
        symbol=symbol,
        status="TRADING",
        base_asset=base_asset,
        quote_asset="USDT",
        base_asset_precision=8,
        quote_asset_precision=8,
        spot_trading_allowed=True,
        permission_sets=(("SPOT",),),
        order_types=("LIMIT", "MARKET"),
    )


def _fill(
    *,
    side: str,
    trade_id: str,
    qty: str,
    quote_qty: str,
    fee: str,
    fee_asset: str,
    order_id: str = "order-1",
    status: str = "FILLED",
    orig_qty: str | None = None,
    executed_qty: str | None = None,
    remaining_qty: str = "0",
    price: str = "50000",
):
    return SimpleNamespace(
        venue_id=10,
        exchange="binance",
        market="spot",
        symbol="BTCUSDT",
        side=side,
        status=status,
        order_id=order_id,
        exchange_order_id=order_id,
        exchange_trade_id=trade_id,
        qty_decimal=qty,
        fill_price_decimal=str(Decimal(quote_qty) / Decimal(qty)) if Decimal(qty) else "0",
        quote_qty_decimal=quote_qty,
        fee_decimal=fee,
        fee_asset=fee_asset,
        orig_qty_decimal=orig_qty if orig_qty is not None else qty,
        executed_qty_decimal=executed_qty if executed_qty is not None else qty,
        remaining_qty_decimal=remaining_qty,
        price_decimal=price,
        cumulative_quote_qty_decimal=quote_qty,
    )


def test_spot_buy_sell_and_commission_use_binance_asset_codes():
    wallet = SpotWallet.from_assets({
        "USDT": ("1000", "0"),
        "BNB": ("1", "0"),
    })
    metadata = wallet.register_metadata(_metadata())

    wallet.apply_order_update(_fill(
        side="BUY",
        trade_id="trade-buy",
        qty="0.01",
        quote_qty="500",
        fee="0.001",
        fee_asset="BNB",
        order_id="buy-1",
    ), metadata)
    wallet.apply_order_update(_fill(
        side="SELL",
        trade_id="trade-sell",
        qty="0.01",
        quote_qty="510",
        fee="0.51",
        fee_asset="USDT",
        order_id="sell-1",
        price="51000",
    ), metadata)

    assert wallet.assets["BTC"].free == Decimal("0")
    assert wallet.assets["USDT"].free == Decimal("1009.49")
    assert wallet.assets["BNB"].free == Decimal("0.999")
    assert "BTCUSDT" not in wallet.assets


def test_same_route_trade_is_applied_once_across_post_ws_and_rest_replay():
    wallet = SpotWallet.from_assets({"USDT": ("1000", "0")})
    metadata = wallet.register_metadata(_metadata())
    event = _fill(
        side="BUY",
        trade_id="trade-7",
        qty="0.01",
        quote_qty="500",
        fee="0.5",
        fee_asset="USDT",
    )

    assert wallet.apply_order_update(event, metadata) is True
    assert wallet.apply_order_update(event, metadata) is False
    assert wallet.apply_order_update(event, metadata) is False

    assert wallet.assets["BTC"].free == Decimal("0.01")
    assert wallet.assets["USDT"].free == Decimal("499.5")
    assert len(wallet.applied_trade_ids) == 1


def test_duplicate_trade_can_advance_terminal_state_without_reapplying_fill():
    wallet = SpotWallet.from_assets({"USDT": ("1000", "0")})
    metadata = wallet.register_metadata(_metadata())
    new_order = _fill(
        side="BUY",
        trade_id="",
        qty="0",
        quote_qty="0",
        fee="0",
        fee_asset="USDT",
        status="NEW",
        orig_qty="0.01",
        executed_qty="0",
        remaining_qty="0.01",
    )
    partial = _fill(
        side="BUY",
        trade_id="trade-1",
        qty="0.004",
        quote_qty="200",
        fee="0.2",
        fee_asset="USDT",
        status="PARTIALLY_FILLED",
        orig_qty="0.01",
        executed_qty="0.004",
        remaining_qty="0.006",
    )
    terminal_duplicate = _fill(
        side="BUY",
        trade_id="trade-1",
        qty="0.004",
        quote_qty="200",
        fee="0.2",
        fee_asset="USDT",
        status="CANCELED",
        orig_qty="0.01",
        executed_qty="0.004",
        remaining_qty="0.006",
    )

    assert wallet.apply_order_update(new_order, metadata) is False
    assert wallet.apply_order_update(partial, metadata) is True
    assert wallet.apply_order_update(terminal_duplicate, metadata) is False

    assert wallet.assets["BTC"].free == Decimal("0.004")
    assert wallet.assets["USDT"].free == Decimal("799.8")
    assert wallet.assets["USDT"].locked == Decimal("0")
    assert wallet.open_orders == {}
    assert next(iter(wallet.order_states.values())).status == "CANCELED"


def test_partial_buy_then_cancel_releases_only_the_unfilled_quote_lock():
    wallet = SpotWallet.from_assets({"USDT": ("1000", "0")})
    metadata = wallet.register_metadata(_metadata())
    new_order = _fill(
        side="BUY",
        trade_id="",
        qty="0",
        quote_qty="0",
        fee="0",
        fee_asset="USDT",
        status="NEW",
        orig_qty="0.01",
        executed_qty="0",
        remaining_qty="0.01",
    )
    partial = _fill(
        side="BUY",
        trade_id="trade-1",
        qty="0.004",
        quote_qty="200",
        fee="0.2",
        fee_asset="USDT",
        status="PARTIALLY_FILLED",
        orig_qty="0.01",
        executed_qty="0.004",
        remaining_qty="0.006",
    )
    canceled = _fill(
        side="BUY",
        trade_id="",
        qty="0",
        quote_qty="0",
        fee="0",
        fee_asset="USDT",
        status="CANCELED",
        orig_qty="0.01",
        executed_qty="0.004",
        remaining_qty="0.006",
    )
    canceled.cumulative_quote_qty_decimal = "200"

    assert wallet.apply_order_update(new_order, metadata) is False
    assert wallet.assets["USDT"].free == Decimal("500")
    assert wallet.assets["USDT"].locked == Decimal("500")
    assert wallet.apply_order_update(partial, metadata) is True
    assert wallet.assets["USDT"].free == Decimal("499.8")
    assert wallet.assets["USDT"].locked == Decimal("300")
    assert wallet.apply_order_update(canceled, metadata) is False
    assert wallet.assets["USDT"].free == Decimal("799.8")
    assert wallet.assets["USDT"].locked == Decimal("0")
    assert wallet.assets["BTC"].free == Decimal("0.004")


def test_spot_metadata_rejects_non_usdt_quote_and_pseudo_asset_is_not_derived():
    wallet = SpotWallet.from_assets({"BTCUSDT": ("1", "0"), "USDT": ("1", "0")})
    assert "BTCUSDT" in wallet.assets
    with pytest.raises(ValueError, match="asset code"):
        wallet.register_metadata(_metadata(base_asset="BTCUSDT"))

    with pytest.raises(ValueError, match="USDT"):
        wallet.register_metadata(replace(
            _metadata(),
            symbol="ETHBTC",
            base_asset="ETH",
            quote_asset="BTC",
        ))


def test_spot_wallet_rejects_a_trading_symbol_as_an_account_asset():
    wallet = SpotWallet.from_assets({"BTCUSDT": ("1", "0"), "USDT": ("1", "0")})

    with pytest.raises(ValueError, match="trading symbol"):
        wallet.register_metadata(_metadata())


def test_spot_wallet_rejects_duplicate_normalized_asset_codes():
    with pytest.raises(ValueError, match="duplicate normalized Spot asset"):
        SpotWallet.from_assets({"btc": ("1", "0"), "BTC": ("2", "0")})


def test_spot_metadata_copies_filter_facts_immutably():
    lot_size = {
        "filter_type": "LOT_SIZE",
        "min_qty": "0.00001",
        "max_qty": "9000",
        "step_size": "0.00001",
    }
    metadata = replace(_metadata(), filters=(lot_size,))

    lot_size["min_qty"] = "10"

    assert metadata.filter_facts()["filters"][0]["min_qty"] == "0.00001"


def test_spot_wallet_rejects_conflicting_metadata_for_the_same_route():
    wallet = SpotWallet.from_assets({"USDT": ("1000", "0")})
    wallet.register_metadata(_metadata())

    with pytest.raises(ValueError, match="conflicting immutable Spot metadata"):
        wallet.register_metadata(replace(_metadata(), base_asset_precision=7))
