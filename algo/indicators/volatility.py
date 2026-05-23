import pandas as pd


def bollinger_bands(
    ohlc: pd.DataFrame, length: int = 20, std: float = 2.0, column: str = "close"
) -> pd.DataFrame:
    series = ohlc[column]
    middle = series.rolling(window=length, min_periods=length).mean()
    deviation = series.rolling(window=length, min_periods=length).std(ddof=0)
    upper = middle + std * deviation
    lower = middle - std * deviation
    return pd.DataFrame(
        {
            "bb_lower": lower,
            "bb_middle": middle,
            "bb_upper": upper,
        },
        index=ohlc.index,
    )


def atr(ohlc: pd.DataFrame, length: int = 14) -> pd.Series:
    high = ohlc["high"]
    low = ohlc["low"]
    close = ohlc["close"]
    prev_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    result = true_range.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    result.name = f"atr_{length}"
    return result
