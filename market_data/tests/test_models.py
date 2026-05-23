"""
Unit tests for market data models.
"""
import pytest
from market_data.models import PriceLevel, MarketKline, MarketOI, MarketFunding, MarketOrderBook


class TestPriceLevel:
    def test_from_dict(self):
        data = {"price": "100.5", "quantity": "10.25"}
        pl = PriceLevel.from_dict(data)
        assert pl.price == 100.5
        assert pl.quantity == 10.25

    def test_to_dict(self):
        pl = PriceLevel(price=100.5, quantity=10.25)
        result = pl.to_dict()
        assert result == {"price": 100.5, "quantity": 10.25}

    def test_from_dict_integer_values(self):
        data = {"price": 100, "quantity": 10}
        pl = PriceLevel.from_dict(data)
        assert pl.price == 100.0
        assert pl.quantity == 10.0


class TestMarketKline:
    def test_from_dict(self):
        data = {
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
        kline = MarketKline.from_dict(data)
        assert kline.symbol == "BTCUSDT"
        assert kline.interval == "1m"
        assert kline.open_time == 1000
        assert kline.close_time == 2000
        assert kline.open == 100.5
        assert kline.high == 101.0
        assert kline.low == 100.0
        assert kline.close == 100.75
        assert kline.volume == 1000.5
        assert kline.timestamp == 1500
        assert kline.market is None

    def test_from_dict_with_optional_market(self):
        data = {
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
            "market": "SPOT",
        }
        kline = MarketKline.from_dict(data)
        assert kline.market == "spot"

    def test_from_dict_missing_field(self):
        data = {
            "symbol": "BTCUSDT",
            "interval": "1m",
            "open_time": 1000,
            "close_time": 2000,
            "open": "100.5",
            "high": "101.0",
            "low": "100.0",
            "close": "100.75",
            "volume": "1000.5",
        }
        with pytest.raises(ValueError, match="Missing required field"):
            MarketKline.from_dict(data)

    def test_to_dict(self):
        kline = MarketKline(
            symbol="BTCUSDT",
            interval="1m",
            open_time=1000,
            close_time=2000,
            open=100.5,
            high=101.0,
            low=100.0,
            close=100.75,
            volume=1000.5,
            timestamp=1500,
        )
        result = kline.to_dict()
        assert result["symbol"] == "BTCUSDT"
        assert result["interval"] == "1m"
        assert result["open_time"] == 1000
        assert result["close_time"] == 2000
        assert result["open"] == 100.5
        assert result["high"] == 101.0
        assert result["low"] == 100.0
        assert result["close"] == 100.75
        assert result["volume"] == 1000.5
        assert result["timestamp"] == 1500
        assert "market" not in result

    def test_to_dict_includes_market_when_present(self):
        kline = MarketKline(
            symbol="BTCUSDT",
            interval="1m",
            open_time=1000,
            close_time=2000,
            open=100.5,
            high=101.0,
            low=100.0,
            close=100.75,
            volume=1000.5,
            timestamp=1500,
            market="spot",
        )
        result = kline.to_dict()
        assert result["market"] == "spot"


class TestMarketOI:
    def test_from_dict(self):
        data = {
            "symbol": "BTCUSDT",
            "open_interest": "50000.5",
            "period": "realtime",
            "timestamp": 1500,
        }
        oi = MarketOI.from_dict(data)
        assert oi.symbol == "BTCUSDT"
        assert oi.open_interest == 50000.5
        assert oi.period == "realtime"
        assert oi.timestamp == 1500

    def test_from_dict_missing_field(self):
        data = {
            "symbol": "BTCUSDT",
            "open_interest": "50000.5",
            "timestamp": 1500,
        }
        with pytest.raises(ValueError, match="Missing required field"):
            MarketOI.from_dict(data)

    def test_to_dict(self):
        oi = MarketOI(
            symbol="BTCUSDT",
            open_interest=50000.5,
            period="4h",
            timestamp=1500,
        )
        result = oi.to_dict()
        assert result == {
            "symbol": "BTCUSDT",
            "open_interest": 50000.5,
            "period": "4h",
            "timestamp": 1500,
        }


class TestMarketFunding:
    def test_from_dict(self):
        data = {
            "symbol": "BTCUSDT",
            "funding_rate": "0.0001",
            "mark_price": "50000.5",
            "next_funding_time": 2000,
            "timestamp": 1500,
        }
        funding = MarketFunding.from_dict(data)
        assert funding.symbol == "BTCUSDT"
        assert funding.funding_rate == 0.0001
        assert funding.mark_price == 50000.5
        assert funding.next_funding_time == 2000
        assert funding.timestamp == 1500

    def test_from_dict_missing_field(self):
        data = {
            "symbol": "BTCUSDT",
            "funding_rate": "0.0001",
            "mark_price": "50000.5",
            "timestamp": 1500,
        }
        with pytest.raises(ValueError, match="Missing required field"):
            MarketFunding.from_dict(data)

    def test_to_dict(self):
        funding = MarketFunding(
            symbol="BTCUSDT",
            funding_rate=0.0001,
            mark_price=50000.5,
            next_funding_time=2000,
            timestamp=1500,
        )
        result = funding.to_dict()
        assert result == {
            "symbol": "BTCUSDT",
            "funding_rate": 0.0001,
            "mark_price": 50000.5,
            "next_funding_time": 2000,
            "timestamp": 1500,
        }


class TestMarketOrderBook:
    def test_from_dict(self):
        data = {
            "symbol": "BTCUSDT",
            "bids": [{"price": "100.0", "quantity": "10.0"}],
            "asks": [{"price": "101.0", "quantity": "5.0"}],
            "timestamp": 1500,
        }
        ob = MarketOrderBook.from_dict(data)
        assert ob.symbol == "BTCUSDT"
        assert len(ob.bids) == 1
        assert ob.bids[0].price == 100.0
        assert ob.bids[0].quantity == 10.0
        assert len(ob.asks) == 1
        assert ob.asks[0].price == 101.0
        assert ob.asks[0].quantity == 5.0
        assert ob.timestamp == 1500

    def test_from_dict_with_price_level_objects(self):
        bids = [PriceLevel(price=100.0, quantity=10.0)]
        asks = [PriceLevel(price=101.0, quantity=5.0)]
        data = {
            "symbol": "BTCUSDT",
            "bids": bids,
            "asks": asks,
            "timestamp": 1500,
        }
        ob = MarketOrderBook.from_dict(data)
        assert len(ob.bids) == 1
        assert ob.bids[0].price == 100.0

    def test_from_dict_missing_field(self):
        data = {
            "symbol": "BTCUSDT",
            "bids": [{"price": "100.0", "quantity": "10.0"}],
            "timestamp": 1500,
        }
        with pytest.raises(ValueError, match="Missing required field"):
            MarketOrderBook.from_dict(data)

    def test_to_dict(self):
        ob = MarketOrderBook(
            symbol="BTCUSDT",
            bids=[PriceLevel(price=100.0, quantity=10.0)],
            asks=[PriceLevel(price=101.0, quantity=5.0)],
            timestamp=1500,
        )
        result = ob.to_dict()
        assert result["symbol"] == "BTCUSDT"
        assert len(result["bids"]) == 1
        assert result["bids"][0] == {"price": 100.0, "quantity": 10.0}
        assert result["timestamp"] == 1500
