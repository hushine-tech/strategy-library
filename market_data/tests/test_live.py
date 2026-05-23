"""
Unit tests for LiveDataSource.
"""
import sys
import types

import pytest
from unittest.mock import MagicMock, patch
from market_data.config import KafkaConfig, LiveKlineSubscription
from market_data.live import LiveDataSource
from market_data.models import MarketKline, MarketOI, MarketFunding, MarketOrderBook


class TestLiveDataSource:
    def test_init_with_config(self, kafka_config):
        ds = LiveDataSource(config=kafka_config)
        assert ds._config == kafka_config
        assert ds._running is False
        assert ds._consumer is None

    def test_callback_registration(self, kafka_config):
        ds = LiveDataSource(config=kafka_config)
        cb = MagicMock()
        
        ds.on_kline(cb)
        assert cb in ds._callbacks["kline"]
        
        ds.on_oi(cb)
        assert cb in ds._callbacks["oi"]
        
        ds.on_funding(cb)
        assert cb in ds._callbacks["funding"]
        
        ds.on_orderbook(cb)
        assert cb in ds._callbacks["orderbook"]


class TestLiveDataSourceProcessMessage:
    def test_process_kline_message(self, kafka_config):
        ds = LiveDataSource(config=kafka_config)
        mock_message = MagicMock()
        mock_message.topic = "market-kline"
        mock_message.value = {
            "symbol": "BTCUSDT",
            "interval": "1m",
            "open_time": 1000,
            "close_time": 2000,
            "open": "100.5",
            "high": "101.0",
            "low": "100.0",
            "close": "100.75",
            "volume": "1000.5",
            "timestamp": 1500,
        }
        
        cb = MagicMock()
        ds.on_kline(cb)
        ds._process_message(mock_message)
        
        cb.assert_called_once()
        kline = cb.call_args[0][0]
        assert isinstance(kline, MarketKline)
        assert kline.symbol == "BTCUSDT"
        assert kline.market is None

    def test_process_prefixed_kline_topic(self, kafka_config):
        ds = LiveDataSource(config=kafka_config)
        mock_message = MagicMock()
        mock_message.topic = "md.kline.binance.futures.1m"
        mock_message.value = {
            "symbol": "BTCUSDT",
            "interval": "1m",
            "open_time": 1000,
            "close_time": 2000,
            "open": "100.5",
            "high": "101.0",
            "low": "100.0",
            "close": "100.75",
            "volume": "1000.5",
            "timestamp": 2000,
            "market": "futures",
        }

        cb = MagicMock()
        ds.on_kline(cb)
        ds._process_message(mock_message)

        cb.assert_called_once()
        kline = cb.call_args[0][0]
        assert isinstance(kline, MarketKline)
        assert kline.symbol == "BTCUSDT"
        assert kline.market == "futures"

    def test_process_matching_kline_with_subscription_delivers_callback(self):
        subscription = LiveKlineSubscription.from_symbols_with_market(
            [("BTCUSDT", "futures")],
            interval="1m",
            consumer_group="strategy-session-7-sess-123",
        )
        ds = LiveDataSource(config=KafkaConfig.for_live_kline_subscription(subscription))
        mock_message = MagicMock()
        mock_message.topic = "md.kline.binance.futures.1m"
        mock_message.value = {
            "symbol": "BTCUSDT",
            "interval": "1m",
            "open_time": 1000,
            "close_time": 2000,
            "open": "100.5",
            "high": "101.0",
            "low": "100.0",
            "close": "100.75",
            "volume": "1000.5",
            "timestamp": 2000,
            "market": "futures",
        }

        cb = MagicMock()
        ds.on_kline(cb)
        ds._process_message(mock_message)

        cb.assert_called_once()

    def test_process_non_matching_kline_with_subscription_is_discarded(self):
        subscription = LiveKlineSubscription.from_symbols_with_market(
            [("BTCUSDT", "futures")],
            interval="1m",
            consumer_group="strategy-session-7-sess-123",
        )
        ds = LiveDataSource(config=KafkaConfig.for_live_kline_subscription(subscription))
        mock_message = MagicMock()
        mock_message.topic = "md.kline.binance.futures.1m"
        mock_message.value = {
            "symbol": "ETHUSDT",
            "interval": "1m",
            "open_time": 1000,
            "close_time": 2000,
            "open": "100.5",
            "high": "101.0",
            "low": "100.0",
            "close": "100.75",
            "volume": "1000.5",
            "timestamp": 2000,
            "market": "futures",
        }

        cb = MagicMock()
        ds.on_kline(cb)
        ds._process_message(mock_message)

        cb.assert_not_called()

    def test_process_kline_message_preserves_market(self, kafka_config):
        ds = LiveDataSource(config=kafka_config)
        mock_message = MagicMock()
        mock_message.topic = "market-kline"
        mock_message.value = {
            "symbol": "BTCUSDT",
            "interval": "1m",
            "open_time": 1000,
            "close_time": 2000,
            "open": "100.5",
            "high": "101.0",
            "low": "100.0",
            "close": "100.75",
            "volume": "1000.5",
            "timestamp": 1500,
            "market": "spot",
        }

        cb = MagicMock()
        ds.on_kline(cb)
        ds._process_message(mock_message)

        cb.assert_called_once()
        kline = cb.call_args[0][0]
        assert isinstance(kline, MarketKline)
        assert kline.market == "spot"

    def test_process_oi_message(self, kafka_config):
        ds = LiveDataSource(config=kafka_config)
        mock_message = MagicMock()
        mock_message.topic = "market-oi"
        mock_message.value = {
            "symbol": "BTCUSDT",
            "open_interest": "50000.5",
            "period": "realtime",
            "timestamp": 1500,
        }
        
        cb = MagicMock()
        ds.on_oi(cb)
        ds._process_message(mock_message)
        
        cb.assert_called_once()
        oi = cb.call_args[0][0]
        assert isinstance(oi, MarketOI)
        assert oi.symbol == "BTCUSDT"

    def test_process_funding_message(self, kafka_config):
        ds = LiveDataSource(config=kafka_config)
        mock_message = MagicMock()
        mock_message.topic = "market-funding"
        mock_message.value = {
            "symbol": "BTCUSDT",
            "funding_rate": "0.0001",
            "mark_price": "50000.5",
            "next_funding_time": 2000,
            "timestamp": 1500,
        }
        
        cb = MagicMock()
        ds.on_funding(cb)
        ds._process_message(mock_message)
        
        cb.assert_called_once()
        funding = cb.call_args[0][0]
        assert isinstance(funding, MarketFunding)
        assert funding.symbol == "BTCUSDT"

    def test_process_orderbook_message(self, kafka_config):
        ds = LiveDataSource(config=kafka_config)
        mock_message = MagicMock()
        mock_message.topic = "market-orderbook"
        mock_message.value = {
            "symbol": "BTCUSDT",
            "bids": [{"price": "100.0", "quantity": "10.0"}],
            "asks": [{"price": "101.0", "quantity": "5.0"}],
            "timestamp": 1500,
        }
        
        cb = MagicMock()
        ds.on_orderbook(cb)
        ds._process_message(mock_message)
        
        cb.assert_called_once()
        ob = cb.call_args[0][0]
        assert isinstance(ob, MarketOrderBook)
        assert ob.symbol == "BTCUSDT"

    def test_process_malformed_message_logs_warning(self, kafka_config):
        ds = LiveDataSource(config=kafka_config)
        mock_message = MagicMock()
        mock_message.topic = "market-kline"
        mock_message.value = {
            "symbol": "BTCUSDT",
            "interval": "1m",
        }
        
        cb = MagicMock()
        ds.on_kline(cb)
        with patch("market_data.live.logger") as mock_logger:
            ds._process_message(mock_message)
            assert mock_logger.warning.called
            cb.assert_not_called()

    def test_process_unknown_topic_logs_warning(self, kafka_config):
        ds = LiveDataSource(config=kafka_config)
        mock_message = MagicMock()
        mock_message.topic = "unknown-topic"
        mock_message.value = {}
        
        with patch("market_data.live.logger") as mock_logger:
            ds._process_message(mock_message)
            mock_logger.warning.assert_called_once()


class TestLiveDataSourceStartStop:
    def test_start_when_already_running(self, kafka_config):
        ds = LiveDataSource(config=kafka_config)
        ds._running = True
        ds._consumer = MagicMock()
        
        ds.start()
        assert ds._running is True

    def test_stop_when_not_running(self, kafka_config):
        ds = LiveDataSource(config=kafka_config)
        ds._running = False
        ds.stop()
        assert ds._running is False

    def test_create_consumer_uses_explicit_consumer_group(self, monkeypatch):
        captured: dict = {}

        class FakeConsumer:
            pass

        def fake_kafka_consumer(**kwargs):
            captured["group_id"] = kwargs["group_id"]
            return FakeConsumer()

        class FakeConsumerMiddleware:
            def __init__(self, raw_consumer, consumer_group, logger):
                captured["middleware_group"] = consumer_group
                self._raw_consumer = raw_consumer

        monkeypatch.setitem(
            sys.modules,
            "kafka",
            types.SimpleNamespace(KafkaConsumer=fake_kafka_consumer),
        )
        monkeypatch.setitem(
            sys.modules,
            "utils.middleware.kafka",
            types.SimpleNamespace(KafkaConsumerMiddleware=FakeConsumerMiddleware),
        )

        subscription = LiveKlineSubscription.from_symbols_with_market(
            [("BTCUSDT", "futures")],
            interval="1m",
            consumer_group="strategy-session-7-sess-123",
        )
        ds = LiveDataSource(config=KafkaConfig.for_live_kline_subscription(subscription))

        ds._create_consumer()

        assert captured["group_id"] == "strategy-session-7-sess-123"
        assert captured["middleware_group"] == "strategy-session-7-sess-123"


class TestLiveDataSourceNotImplemented:
    def test_get_klines_raises_not_implemented(self, kafka_config):
        ds = LiveDataSource(config=kafka_config)
        with pytest.raises(NotImplementedError):
            ds.get_klines("BTCUSDT", "1m", 1000, 2000)

    def test_get_open_interest_raises_not_implemented(self, kafka_config):
        ds = LiveDataSource(config=kafka_config)
        with pytest.raises(NotImplementedError):
            ds.get_open_interest("BTCUSDT", 1000, 2000)

    def test_get_funding_rates_raises_not_implemented(self, kafka_config):
        ds = LiveDataSource(config=kafka_config)
        with pytest.raises(NotImplementedError):
            ds.get_funding_rates("BTCUSDT", 1000, 2000)

    def test_get_orderbook_raises_not_implemented(self, kafka_config):
        ds = LiveDataSource(config=kafka_config)
        with pytest.raises(NotImplementedError):
            ds.get_orderbook("BTCUSDT", 1000, 2000)
