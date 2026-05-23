import pandas as pd


def rsi(ohlc: pd.DataFrame, length: int = 14, column: str = "close") -> pd.Series:
    close = ohlc[column]
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()

    rs = avg_gain / avg_loss.where(avg_loss != 0)
    result = 100 - (100 / (1 + rs))
    result = result.where(avg_loss != 0, 100.0)
    result.name = f"rsi_{length}"
    return result


def cci(ohlc: pd.DataFrame, length: int = 14) -> pd.Series:
    typical_price = (ohlc["high"] + ohlc["low"] + ohlc["close"]) / 3.0
    mean_tp = typical_price.rolling(window=length, min_periods=length).mean()
    mean_dev = typical_price.rolling(window=length, min_periods=length).apply(
        lambda values: (values - values.mean()).abs().mean(),
        raw=False,
    )
    result = (typical_price - mean_tp) / (0.015 * mean_dev.where(mean_dev != 0))
    result.name = f"cci_{length}"
    return result
