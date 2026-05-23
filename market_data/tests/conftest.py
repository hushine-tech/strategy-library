"""
Shared pytest fixtures for market_data tests.
"""
import pytest
from unittest.mock import MagicMock
from market_data.config import TimescaleConfig, KafkaConfig, KafkaBrokerConfig


@pytest.fixture
def mock_conn():
    conn = MagicMock()
    conn.closed = False
    return conn


@pytest.fixture
def mock_cursor(mock_conn):
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = cursor
    return cursor


@pytest.fixture
def kafka_config():
    return KafkaConfig(
        brokers=[KafkaBrokerConfig(host="localhost", port=9092)],
        consumer_group="test-group",
        auto_offset_reset="latest",
        enable_auto_commit=True,
        topics=["md.kline.binance.futures.1m"],
    )


@pytest.fixture
def timescale_config():
    return TimescaleConfig(
        host="localhost",
        port=5432,
        database="test_db",
        user="test_user",
        password="test_pass",
    )
