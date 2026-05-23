import pandas as pd


def _wilder_smooth(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def sma(ohlc: pd.DataFrame, length: int = 14, column: str = "close") -> pd.Series:
    result = ohlc[column].rolling(window=length, min_periods=length).mean()
    result.name = f"sma_{length}"
    return result


def ema(ohlc: pd.DataFrame, length: int = 14, column: str = "close") -> pd.Series:
    result = ohlc[column].ewm(span=length, adjust=False, min_periods=length).mean()
    result.name = f"ema_{length}"
    return result


def macd(
    ohlc: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    column: str = "close",
) -> pd.DataFrame:
    close = ohlc[column]
    fast_ema = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    slow_ema = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    hist = macd_line - signal_line
    return pd.DataFrame(
        {
            "macd_macd": macd_line,
            "macd_signal": signal_line,
            "macd_hist": hist,
        },
        index=ohlc.index,
    )


def dmi(ohlc: pd.DataFrame, length: int = 14) -> pd.DataFrame:
    high = ohlc["high"]
    low = ohlc["low"]
    close = ohlc["close"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0).fillna(0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0).fillna(0.0)

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = _wilder_smooth(tr, length)
    plus_di = 100 * _wilder_smooth(plus_dm, length) / atr
    minus_di = 100 * _wilder_smooth(minus_dm, length) / atr

    di_sum = plus_di + minus_di
    dx = 100 * (plus_di - minus_di).abs() / di_sum.where(di_sum != 0)
    adx = _wilder_smooth(dx, length)

    return pd.DataFrame(
        {
            "dmi_plus": plus_di,
            "dmi_minus": minus_di,
            "dmi_adx": adx,
        },
        index=ohlc.index,
    )
