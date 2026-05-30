import pytest

from hushine_strategy import (
    Exchange,
    InputView,
    LocalNotifier,
    Market,
    MarketData,
    OrderDecision,
    StrategyInput,
    parse_order_targets as parse_order_targets_from_root,
    parse_declared_inputs,
)
from hushine_strategy.inputs import StrategyOrderTarget, parse_order_targets
from hushine_strategy.types import OrderSide, OrderType, PositionSide


def test_strategy_api_constants_are_public_string_values():
    assert Exchange.BINANCE == "binance"
    assert Exchange.OKX == "okx"
    assert Market.SPOT == "spot"
    assert Market.PERPETUAL_FUTURES == "perpetual_futures"
    assert Market.DELIVERY_FUTURES == "delivery_futures"
    assert OrderSide.BUY == "BUY"
    assert OrderSide.SELL == "SELL"
    assert OrderType.MARKET == "MARKET"
    assert OrderType.LIMIT == "LIMIT"
    assert PositionSide.BOTH == "BOTH"
    assert PositionSide.LONG == "LONG"
    assert PositionSide.SHORT == "SHORT"


def test_root_import_exports_strategy_api_constants_and_order_target_parser():
    targets = parse_order_targets_from_root([
        {"exchange": Exchange.BINANCE, "market": Market.SPOT, "symbol": "btcusdt"},
    ])
    assert targets == [StrategyOrderTarget(exchange="binance", market="spot", symbol="BTCUSDT")]


def test_order_decision_requires_explicit_route_fields():
    decision = OrderDecision(
        exchange=Exchange.BINANCE,
        market=Market.PERPETUAL_FUTURES,
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        qty="0.01",
        order_type=OrderType.MARKET,
    )
    assert decision.exchange == "binance"
    assert decision.market == "perpetual_futures"
    assert decision.symbol == "BTCUSDT"
    assert decision.side == "BUY"
    assert decision.qty == "0.01"
    assert decision.order_type == "MARKET"
    assert decision.price is None


def test_parse_declared_inputs_normalizes_values():
    inputs = parse_declared_inputs([
        {"exchange": "Binance", "market": "perpetual_futures", "symbol": "btcusdt", "interval": "1m"},
        {"exchange": "okx", "market": "spot", "symbol": "ethusdt", "interval": "5m"},
    ])
    assert inputs == [
        StrategyInput(exchange="binance", market="perpetual_futures", symbol="BTCUSDT", interval="1m"),
        StrategyInput(exchange="okx", market="spot", symbol="ETHUSDT", interval="5m"),
    ]


def test_parse_declared_inputs_requires_exchange():
    with pytest.raises(ValueError, match="INPUTS exchange, market, symbol, and interval are required"):
        parse_declared_inputs([{"market": "perpetual_futures", "symbol": "BTCUSDT", "interval": "1m"}])


def test_parse_declared_inputs_rejects_futures_alias():
    with pytest.raises(ValueError, match="unsupported market: futures"):
        parse_declared_inputs([
            {"exchange": "Binance", "market": "futures", "symbol": "btcusdt", "interval": "1m"},
        ])


def test_parse_declared_inputs_rejects_empty_list():
    with pytest.raises(ValueError, match="INPUTS must declare at least one stream"):
        parse_declared_inputs([])


@pytest.mark.parametrize("raw", [
    [{"market": "perpetual_futures", "symbol": "BTCUSDT", "interval": "1m"}],
    [{"exchange": None, "market": "perpetual_futures", "symbol": "BTCUSDT", "interval": "1m"}],
    [{"exchange": "binance", "market": None, "symbol": "BTCUSDT", "interval": "1m"}],
    [{"exchange": "binance", "market": "perpetual_futures", "symbol": None, "interval": "1m"}],
    [{"exchange": "binance", "market": "perpetual_futures", "symbol": "BTCUSDT", "interval": None}],
    [{"exchange": "binance", "market": " ", "symbol": "BTCUSDT", "interval": "1m"}],
])
def test_parse_declared_inputs_rejects_missing_none_or_blank_fields(raw):
    with pytest.raises(ValueError, match="INPUTS exchange, market, symbol, and interval are required"):
        parse_declared_inputs(raw)


def test_input_view_returns_latest_tick_by_market_symbol_interval():
    view = InputView([StrategyInput(exchange="binance", market="perpetual_futures", symbol="BTCUSDT", interval="1m")])
    tick = MarketData(symbol="BTCUSDT", price=100.0, timestamp=123, market="perpetual_futures", interval="1m")
    assert view.update(tick) is True
    assert view.exchange["binance"]["perpetual_futures"].symbol["BTCUSDT"].interval["1m"] == tick


def test_market_data_defaults_to_perpetual_futures_and_keeps_platform_kline_compatibility_fields():
    default_tick = MarketData(symbol="BTCUSDT", price=100.0, timestamp=123)
    assert default_tick.market == Market.PERPETUAL_FUTURES

    tick = MarketData(
        symbol="BTCUSDT",
        price=100.0,
        timestamp=123,
        market="perpetual_futures",
        interval="1m",
        klines={"close": 100.0},
    )
    assert tick.klines == {"close": 100.0}
    assert tick.orderbook is None
    assert tick.oi is None
    assert tick.funding_rate is None


def test_input_view_ignores_undeclared_ticks():
    view = InputView([StrategyInput(exchange="binance", market="perpetual_futures", symbol="BTCUSDT", interval="1m")])
    tick = MarketData(symbol="ETHUSDT", price=100.0, timestamp=123, market="perpetual_futures", interval="1m")
    assert view.update(tick) is False
    assert view.exchange["binance"]["perpetual_futures"].symbol["ETHUSDT"].interval["1m"] is None


def test_input_view_normalizes_public_strategy_input_keys():
    view = InputView([StrategyInput(exchange="Binance", market="perpetual_futures", symbol="btcusdt", interval="1m")])
    tick = MarketData(symbol="BTCUSDT", price=100.0, timestamp=123, market="perpetual_futures", interval="1m")
    assert view.update(tick) is True
    assert view.exchange["binance"]["perpetual_futures"].symbol["BTCUSDT"].interval["1m"] == tick


def test_parse_order_targets_normalizes_symbol_scoped_target():
    targets = parse_order_targets([
        {"exchange": "Binance", "market": "perpetual_futures", "symbol": "btcusdt"},
        StrategyOrderTarget(exchange="okx", market="spot", symbol="ethusdt"),
    ])
    assert targets == [
        StrategyOrderTarget(exchange="binance", market="perpetual_futures", symbol="BTCUSDT"),
        StrategyOrderTarget(exchange="okx", market="spot", symbol="ETHUSDT"),
    ]
    assert targets[0].key == ("binance", "perpetual_futures", "BTCUSDT")


def test_parse_order_targets_allows_read_only_empty_list():
    assert parse_order_targets([]) == []


def test_parse_order_targets_requires_explicit_declaration():
    with pytest.raises(ValueError, match="ORDER_TARGETS must be declared, use \\[\\] for read-only strategies"):
        parse_order_targets(None)


@pytest.mark.parametrize("raw", [
    [{"market": "perpetual_futures", "symbol": "BTCUSDT"}],
    [{"exchange": None, "market": "perpetual_futures", "symbol": "BTCUSDT"}],
    [{"exchange": "binance", "market": None, "symbol": "BTCUSDT"}],
    [{"exchange": "binance", "market": "perpetual_futures", "symbol": None}],
    [{"exchange": "binance", "market": " ", "symbol": "BTCUSDT"}],
    [{"exchange": "binance", "market": "perpetual_futures", "symbol": " "}],
])
def test_parse_order_targets_rejects_missing_none_or_blank_fields(raw):
    with pytest.raises(ValueError, match="ORDER_TARGETS exchange, market, and symbol are required"):
        parse_order_targets(raw)


def test_parse_order_targets_rejects_futures_alias():
    with pytest.raises(ValueError, match="unsupported market: futures"):
        parse_order_targets([
            {"exchange": "Binance", "market": "futures", "symbol": "btcusdt"},
        ])


def test_parse_order_targets_rejects_invalid_item_type():
    with pytest.raises(ValueError, match="each ORDER_TARGETS item must be a dict with exchange, market, and symbol"):
        parse_order_targets(["BTCUSDT"])


def test_input_view_does_not_expose_market_shortcut():
    view = InputView([StrategyInput(exchange="binance", market="perpetual_futures", symbol="BTCUSDT", interval="1m")])
    assert not hasattr(view, "market")


def test_local_notifier_writes_notifications_to_log_file(tmp_path):
    log_path = tmp_path / "notifications.log"
    notifier = LocalNotifier(log_path)

    notifier.info("booted", "session")
    notifier.warn("lagging")
    notifier.error("failed", "order")

    assert log_path.read_text(encoding="utf-8").splitlines() == [
        "INFO session booted",
        "WARNING  lagging",
        "ERROR order failed",
    ]
