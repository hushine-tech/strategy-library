import pytest
import pandas as pd
import numpy as np
from algo.bundle import IndicatorBundle


@pytest.fixture
def sample_ohlc():
    dates = pd.date_range("2024-01-01", periods=100, freq="D")
    data = {
        "open": np.random.uniform(100, 110, 100),
        "high": np.random.uniform(110, 120, 100),
        "low": np.random.uniform(90, 100, 100),
        "close": np.random.uniform(100, 110, 100),
        "volume": np.random.uniform(1000, 5000, 100),
    }
    return pd.DataFrame(data, index=dates)


def test_bundle_default_indicators():
    bundle = IndicatorBundle()
    assert bundle.indicators == ["sma", "ema", "rsi", "macd", "bollinger_bands", "atr", "dmi", "cci"]


def test_bundle_custom_indicators():
    bundle = IndicatorBundle(indicators=["sma", "rsi"])
    assert bundle.indicators == ["sma", "rsi"]


def test_bundle_compute_all(sample_ohlc):
    bundle = IndicatorBundle()
    result = bundle.compute(sample_ohlc)
    assert isinstance(result, pd.DataFrame)
    assert "sma" in result.columns
    assert "ema" in result.columns
    assert "rsi" in result.columns
    assert "macd_macd" in result.columns
    assert "bb_upper" in result.columns
    assert "atr" in result.columns
    assert "dmi_plus" in result.columns
    assert "cci" in result.columns


def test_bundle_compute_custom(sample_ohlc):
    bundle = IndicatorBundle(indicators=["sma", "rsi"])
    result = bundle.compute(sample_ohlc)
    assert isinstance(result, pd.DataFrame)
    assert "sma" in result.columns
    assert "rsi" in result.columns
    assert "ema" not in result.columns
