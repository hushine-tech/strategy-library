"""
Unit tests for BacktestDataSource.
"""
import pytest
import datetime
from unittest.mock import MagicMock, patch
from market_data.backtest import BacktestDataSource
from market_data.config import TimescaleConfig


class TestBacktestDataSource:
    def test_init_with_config(self, timescale_config):
        ds = BacktestDataSource(config=timescale_config)
        assert ds._config == timescale_config
        assert ds._conns == {}

    def test_ensure_connection_creates_new_connection(self, timescale_config):
        ds = BacktestDataSource(config=timescale_config)
        with patch("market_data.backtest.psycopg2.connect") as mock_connect:
            mock_conn = MagicMock()
            mock_conn.closed = False
            mock_connect.return_value = mock_conn
            conn = ds._ensure_connection()
            assert conn == mock_conn
            mock_connect.assert_called_once_with(
                host="localhost",
                port=5432,
                database="test_db",
                user="test_user",
                password="test_pass",
            )
            assert ds._conns == {"test_db": mock_conn}

    def test_ensure_connection_reuses_existing_connection(self, timescale_config, mock_conn):
        ds = BacktestDataSource(config=timescale_config)
        ds._conns["test_db"] = mock_conn
        conn = ds._ensure_connection()
        assert conn == mock_conn

    def test_close(self, timescale_config, mock_conn):
        mock_conn.closed = False
        ds = BacktestDataSource(config=timescale_config)
        ds._conns["test_db"] = mock_conn
        ds.close()
        mock_conn.close.assert_called_once()
        assert ds._conns == {}

    def test_context_manager(self, timescale_config):
        ds = BacktestDataSource(config=timescale_config)
        with patch.object(ds, "close") as mock_close:
            with ds as ctx:
                assert ctx == ds
            mock_close.assert_called_once()


class TestBacktestDataSourceGetKlines:
    def test_get_klines_futures(self, timescale_config, mock_conn, mock_cursor):
        ds = BacktestDataSource(config=timescale_config)
        ds._conns["test_db"] = mock_conn
        
        kline_row = {
            "symbol": "BTCUSDT",
            "open_time": datetime.datetime.fromtimestamp(1, datetime.UTC),
            "close_time": datetime.datetime.fromtimestamp(2, datetime.UTC),
            "open": 100.5,
            "high": 101.0,
            "low": 100.0,
            "close": 100.75,
            "volume": 1000.5,
        }
        mock_cursor.__iter__ = MagicMock(return_value=iter([kline_row]))

        klines = list(ds.get_klines("BTCUSDT", "1m", 1000, 2000, market="futures"))
        assert len(klines) == 1
        assert klines[0].symbol == "BTCUSDT"
        query = mock_cursor.execute.call_args.args[0]
        params = mock_cursor.execute.call_args.args[1]
        assert "futures_klines_btcusdt_1m" in repr(query)
        assert "to_timestamp" in repr(query)
        assert "open_time <" in repr(query)
        assert params == ("BTCUSDT", 1000, 2000)
        assert klines[0].interval == "1m"
        assert klines[0].timestamp == 2000
        mock_cursor.close.assert_called_once()

    def test_get_klines_spot(self, timescale_config, mock_conn, mock_cursor):
        ds = BacktestDataSource(config=timescale_config)
        ds._conns["test_db"] = mock_conn
        mock_cursor.__iter__ = MagicMock(return_value=iter([]))

        list(ds.get_klines("BTCUSDT", "1m", 1000, 2000, market="spot"))
        mock_cursor.execute.assert_called_once()
        query = mock_cursor.execute.call_args.args[0]
        assert "spot_klines_btcusdt_1m" in repr(query)

    def test_has_kline_coverage_returns_true_for_complete_window(self, timescale_config, mock_conn):
        ds = BacktestDataSource(config=timescale_config)
        ds._conns["test_db"] = mock_conn

        cursor = MagicMock()
        cursor.fetchall.return_value = [
            (datetime.datetime.fromtimestamp(60, datetime.UTC),),
            (datetime.datetime.fromtimestamp(120, datetime.UTC),),
        ]
        mock_conn.cursor.return_value = cursor

        assert ds.has_kline_coverage("BTCUSDT", "1m", 60000, 180000, market="futures") is True
        cursor.execute.assert_called_once()
        query = cursor.execute.call_args.args[0]
        params = cursor.execute.call_args.args[1]
        assert "open_time <" in repr(query)
        assert params == ("BTCUSDT", 60000, 180000)

    def test_has_kline_coverage_returns_false_when_count_is_short(self, timescale_config, mock_conn):
        ds = BacktestDataSource(config=timescale_config)
        ds._conns["test_db"] = mock_conn

        cursor = MagicMock()
        cursor.fetchall.return_value = [(datetime.datetime.fromtimestamp(60, datetime.UTC),)]
        mock_conn.cursor.return_value = cursor

        assert ds.has_kline_coverage("BTCUSDT", "1m", 60000, 180000, market="futures") is False

    def test_has_kline_coverage_returns_false_when_window_has_gap(self, timescale_config, mock_conn):
        ds = BacktestDataSource(config=timescale_config)
        ds._conns["test_db"] = mock_conn

        cursor = MagicMock()
        cursor.fetchall.return_value = [
            (datetime.datetime.fromtimestamp(60, datetime.UTC),),
            (datetime.datetime.fromtimestamp(180, datetime.UTC),),
        ]
        mock_conn.cursor.return_value = cursor

        assert ds.has_kline_coverage("BTCUSDT", "1m", 60000, 180000, market="futures") is False

    def test_years_in_range_uses_end_exclusive_boundary(self):
        start = int(datetime.datetime(2026, 12, 31, 23, 59, tzinfo=datetime.UTC).timestamp() * 1000)
        end = int(datetime.datetime(2027, 1, 1, 0, 0, tzinfo=datetime.UTC).timestamp() * 1000)

        assert BacktestDataSource._years_in_range(start, end) == [2026]


class TestBacktestDataSourceGetOpenInterest:
    def test_get_open_interest(self, timescale_config, mock_conn, mock_cursor):
        mock_cursor.__iter__ = MagicMock(return_value=iter([
            {
                "symbol": "BTCUSDT",
                "open_interest": 50000.5,
                "period": "realtime",
                "time": datetime.datetime.fromtimestamp(1.5, datetime.UTC),
            }
        ]))
        
        ds = BacktestDataSource(config=timescale_config)
        ds._conns["test_db"] = mock_conn

        oi_list = list(ds.get_open_interest("BTCUSDT", 1000, 2000))
        assert len(oi_list) == 1
        assert oi_list[0].symbol == "BTCUSDT"
        assert oi_list[0].open_interest == 50000.5
        assert oi_list[0].timestamp == 1500
        query = mock_cursor.execute.call_args.args[0]
        assert "futures_open_interest_btcusdt" in repr(query)
        assert " time" in repr(query)


class TestBacktestDataSourceGetFundingRates:
    def test_get_funding_rates(self, timescale_config, mock_conn, mock_cursor):
        mock_cursor.__iter__ = MagicMock(return_value=iter([
            {
                "symbol": "BTCUSDT",
                "funding_rate": 0.0001,
                "mark_price": 50000.5,
                "next_funding_time": 2000,
                "time": datetime.datetime.fromtimestamp(1.5, datetime.UTC),
            }
        ]))
        
        ds = BacktestDataSource(config=timescale_config)
        ds._conns["test_db"] = mock_conn

        funding_list = list(ds.get_funding_rates("BTCUSDT", 1000, 2000))
        assert len(funding_list) == 1
        assert funding_list[0].symbol == "BTCUSDT"
        assert funding_list[0].funding_rate == 0.0001
        assert funding_list[0].timestamp == 1500
        query = mock_cursor.execute.call_args.args[0]
        assert "futures_funding_rates_btcusdt" in repr(query)
        assert " time" in repr(query)


class TestBacktestDataSourceGetOrderbook:
    def test_get_orderbook_futures(self, timescale_config, mock_conn, mock_cursor):
        mock_cursor.__iter__ = MagicMock(return_value=iter([
            {
                "symbol": "BTCUSDT",
                "bids": [[100.0, 10.0]],
                "asks": [[101.0, 5.0]],
                "time": datetime.datetime.fromtimestamp(1.5, datetime.UTC),
            }
        ]))
        
        ds = BacktestDataSource(config=timescale_config)
        ds._conns["test_db"] = mock_conn

        ob_list = list(ds.get_orderbook("BTCUSDT", 1000, 2000, market="futures"))
        assert len(ob_list) == 1
        assert ob_list[0].symbol == "BTCUSDT"
        assert len(ob_list[0].bids) == 1
        assert ob_list[0].bids[0].price == 100.0
        assert ob_list[0].timestamp == 1500
        query = mock_cursor.execute.call_args.args[0]
        assert "futures_orderbook_btcusdt" in repr(query)
        assert " time" in repr(query)

    def test_get_orderbook_spot(self, timescale_config, mock_conn, mock_cursor):
        mock_cursor.__iter__ = MagicMock(return_value=iter([]))
        
        ds = BacktestDataSource(config=timescale_config)
        ds._conns["test_db"] = mock_conn

        list(ds.get_orderbook("BTCUSDT", 1000, 2000, market="spot"))
        mock_cursor.execute.assert_called_once()
        query = mock_cursor.execute.call_args.args[0]
        assert "spot_orderbook_btcusdt" in repr(query)
