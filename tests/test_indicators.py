import pytest
import pandas as pd
import numpy as np
from algo.indicators.trend import sma, ema, macd, dmi
from algo.indicators.momentum import rsi, cci
from algo.indicators.volatility import bollinger_bands, atr


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


def test_sma_default(sample_ohlc):
    result = sma(sample_ohlc)
    assert isinstance(result, pd.Series)
    assert len(result) == len(sample_ohlc)
    assert result.name is not None


def test_sma_custom_length(sample_ohlc):
    result = sma(sample_ohlc, length=20)
    assert isinstance(result, pd.Series)
    assert len(result) == len(sample_ohlc)


def test_ema_default(sample_ohlc):
    result = ema(sample_ohlc)
    assert isinstance(result, pd.Series)
    assert len(result) == len(sample_ohlc)


def test_macd_default(sample_ohlc):
    result = macd(sample_ohlc)
    assert isinstance(result, pd.DataFrame)
    assert set(result.columns) == {"macd_macd", "macd_signal", "macd_hist"}


def test_rsi_default(sample_ohlc):
    result = rsi(sample_ohlc)
    assert isinstance(result, pd.Series)
    assert len(result) == len(sample_ohlc)
    assert result.min() >= 0
    assert result.max() <= 100


def test_cci_default(sample_ohlc):
    result = cci(sample_ohlc)
    assert isinstance(result, pd.Series)
    assert len(result) == len(sample_ohlc)


def test_bollinger_bands_default(sample_ohlc):
    result = bollinger_bands(sample_ohlc)
    assert isinstance(result, pd.DataFrame)
    assert set(result.columns) == {"bb_upper", "bb_middle", "bb_lower"}


def test_atr_default(sample_ohlc):
    result = atr(sample_ohlc)
    assert isinstance(result, pd.Series)
    assert len(result) == len(sample_ohlc)


def test_dmi_default(sample_ohlc):
    result = dmi(sample_ohlc)
    assert isinstance(result, pd.DataFrame)
    assert set(result.columns) == {"dmi_plus", "dmi_minus", "dmi_adx"}
