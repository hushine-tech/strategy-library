import pytest

from hushine_strategy import (
    InputView,
    LocalNotifier,
    MarketData,
    OrderDecision,
    StrategyInput,
    parse_declared_inputs,
)


def test_order_decision_defaults_market_to_none():
    decision = OrderDecision(symbol="btcusdt", side="LONG", qty=0.01)
    assert decision.symbol == "btcusdt"
    assert decision.side == "LONG"
    assert decision.qty == 0.01
    assert decision.market is None


def test_parse_declared_inputs_normalizes_values():
    inputs = parse_declared_inputs([
        {"market": "Futures", "symbol": "btcusdt", "interval": "1m"},
        ("spot", "ethusdt", "5m"),
    ])
    assert inputs == [
        StrategyInput(market="futures", symbol="BTCUSDT", interval="1m"),
        StrategyInput(market="spot", symbol="ETHUSDT", interval="5m"),
    ]


def test_parse_declared_inputs_rejects_empty_list():
    with pytest.raises(ValueError, match="INPUTS must declare at least one stream"):
        parse_declared_inputs([])


@pytest.mark.parametrize("raw", [
    [{"symbol": "BTCUSDT", "interval": "1m"}],
    [{"market": None, "symbol": "BTCUSDT", "interval": "1m"}],
    [{"market": "futures", "symbol": None, "interval": "1m"}],
    [{"market": "futures", "symbol": "BTCUSDT", "interval": None}],
    [{"market": " ", "symbol": "BTCUSDT", "interval": "1m"}],
])
def test_parse_declared_inputs_rejects_missing_none_or_blank_fields(raw):
    with pytest.raises(ValueError, match="INPUTS market, symbol, and interval are required"):
        parse_declared_inputs(raw)


def test_input_view_returns_latest_tick_by_market_symbol_interval():
    view = InputView([StrategyInput(market="futures", symbol="BTCUSDT", interval="1m")])
    tick = MarketData(symbol="BTCUSDT", price=100.0, timestamp=123, market="futures", interval="1m")
    assert view.update(tick) is True
    assert view.market["futures"].symbol["BTCUSDT"].interval["1m"] == tick


def test_market_data_keeps_platform_kline_compatibility_fields():
    tick = MarketData(
        symbol="BTCUSDT",
        price=100.0,
        timestamp=123,
        market="futures",
        interval="1m",
        klines={"close": 100.0},
    )
    assert tick.klines == {"close": 100.0}
    assert tick.orderbook is None
    assert tick.oi is None
    assert tick.funding_rate is None


def test_input_view_ignores_undeclared_ticks():
    view = InputView([StrategyInput(market="futures", symbol="BTCUSDT", interval="1m")])
    tick = MarketData(symbol="ETHUSDT", price=100.0, timestamp=123, market="futures", interval="1m")
    assert view.update(tick) is False
    assert view.market["futures"].symbol["ETHUSDT"].interval["1m"] is None


def test_input_view_normalizes_public_strategy_input_keys():
    view = InputView([StrategyInput(market="Futures", symbol="btcusdt", interval="1m")])
    tick = MarketData(symbol="BTCUSDT", price=100.0, timestamp=123, market="futures", interval="1m")
    assert view.update(tick) is True
    assert view.market["futures"].symbol["BTCUSDT"].interval["1m"] == tick


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
