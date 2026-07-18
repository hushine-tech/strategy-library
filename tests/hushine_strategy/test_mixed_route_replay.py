from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from hushine_strategy import MarketData
from hushine_strategy import OrderDecision
from hushine_strategy.inputs import StrategyInput, StrategyOrderTarget
from hushine_strategy.replay import ReplayEngine
from hushine_strategy.wallet import (
    FuturesWallet,
    PortfolioWallet,
    SpotFilterViolation,
    SpotSymbolMetadata,
    SpotWallet,
)


def _spot_metadata() -> SpotSymbolMetadata:
    return SpotSymbolMetadata(
        venue_id=10,
        exchange="binance",
        market="spot",
        symbol="BTCUSDT",
        status="TRADING",
        base_asset="BTC",
        quote_asset="USDT",
        base_asset_precision=8,
        quote_asset_precision=8,
        spot_trading_allowed=True,
    )


def _mixed_wallet() -> PortfolioWallet:
    return PortfolioWallet(
        allowed_routes={("binance", "spot"), ("binance", "perpetual_futures")},
        wallets={
            ("binance", "spot", 10): SpotWallet.from_assets({"USDT": ("1000", "0")}),
            ("binance", "perpetual_futures", 20): FuturesWallet(1000),
        },
    )


def test_same_symbol_spot_and_futures_prices_are_isolated_by_full_stream_identity():
    wallet = _mixed_wallet()
    metadata = _spot_metadata()
    engine = ReplayEngine(
        wallet=wallet,
        metadata={metadata.route_key: metadata},
        declared_inputs=[
            StrategyInput("binance", "spot", "BTCUSDT", "1m"),
            StrategyInput("binance", "perpetual_futures", "BTCUSDT", "1m"),
        ],
    )
    spot_tick = MarketData(
        stream_id="spot-btc-1m",
        exchange="binance",
        market="spot",
        kind="kline",
        symbol="BTCUSDT",
        interval="1m",
        price=50_000,
        timestamp=1,
    )
    futures_tick = MarketData(
        stream_id="futures-btc-1m",
        exchange="binance",
        market="perpetual_futures",
        kind="kline",
        symbol="BTCUSDT",
        interval="1m",
        price=50_100,
        timestamp=1,
    )

    assert engine.push_market_data(spot_tick) is True
    assert engine.push_market_data(futures_tick) is True

    assert engine.last_price(engine.stream_identity(spot_tick)) == Decimal("50000")
    assert engine.last_price(engine.stream_identity(futures_tick)) == Decimal("50100")
    assert wallet.get("binance", "spot").symbol_prices[metadata.route_key] == Decimal("50000")
    assert wallet.get("binance", "perpetual_futures").mark_price("BTCUSDT") == 50_100


def test_stream_id_and_kind_prevent_same_route_data_from_collapsing():
    wallet = _mixed_wallet()
    metadata = _spot_metadata()
    engine = ReplayEngine(
        wallet=wallet,
        metadata={metadata.route_key: metadata},
        declared_inputs=[StrategyInput("binance", "spot", "BTCUSDT", "1m")],
    )
    first = MarketData(
        stream_id="spot-a",
        exchange="binance",
        market="spot",
        kind="kline",
        symbol="BTCUSDT",
        interval="1m",
        price=50_000,
        timestamp=1,
    )
    second = MarketData(
        stream_id="spot-b",
        exchange="binance",
        market="spot",
        kind="mark_price",
        symbol="BTCUSDT",
        interval="1m",
        price=50_010,
        timestamp=2,
    )

    engine.push_market_data(first)
    engine.push_market_data(second)

    assert engine.stream_identity(first) != engine.stream_identity(second)
    assert engine.last_price(engine.stream_identity(first)) == Decimal("50000")
    assert engine.last_price(engine.stream_identity(second)) == Decimal("50010")


def test_undeclared_input_and_order_target_fail_closed():
    wallet = _mixed_wallet()
    metadata = _spot_metadata()
    engine = ReplayEngine(
        wallet=wallet,
        metadata={metadata.route_key: metadata},
        declared_inputs=[StrategyInput("binance", "spot", "BTCUSDT", "1m")],
        order_targets=[StrategyOrderTarget("binance", "spot", "BTCUSDT")],
    )

    assert engine.push_market_data(MarketData(
        stream_id="undeclared",
        exchange="binance",
        market="spot",
        kind="kline",
        symbol="ETHUSDT",
        interval="1m",
        price=3000,
        timestamp=1,
    )) is False

    with pytest.raises(ValueError, match="not declared"):
        engine.execute_order(OrderDecision(
            exchange="binance",
            market="spot",
            symbol="ETHUSDT",
            side="BUY",
            qty="0.01",
            order_type="MARKET",
        ), mark_price="3000")


def test_okx_execution_remains_fail_closed():
    wallet = PortfolioWallet(
        allowed_routes={("okx", "perpetual_futures")},
        wallets={("okx", "perpetual_futures", 21): FuturesWallet(1000)},
    )
    engine = ReplayEngine(
        wallet=wallet,
        order_targets=[StrategyOrderTarget("okx", "perpetual_futures", "BTCUSDT")],
    )

    with pytest.raises(ValueError, match="exchange okx"):
        engine.execute_order(OrderDecision(
            exchange="okx",
            market="perpetual_futures",
            symbol="BTCUSDT",
            side="BUY",
            qty="0.01",
            order_type="MARKET",
        ), mark_price="50000")


def test_explicit_stream_and_kind_declaration_rejects_near_matches():
    wallet = _mixed_wallet()
    metadata = _spot_metadata()
    engine = ReplayEngine(
        wallet=wallet,
        metadata={metadata.route_key: metadata},
        declared_inputs=[StrategyInput(
            "binance",
            "spot",
            "BTCUSDT",
            "1m",
            stream_id="spot-btc-kline",
            kind="kline",
        )],
    )

    def tick(stream_id: str, kind: str) -> MarketData:
        return MarketData(
            stream_id=stream_id,
            exchange="binance",
            market="spot",
            kind=kind,
            symbol="BTCUSDT",
            interval="1m",
            price=50_000,
            timestamp=1,
        )

    assert engine.push_market_data(tick("wrong-stream", "kline")) is False
    assert engine.push_market_data(tick("spot-btc-kline", "mark_price")) is False
    assert engine.push_market_data(tick("spot-btc-kline", "kline")) is True


def test_metadata_mapping_key_must_match_the_immutable_route():
    wallet = _mixed_wallet()
    metadata = _spot_metadata()

    with pytest.raises(ValueError, match="metadata key does not match"):
        ReplayEngine(
            wallet=wallet,
            metadata={(999, "binance", "spot", "BTCUSDT"): metadata},
        )


def test_declared_spot_route_requires_metadata_before_replay_starts():
    with pytest.raises(ValueError, match="missing immutable Spot metadata"):
        ReplayEngine(
            wallet=_mixed_wallet(),
            declared_inputs=[StrategyInput("binance", "spot", "BTCUSDT", "1m")],
        )


def test_multi_symbol_spot_prices_and_mixed_market_orders_remain_isolated():
    wallet = _mixed_wallet()
    btc = _spot_metadata()
    eth = SpotSymbolMetadata(
        venue_id=10,
        exchange="binance",
        market="spot",
        symbol="ETHUSDT",
        status="TRADING",
        base_asset="ETH",
        quote_asset="USDT",
        base_asset_precision=8,
        quote_asset_precision=8,
        spot_trading_allowed=True,
        filters=({
            "filter_type": "LOT_SIZE",
            "min_qty": "0.00001",
            "max_qty": "9000",
            "step_size": "0.00001",
        },),
    )
    btc = replace(btc, filters=({
            "filter_type": "LOT_SIZE",
            "min_qty": "0.00001",
            "max_qty": "9000",
            "step_size": "0.00001",
        },))
    engine = ReplayEngine(
        wallet=wallet,
        metadata={btc.route_key: btc, eth.route_key: eth},
        declared_inputs=[
            StrategyInput("binance", "spot", "BTCUSDT", "1m"),
            StrategyInput("binance", "spot", "ETHUSDT", "5m"),
            StrategyInput("binance", "perpetual_futures", "BTCUSDT", "1m"),
        ],
        order_targets=[
            StrategyOrderTarget("binance", "spot", "BTCUSDT"),
            StrategyOrderTarget("binance", "perpetual_futures", "BTCUSDT"),
        ],
        risk_facts={btc.route_key: {"reference_price_decimal": "50000"}},
    )
    btc_tick = MarketData(
        stream_id="spot-btc",
        exchange="binance",
        market="spot",
        kind="kline",
        symbol="BTCUSDT",
        interval="1m",
        price=50_000,
        timestamp=1,
    )
    eth_tick = MarketData(
        stream_id="spot-eth",
        exchange="binance",
        market="spot",
        kind="kline",
        symbol="ETHUSDT",
        interval="5m",
        price=3_000,
        timestamp=1,
    )
    assert engine.push_market_data(btc_tick) is True
    assert engine.push_market_data(eth_tick) is True
    assert engine.last_price(engine.stream_identity(btc_tick)) == Decimal("50000")
    assert engine.last_price(engine.stream_identity(eth_tick)) == Decimal("3000")

    engine.execute_order(OrderDecision(
        exchange="binance",
        market="spot",
        symbol="BTCUSDT",
        side="BUY",
        qty="0.01",
        order_type="MARKET",
    ), mark_price="50000")
    engine.execute_order(OrderDecision(
        exchange="binance",
        market="perpetual_futures",
        symbol="BTCUSDT",
        side="BUY",
        qty="0.02",
        order_type="MARKET",
    ), mark_price="50100")

    assert wallet.get("binance", "spot").assets["BTC"].free == Decimal("0.01")
    assert wallet.get("binance", "spot").assets["USDT"].free == Decimal("499.8")
    assert wallet.get("binance", "perpetual_futures").position_qty("BTCUSDT") == 0.02


def test_replay_engine_copies_metadata_and_risk_facts_before_execution():
    wallet = _mixed_wallet()
    lot_size = {
        "filter_type": "LOT_SIZE",
        "min_qty": "0.00001",
        "max_qty": "9000",
        "step_size": "0.00001",
    }
    metadata = SpotSymbolMetadata(
        venue_id=10,
        exchange="binance",
        market="spot",
        symbol="BTCUSDT",
        status="TRADING",
        base_asset="BTC",
        quote_asset="USDT",
        base_asset_precision=8,
        quote_asset_precision=8,
        spot_trading_allowed=True,
        filters=(lot_size,),
    )
    risk_facts = {metadata.route_key: {"reference_price_decimal": "50000"}}
    engine = ReplayEngine(
        wallet=wallet,
        metadata={metadata.route_key: metadata},
        order_targets=[StrategyOrderTarget("binance", "spot", "BTCUSDT")],
        risk_facts=risk_facts,
    )

    lot_size["min_qty"] = "10"
    risk_facts[metadata.route_key]["reference_price_decimal"] = "1"

    assert engine.execute_order(OrderDecision(
        exchange="binance",
        market="spot",
        symbol="BTCUSDT",
        side="BUY",
        qty="0.01",
        order_type="MARKET",
    ), mark_price="50000") is True


@pytest.mark.parametrize(
    ("metadata_changes", "order_type", "expected_code"),
    [
        ({"status": "BREAK"}, "MARKET", "SPOT_SYMBOL_NOT_TRADING"),
        ({"spot_trading_allowed": False}, "MARKET", "SPOT_TRADING_DISABLED"),
        ({"order_types": ("LIMIT",)}, "MARKET", "SPOT_ORDER_TYPE_UNSUPPORTED"),
    ],
)
def test_spot_execution_rechecks_immutable_symbol_admission(
    metadata_changes,
    order_type,
    expected_code,
):
    wallet = _mixed_wallet()
    metadata = replace(
        _spot_metadata(),
        filters=({
            "filter_type": "LOT_SIZE",
            "min_qty": "0.00001",
            "max_qty": "9000",
            "step_size": "0.00001",
        },),
        **metadata_changes,
    )
    engine = ReplayEngine(
        wallet=wallet,
        metadata={metadata.route_key: metadata},
        order_targets=[StrategyOrderTarget("binance", "spot", "BTCUSDT")],
        risk_facts={metadata.route_key: {"reference_price_decimal": "50000"}},
    )

    with pytest.raises(SpotFilterViolation) as captured:
        engine.execute_order(OrderDecision(
            exchange="binance",
            market="spot",
            symbol="BTCUSDT",
            side="BUY",
            qty="0.01",
            order_type=order_type,
            price="50000" if order_type == "LIMIT" else None,
        ), mark_price="50000")

    assert captured.value.code == expected_code
