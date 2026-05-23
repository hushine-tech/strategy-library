"""
BacktestDataSource - TimescaleDB-backed historical market data queries.

Uses psycopg2 to connect to TimescaleDB and provides generator-based
iteration over historical market data for backtesting purposes.
"""
import datetime
import logging
from typing import Generator, Optional

from .base import DataSource
from .config import TimescaleConfig
from .models import MarketKline, MarketOI, MarketFunding, MarketOrderBook, PriceLevel

logger = logging.getLogger(__name__)

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor


class BacktestDataSource(DataSource):
    """TimescaleDB-backed data source for historical market data.

    支持按年份分库（database="binance_{year}"）和按 interval 分表。
    """

    def __init__(self, config: Optional[TimescaleConfig] = None):
        if config is None:
            config = self._load_config()
        self._config = config
        self._conns: dict[str, object] = {}  # db_name → connection

    def _load_config(self) -> TimescaleConfig:
        try:
            import yaml
            with open("config.yaml", "r") as f:
                config_data = yaml.safe_load(f)
            timescale_data = config_data.get("market_data", {}).get("timescale", {})
            return TimescaleConfig.from_dict(timescale_data)
        except Exception as e:
            logger.warning(f"Failed to load config from file: {e}, using defaults")
            return TimescaleConfig()

    def _get_connection(self, database: str) -> object:
        """获取指定数据库的连接（带连接池缓存）。"""
        conn = self._conns.get(database)
        if conn is not None and not conn.closed:
            return conn
        conn = psycopg2.connect(
            host=self._config.host,
            port=self._config.port,
            database=database,
            user=self._config.user,
            password=self._config.password,
        )
        self._conns[database] = conn
        return conn

    def _ensure_connection(self) -> object:
        """兼容旧调用 — 连接到 config 中配置的默认数据库。"""
        db = self._config.database
        if "{year}" in db:
            db = db.replace("{year}", str(datetime.datetime.now(datetime.UTC).year))
        return self._get_connection(db)

    def close(self) -> None:
        for conn in self._conns.values():
            if conn is not None and not conn.closed:
                conn.close()
        self._conns.clear()

    def __enter__(self) -> "BacktestDataSource":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    @staticmethod
    def _table_name_kline(market: str, symbol: str, interval: str) -> str:
        """K 线表名：{market}_klines_{symbol}_{interval}"""
        m = "futures" if market == "futures" else "spot"
        return f"{m}_klines_{symbol.lower()}_{interval.lower()}"

    @staticmethod
    def _table_name_other(market: str, datatype: str, symbol: str) -> str:
        """非 K 线表名：{market}_{datatype}_{symbol}"""
        m = "futures" if market == "futures" else "spot"
        return f"{m}_{datatype}_{symbol.lower()}"

    @staticmethod
    def _years_in_range(start_time: int, end_time: int) -> list[int]:
        if end_time <= start_time:
            return []
        start_year = datetime.datetime.fromtimestamp(start_time / 1000, datetime.UTC).year
        end_year = datetime.datetime.fromtimestamp((end_time - 1) / 1000, datetime.UTC).year
        return list(range(start_year, end_year + 1))

    @staticmethod
    def _interval_ms(interval: str) -> int:
        interval = str(interval).strip()
        if len(interval) < 2:
            raise ValueError(f"invalid interval: {interval!r}")
        value = int(interval[:-1])
        unit = interval[-1]
        multipliers = {
            "s": 1_000,
            "m": 60_000,
            "h": 3_600_000,
            "d": 86_400_000,
            "w": 7 * 86_400_000,
            "M": 30 * 86_400_000,
        }
        if value <= 0 or unit not in multipliers:
            raise ValueError(f"invalid interval: {interval!r}")
        return value * multipliers[unit]

    # 保留向后兼容：旧的分区表名格式（{prefix}_{symbol}_{year}）
    @staticmethod
    def _partition_table_names(base_prefix: str, symbol: str, start_time: int, end_time: int) -> list[str]:
        symbol_part = symbol.lower()
        start_year = datetime.datetime.fromtimestamp(start_time / 1000, datetime.UTC).year
        end_year = datetime.datetime.fromtimestamp(end_time / 1000, datetime.UTC).year
        return [f"{base_prefix}_{symbol_part}_{year}" for year in range(start_year, end_year + 1)]

    def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        market: str = "futures",
    ) -> Generator[MarketKline, None, None]:
        """Fetch kline data from TimescaleDB.

        Args:
            symbol: Trading pair symbol (e.g., "BTCUSDT")
            interval: Kline interval (e.g., "1m", "5m", "1h", "4h", "1d")
            start_time: Start time as Unix timestamp in milliseconds
            end_time: End time as Unix timestamp in milliseconds
            market: Market type ("futures" or "spot")

        Yields:
            MarketKline instances
        """
        table = self._table_name_kline(market, symbol, interval)
        years = self._years_in_range(start_time, end_time)

        for year in years:
            db_name = self._config.database_for_year(year)
            conn = self._get_connection(db_name)
            query = sql.SQL("""
                SELECT symbol, open_time, close_time,
                       open, high, low, close, volume
                FROM {table}
                WHERE symbol = %s
                  AND open_time >= to_timestamp(%s/1000.0)
                  AND open_time < to_timestamp(%s/1000.0)
                ORDER BY open_time ASC
            """).format(table=sql.Identifier(table))

            cursor = conn.cursor(cursor_factory=RealDictCursor)
            try:
                cursor.execute(query, (symbol, start_time, end_time))
                for row in cursor:
                    yield MarketKline(
                        symbol=str(row["symbol"]),
                        interval=interval,
                        open_time=int(row["open_time"].timestamp() * 1000),
                        close_time=int(row["close_time"].timestamp() * 1000),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                        timestamp=int(row["close_time"].timestamp() * 1000),
                    )
            finally:
                cursor.close()

    def has_kline_coverage(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        market: str = "futures",
    ) -> bool:
        """Return True only when the full requested kline window is readable.

        Coverage is evaluated per year-sharded database using the end-exclusive
        ``[start_time, end_time)`` window and exact ``open_time`` continuity.

        This is stricter than "at least one row exists" and aligns mode=0
        preflight with the historical request readiness contract.
        """
        table = self._table_name_kline(market, symbol, interval)
        interval_ms = self._interval_ms(interval)

        if end_time <= start_time:
            return False
        if start_time % interval_ms != 0 or end_time % interval_ms != 0:
            return False

        for year in self._years_in_range(start_time, end_time):
            db_name = self._config.database_for_year(year)
            conn = self._get_connection(db_name)

            year_start = int(datetime.datetime(year, 1, 1, tzinfo=datetime.UTC).timestamp() * 1000)
            next_year_start = int(datetime.datetime(year + 1, 1, 1, tzinfo=datetime.UTC).timestamp() * 1000)
            segment_start = max(start_time, year_start)
            segment_end = min(end_time, next_year_start)
            if segment_end <= segment_start:
                continue
            if (segment_end - segment_start) % interval_ms != 0:
                return False
            expected_count = (segment_end - segment_start) // interval_ms

            query = sql.SQL("""
                SELECT open_time
                FROM {table}
                WHERE symbol = %s
                  AND open_time >= to_timestamp(%s/1000.0)
                  AND open_time < to_timestamp(%s/1000.0)
                ORDER BY open_time ASC
            """).format(table=sql.Identifier(table))

            cursor = conn.cursor()
            try:
                cursor.execute(query, (symbol, segment_start, segment_end))
                rows = cursor.fetchall()
            finally:
                cursor.close()

            if len(rows) != int(expected_count):
                return False
            for idx, row in enumerate(rows):
                open_time = row[0] if isinstance(row, (tuple, list)) else row["open_time"]
                open_ms = int(open_time.timestamp() * 1000)
                if open_ms != segment_start + (idx * interval_ms):
                    return False

        return True

    def get_open_interest(
        self,
        symbol: str,
        start_time: int,
        end_time: int,
    ) -> Generator[MarketOI, None, None]:
        """Fetch open interest data from TimescaleDB.

        Args:
            symbol: Trading pair symbol (e.g., "BTCUSDT")
            start_time: Start time as Unix timestamp in milliseconds
            end_time: End time as Unix timestamp in milliseconds

        Yields:
            MarketOI instances
        """
        table = self._table_name_other("futures", "open_interest", symbol)
        years = self._years_in_range(start_time, end_time)

        for year in years:
            db_name = self._config.database_for_year(year)
            conn = self._get_connection(db_name)
            query = sql.SQL("""
                SELECT symbol, open_interest, period, time
                FROM {table}
                WHERE symbol = %s
                  AND time >= to_timestamp(%s/1000.0)
                  AND time <= to_timestamp(%s/1000.0)
                ORDER BY time ASC
            """).format(table=sql.Identifier(table))

            cursor = conn.cursor(cursor_factory=RealDictCursor)
            try:
                cursor.execute(query, (symbol, start_time, end_time))
                for row in cursor:
                    yield MarketOI(
                        symbol=str(row["symbol"]),
                        open_interest=float(row["open_interest"]),
                        period=str(row["period"]),
                        timestamp=int(row["time"].timestamp() * 1000),
                    )
            finally:
                cursor.close()

    def get_funding_rates(
        self,
        symbol: str,
        start_time: int,
        end_time: int,
    ) -> Generator[MarketFunding, None, None]:
        """Fetch funding rate data from TimescaleDB.

        Args:
            symbol: Trading pair symbol (e.g., "BTCUSDT")
            start_time: Start time as Unix timestamp in milliseconds
            end_time: End time as Unix timestamp in milliseconds

        Yields:
            MarketFunding instances
        """
        table = self._table_name_other("futures", "funding_rates", symbol)
        years = self._years_in_range(start_time, end_time)

        for year in years:
            db_name = self._config.database_for_year(year)
            conn = self._get_connection(db_name)
            query = sql.SQL("""
                SELECT symbol, funding_rate, mark_price, next_funding_time, time
                FROM {table}
                WHERE symbol = %s
                  AND time >= to_timestamp(%s/1000.0)
                  AND time <= to_timestamp(%s/1000.0)
                ORDER BY time ASC
            """).format(table=sql.Identifier(table))

            cursor = conn.cursor(cursor_factory=RealDictCursor)
            try:
                cursor.execute(query, (symbol, start_time, end_time))
                for row in cursor:
                    yield MarketFunding(
                        symbol=str(row["symbol"]),
                        funding_rate=float(row["funding_rate"]),
                        mark_price=float(row["mark_price"]),
                        next_funding_time=(
                            int(row["next_funding_time"].timestamp() * 1000)
                            if isinstance(row["next_funding_time"], datetime.datetime)
                            else int(row["next_funding_time"])
                        ),
                        timestamp=int(row["time"].timestamp() * 1000),
                    )
            finally:
                cursor.close()

    def get_orderbook(
        self,
        symbol: str,
        start_time: int,
        end_time: int,
        market: str = "futures",
    ) -> Generator[MarketOrderBook, None, None]:
        """Fetch orderbook data from TimescaleDB.

        Args:
            symbol: Trading pair symbol (e.g., "BTCUSDT")
            start_time: Start time as Unix timestamp in milliseconds
            end_time: End time as Unix timestamp in milliseconds
            market: Market type ("futures" or "spot")

        Yields:
            MarketOrderBook instances
        """
        table = self._table_name_other(market, "orderbook", symbol)
        years = self._years_in_range(start_time, end_time)

        for year in years:
            db_name = self._config.database_for_year(year)
            conn = self._get_connection(db_name)
            query = sql.SQL("""
                SELECT symbol, bids, asks, time
                FROM {table}
                WHERE symbol = %s
                  AND time >= to_timestamp(%s/1000.0)
                  AND time <= to_timestamp(%s/1000.0)
                ORDER BY time ASC
            """).format(table=sql.Identifier(table))

            cursor = conn.cursor(cursor_factory=RealDictCursor)
            try:
                cursor.execute(query, (symbol, start_time, end_time))
                for row in cursor:
                    bids = [
                        PriceLevel(price=float(b[0]), quantity=float(b[1]))
                        for b in (row["bids"] or [])
                    ]
                    asks = [
                        PriceLevel(price=float(a[0]), quantity=float(a[1]))
                        for a in (row["asks"] or [])
                    ]
                    yield MarketOrderBook(
                        symbol=str(row["symbol"]),
                        bids=bids,
                        asks=asks,
                        timestamp=int(row["time"].timestamp() * 1000),
                    )
            finally:
                cursor.close()
