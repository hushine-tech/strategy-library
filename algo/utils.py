import pandas as pd
from typing import Union


def to_dataframe(*series_list: pd.Series) -> pd.DataFrame:
    return pd.concat(series_list, axis=1)


def get_column(ohlc: pd.DataFrame, column: str) -> pd.Series:
    return ohlc[column]
