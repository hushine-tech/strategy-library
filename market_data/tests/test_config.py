from market_data.config import (
    KafkaBrokerConfig,
    KafkaConfig,
    LiveKlineSubscription,
    parse_live_kline_topic,
    resolve_live_kline_topic,
)


def test_resolve_live_kline_topic_uses_canonical_family():
    topic = resolve_live_kline_topic("Binance", "Futures", "1m")
    assert topic == "md.kline.binance.futures.1m"


def test_parse_live_kline_topic_extracts_exchange_market_interval():
    parsed = parse_live_kline_topic("md.kline.binance.spot.5m")
    assert parsed == ("binance", "spot", "5m")


def test_live_kline_subscription_from_symbols_with_market_dedupes_markets():
    subscription = LiveKlineSubscription.from_symbols_with_market(
        [("BTCUSDT", "futures"), ("ETHUSDT", "spot"), ("SOLUSDT", "futures")],
        interval="1m",
        consumer_group="strategy-session-7-sess-123",
    )

    assert subscription.consumer_group == "strategy-session-7-sess-123"
    assert subscription.allowed_symbols_with_market == frozenset(
        {
            ("BTCUSDT", "futures"),
            ("ETHUSDT", "spot"),
            ("SOLUSDT", "futures"),
        }
    )
    assert subscription.topics == [
        "md.kline.binance.futures.1m",
        "md.kline.binance.spot.1m",
    ]


def test_kafka_config_for_live_kline_subscription_uses_subscription_topics_and_group():
    subscription = LiveKlineSubscription.from_symbols_with_market(
        [("BTCUSDT", "futures")],
        interval="1m",
        consumer_group="strategy-session-9-sess-555",
    )

    cfg = KafkaConfig.for_live_kline_subscription(
        subscription,
        brokers=[KafkaBrokerConfig(host="kafka-1", port=9092)],
    )

    assert cfg.consumer_group == "strategy-session-9-sess-555"
    assert cfg.topics == ["md.kline.binance.futures.1m"]
    assert cfg.live_kline_subscription == subscription
    assert cfg.bootstrap_servers == "kafka-1:9092"


# ── Multi-interval subscription (pre_C3 gate 2) ───────────────────────────


def test_from_declared_inputs_covers_every_market_interval_pair():
    """A strategy declaring BTCUSDT 1m + BTCUSDT 5m + ETHUSDT 1m spot must
    get THREE distinct topics — collapsing to (market, interval) deduplicates
    but never drops intervals."""
    subscription = LiveKlineSubscription.from_declared_inputs(
        [
            ("futures", "BTCUSDT", "1m"),
            ("futures", "BTCUSDT", "5m"),
            ("spot", "ETHUSDT", "1m"),
        ],
        consumer_group="cg-multi",
    )
    assert subscription.consumer_group == "cg-multi"
    # One topic per (market, interval) pair.
    assert sorted(subscription.topics) == sorted([
        "md.kline.binance.futures.1m",
        "md.kline.binance.futures.5m",
        "md.kline.binance.spot.1m",
    ])
    # allowed_inputs is authoritative for matches().
    assert subscription.allowed_inputs == frozenset({
        ("BTCUSDT", "futures", "1m"),
        ("BTCUSDT", "futures", "5m"),
        ("ETHUSDT", "spot", "1m"),
    })


def test_from_declared_inputs_accepts_strategyinput_like_objects():
    """``from_declared_inputs`` should accept duck-typed objects with
    ``.market`` / ``.symbol`` / ``.interval`` so callers don't have to
    serialise ``StrategyInput`` into tuples."""
    class DuckInput:
        def __init__(self, market, symbol, interval):
            self.market = market
            self.symbol = symbol
            self.interval = interval

    subscription = LiveKlineSubscription.from_declared_inputs(
        [DuckInput("futures", "BTCUSDT", "1m"), DuckInput("futures", "BTCUSDT", "5m")],
        consumer_group="cg-duck",
    )
    assert sorted(subscription.topics) == [
        "md.kline.binance.futures.1m",
        "md.kline.binance.futures.5m",
    ]


def test_matches_uses_three_tuple_filter_when_declared_inputs_present():
    """``matches()`` must reject an interval that isn't in allowed_inputs,
    even when (symbol, market) alone would have been allowed. This is the
    core guarantee: no declared 1m input is silently fed 5m data, and vice
    versa."""
    subscription = LiveKlineSubscription.from_declared_inputs(
        [("futures", "BTCUSDT", "1m")],
        consumer_group="cg-filter",
    )
    # Exact declared match → accepted.
    assert subscription.matches(
        topic="md.kline.binance.futures.1m",
        symbol="BTCUSDT", market="futures", interval="1m",
    ) is True
    # Wrong interval — symbol + market match but interval isn't declared.
    assert subscription.matches(
        topic="md.kline.binance.futures.5m",
        symbol="BTCUSDT", market="futures", interval="5m",
    ) is False
    # Wrong symbol.
    assert subscription.matches(
        topic="md.kline.binance.futures.1m",
        symbol="ETHUSDT", market="futures", interval="1m",
    ) is False


def test_from_declared_inputs_empty_raises():
    import pytest

    with pytest.raises(ValueError, match="at least one declared input"):
        LiveKlineSubscription.from_declared_inputs([], consumer_group="cg-empty")
