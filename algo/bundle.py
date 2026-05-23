import pandas as pd
from typing import List, Optional

from .indicators.trend import sma, ema, macd, dmi
from .indicators.momentum import rsi, cci
from .indicators.volatility import bollinger_bands, atr


class IndicatorBundle:
    def __init__(self, indicators: Optional[List[str]] = None):
        self.indicators = indicators or ["sma", "ema", "rsi", "macd", "bollinger_bands", "atr", "dmi", "cci"]

    def compute(self, ohlc: pd.DataFrame) -> pd.DataFrame:
        result = pd.DataFrame(index=ohlc.index)
        for indicator in self.indicators:
            if indicator == "sma":
                result["sma"] = sma(ohlc)
            elif indicator == "ema":
                result["ema"] = ema(ohlc)
            elif indicator == "rsi":
                result["rsi"] = rsi(ohlc)
            elif indicator == "macd":
                result = pd.concat([result, macd(ohlc)], axis=1)
            elif indicator == "bollinger_bands":
                result = pd.concat([result, bollinger_bands(ohlc)], axis=1)
            elif indicator == "atr":
                result["atr"] = atr(ohlc)
            elif indicator == "dmi":
                result = pd.concat([result, dmi(ohlc)], axis=1)
            elif indicator == "cci":
                result["cci"] = cci(ohlc)
        return result
