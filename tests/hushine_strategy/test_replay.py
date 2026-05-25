import pytest

from hushine_strategy import OrderDecision
from hushine_strategy.replay.engine import ReplayConfig, run_replay
from hushine_strategy.types import MarketData
from hushine_strategy.wallet.futures import FuturesWallet


STRATEGY_CODE = """
from __future__ import annotations
from hushine_strategy import OrderDecision

class MyStrategy:
    INPUTS = [{"market": "futures", "symbol": "BTCUSDT", "interval": "1m"}]

    def __init__(self):
        self.done = False

    def on_market_data(self, data, wallet):
        tick = data.market["futures"].symbol["BTCUSDT"].interval["1m"]
        if tick and not self.done:
            self.done = True
            return OrderDecision(symbol="BTCUSDT", side="LONG", qty=0.01, market="futures")
        return None
"""


UNDECLARED_TICK_STRATEGY_CODE = """
class MyStrategy:
    INPUTS = [{"market": "futures", "symbol": "BTCUSDT", "interval": "1m"}]

    def on_market_data(self, data, wallet):
        1 / 0
"""


SMUGGLED_IMPORT_STRATEGY_CODE = """
from pandas.io.common import os

class MyStrategy:
    INPUTS = [{"market": "futures", "symbol": "BTCUSDT", "interval": "1m"}]

    def on_market_data(self, data, wallet):
        self.leaked = os.name
        return None
"""


SMUGGLED_DOTTED_MODULE_STRATEGY_CODE = """
import pandas.io.common as common

class MyStrategy:
    INPUTS = [{"market": "futures", "symbol": "BTCUSDT", "interval": "1m"}]

    def on_market_data(self, data, wallet):
        1 / 0
        self.leaked = common.os.name
        return None
"""


SMUGGLED_FROMLIST_MODULE_STRATEGY_CODE = """
from pandas.io import common

class MyStrategy:
    INPUTS = [{"market": "futures", "symbol": "BTCUSDT", "interval": "1m"}]

    def on_market_data(self, data, wallet):
        1 / 0
        self.leaked = common.os.name
        return None
"""


NUMPY_IMPORT_STRATEGY_CODE = """
import numpy as np
from hushine_strategy import OrderDecision

class MyStrategy:
    INPUTS = [{"market": "futures", "symbol": "BTCUSDT", "interval": "1m"}]

    def on_market_data(self, data, wallet):
        qty = float(np.array([0.01])[0])
        return OrderDecision(symbol="BTCUSDT", side="LONG", qty=qty, market="futures")
"""


ALLOWED_IMPORTS_STRATEGY_CODE = """
import numpy as np
import pandas as pd
from pandas import DataFrame
from hushine_strategy import OrderDecision

class MyStrategy:
    INPUTS = [{"market": "futures", "symbol": "BTCUSDT", "interval": "1m"}]

    def on_market_data(self, data, wallet):
        frame = DataFrame(pd.Series(np.array([0.01])), columns=["qty"])
        return OrderDecision(symbol="BTCUSDT", side="LONG", qty=float(frame["qty"].iloc[0]), market="futures")
"""


SMUGGLED_ROOT_MODULE_ATTRIBUTE_STRATEGY_CODE = """
import pandas as pd

class MyStrategy:
    INPUTS = [{"market": "futures", "symbol": "BTCUSDT", "interval": "1m"}]

    def on_market_data(self, data, wallet):
        self.leaked = pd.io.common.os.listdir(".")
        return None
"""


SMUGGLED_NUMPY_BUILTINS_STRATEGY_CODE = """
import numpy as np

class MyStrategy:
    INPUTS = [{"market": "futures", "symbol": "BTCUSDT", "interval": "1m"}]

    def on_market_data(self, data, wallet):
        self.leaked = np.__builtins__["__import__"]("os")
        return None
"""


SMUGGLED_HUSHINE_FORBIDDEN_EXPORT_STRATEGY_CODE = """
from hushine_strategy.notifier import Path

class MyStrategy:
    INPUTS = [{"market": "futures", "symbol": "BTCUSDT", "interval": "1m"}]

    def on_market_data(self, data, wallet):
        self.leaked = Path(".").exists()
        return None
"""


def test_replay_processes_ticks_and_fills_local_order():
    ticks = [
        MarketData(symbol="BTCUSDT", price=100.0, timestamp=1, market="futures", interval="1m"),
        MarketData(symbol="BTCUSDT", price=101.0, timestamp=2, market="futures", interval="1m"),
    ]
    wallet = FuturesWallet(initial_balance=1000.0)
    result = run_replay(ReplayConfig(strategy_code=STRATEGY_CODE, ticks=ticks, wallet=wallet))
    assert result.bars_processed == 2
    assert result.orders_filled == 1
    assert wallet.position_qty("BTCUSDT") == 0.01


def test_replay_updates_wallet_mark_price_for_undeclared_ticks_without_strategy_invocation():
    ticks = [
        MarketData(symbol="ETHUSDT", price=200.0, timestamp=1, market="futures", interval="1m"),
    ]
    wallet = FuturesWallet(initial_balance=1000.0)
    result = run_replay(ReplayConfig(strategy_code=UNDECLARED_TICK_STRATEGY_CODE, ticks=ticks, wallet=wallet))
    assert result.bars_processed == 0
    assert result.orders_filled == 0
    assert wallet.mark_price("ETHUSDT") == 200.0


def test_replay_rejects_forbidden_import_smuggling_before_execution():
    ticks = [
        MarketData(symbol="BTCUSDT", price=100.0, timestamp=1, market="futures", interval="1m"),
    ]
    wallet = FuturesWallet(initial_balance=1000.0)
    with pytest.raises(ValueError, match="strategy (import|validation) failed"):
        run_replay(ReplayConfig(strategy_code=SMUGGLED_IMPORT_STRATEGY_CODE, ticks=ticks, wallet=wallet))


def test_replay_rejects_dotted_third_party_module_import_before_execution():
    ticks = [
        MarketData(symbol="BTCUSDT", price=100.0, timestamp=1, market="futures", interval="1m"),
    ]
    wallet = FuturesWallet(initial_balance=1000.0)
    with pytest.raises(ValueError, match="strategy (import|validation) failed"):
        run_replay(ReplayConfig(strategy_code=SMUGGLED_DOTTED_MODULE_STRATEGY_CODE, ticks=ticks, wallet=wallet))


def test_replay_rejects_third_party_module_fromlist_import_before_execution():
    ticks = [
        MarketData(symbol="BTCUSDT", price=100.0, timestamp=1, market="futures", interval="1m"),
    ]
    wallet = FuturesWallet(initial_balance=1000.0)
    with pytest.raises(ValueError, match="strategy (import|validation) failed"):
        run_replay(ReplayConfig(strategy_code=SMUGGLED_FROMLIST_MODULE_STRATEGY_CODE, ticks=ticks, wallet=wallet))


def test_replay_allows_validated_numpy_imports():
    ticks = [
        MarketData(symbol="BTCUSDT", price=100.0, timestamp=1, market="futures", interval="1m"),
    ]
    wallet = FuturesWallet(initial_balance=1000.0)
    result = run_replay(ReplayConfig(strategy_code=NUMPY_IMPORT_STRATEGY_CODE, ticks=ticks, wallet=wallet))
    assert result.orders_filled == 1
    assert wallet.position_qty("BTCUSDT") == 0.01


def test_replay_allows_authoring_imports_for_numpy_pandas_and_order_decision():
    ticks = [
        MarketData(symbol="BTCUSDT", price=100.0, timestamp=1, market="futures", interval="1m"),
    ]
    wallet = FuturesWallet(initial_balance=1000.0)
    result = run_replay(ReplayConfig(strategy_code=ALLOWED_IMPORTS_STRATEGY_CODE, ticks=ticks, wallet=wallet))
    assert result.orders_filled == 1
    assert wallet.position_qty("BTCUSDT") == 0.01


@pytest.mark.parametrize(
    "strategy_code",
    [
        SMUGGLED_ROOT_MODULE_ATTRIBUTE_STRATEGY_CODE,
        SMUGGLED_NUMPY_BUILTINS_STRATEGY_CODE,
        SMUGGLED_HUSHINE_FORBIDDEN_EXPORT_STRATEGY_CODE,
    ],
)
def test_replay_rejects_forbidden_exports_from_allowed_roots(strategy_code):
    ticks = [
        MarketData(symbol="BTCUSDT", price=100.0, timestamp=1, market="futures", interval="1m"),
    ]
    wallet = FuturesWallet(initial_balance=1000.0)
    with pytest.raises((AttributeError, ValueError, ImportError)):
        run_replay(ReplayConfig(strategy_code=strategy_code, ticks=ticks, wallet=wallet))


def test_futures_wallet_rejects_unsupported_side():
    wallet = FuturesWallet(initial_balance=1000.0)
    with pytest.raises(ValueError, match="unsupported side"):
        wallet.fill_order(OrderDecision(symbol="BTCUSDT", side="NOPE", qty=0.01), price=100.0)


@pytest.mark.parametrize("qty", [0, -0.01])
def test_futures_wallet_rejects_non_positive_qty(qty):
    wallet = FuturesWallet(initial_balance=1000.0)
    with pytest.raises(ValueError, match="qty must be positive"):
        wallet.fill_order(OrderDecision(symbol="BTCUSDT", side="LONG", qty=qty), price=100.0)


def test_futures_wallet_flip_entry_price_uses_flip_fill_price():
    wallet = FuturesWallet(initial_balance=1000.0)
    wallet.fill_order(OrderDecision(symbol="BTCUSDT", side="LONG", qty=1), price=100.0)
    wallet.fill_order(OrderDecision(symbol="BTCUSDT", side="SHORT", qty=1.5), price=110.0)
    assert wallet.position_qty("BTCUSDT") == -0.5
    assert wallet.position_entry_price("BTCUSDT") == 110.0
