import pytest

from hushine_strategy import Exchange, Market, OrderDecision, OrderSide, OrderType, PositionSide
from hushine_strategy.replay.engine import ReplayConfig, run_replay
from hushine_strategy.types import MarketData
from hushine_strategy.wallet.futures import FuturesWallet


STRATEGY_CODE = """
from __future__ import annotations
from hushine_strategy import Exchange, Market, OrderDecision, OrderSide, OrderType, PositionSide

class MyStrategy:
    INPUTS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT", "interval": "1m"}]
    ORDER_TARGETS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT"}]

    def __init__(self):
        self.done = False

    def on_market_data(self, data, wallet):
        tick = data.exchange[Exchange.BINANCE][Market.PERPETUAL_FUTURES].symbol["BTCUSDT"].interval["1m"]
        if tick and not self.done:
            self.done = True
            return OrderDecision(
                exchange=Exchange.BINANCE,
                market=Market.PERPETUAL_FUTURES,
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                qty="0.01",
                order_type=OrderType.MARKET,
                position_side=PositionSide.BOTH,
            )
        return None
"""


UNDECLARED_TICK_STRATEGY_CODE = """
from hushine_strategy import Exchange, Market

class MyStrategy:
    INPUTS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT", "interval": "1m"}]
    ORDER_TARGETS = []

    def on_market_data(self, data, wallet):
        1 / 0
"""


SMUGGLED_IMPORT_STRATEGY_CODE = """
from pandas.io.common import os
from hushine_strategy import Exchange, Market

class MyStrategy:
    INPUTS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT", "interval": "1m"}]
    ORDER_TARGETS = []

    def on_market_data(self, data, wallet):
        self.leaked = os.name
        return None
"""


SMUGGLED_DOTTED_MODULE_STRATEGY_CODE = """
import pandas.io.common as common
from hushine_strategy import Exchange, Market

class MyStrategy:
    INPUTS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT", "interval": "1m"}]
    ORDER_TARGETS = []

    def on_market_data(self, data, wallet):
        1 / 0
        self.leaked = common.os.name
        return None
"""


SMUGGLED_FROMLIST_MODULE_STRATEGY_CODE = """
from pandas.io import common
from hushine_strategy import Exchange, Market

class MyStrategy:
    INPUTS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT", "interval": "1m"}]
    ORDER_TARGETS = []

    def on_market_data(self, data, wallet):
        1 / 0
        self.leaked = common.os.name
        return None
"""


NUMPY_IMPORT_STRATEGY_CODE = """
import numpy as np
from hushine_strategy import Exchange, Market, OrderDecision, OrderSide, OrderType, PositionSide

class MyStrategy:
    INPUTS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT", "interval": "1m"}]
    ORDER_TARGETS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT"}]

    def on_market_data(self, data, wallet):
        qty = float(np.array([0.01])[0])
        return OrderDecision(
            exchange=Exchange.BINANCE,
            market=Market.PERPETUAL_FUTURES,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            qty=str(qty),
            order_type=OrderType.MARKET,
            position_side=PositionSide.BOTH,
        )
"""


ALLOWED_IMPORTS_STRATEGY_CODE = """
import numpy as np
import pandas as pd
from pandas import DataFrame
from hushine_strategy import Exchange, Market, OrderDecision, OrderSide, OrderType, PositionSide

class MyStrategy:
    INPUTS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT", "interval": "1m"}]
    ORDER_TARGETS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT"}]

    def on_market_data(self, data, wallet):
        frame = DataFrame(pd.Series(np.array([0.01])), columns=["qty"])
        return OrderDecision(
            exchange=Exchange.BINANCE,
            market=Market.PERPETUAL_FUTURES,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            qty=str(float(frame["qty"].iloc[0])),
            order_type=OrderType.MARKET,
            position_side=PositionSide.BOTH,
        )
"""


SMUGGLED_ROOT_MODULE_ATTRIBUTE_STRATEGY_CODE = """
import pandas as pd
from hushine_strategy import Exchange, Market

class MyStrategy:
    INPUTS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT", "interval": "1m"}]
    ORDER_TARGETS = []

    def on_market_data(self, data, wallet):
        self.leaked = pd.io.common.os.listdir(".")
        return None
"""


SMUGGLED_NUMPY_BUILTINS_STRATEGY_CODE = """
import numpy as np
from hushine_strategy import Exchange, Market

class MyStrategy:
    INPUTS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT", "interval": "1m"}]
    ORDER_TARGETS = []

    def on_market_data(self, data, wallet):
        self.leaked = np.__builtins__["__import__"]("os")
        return None
"""


SMUGGLED_HUSHINE_FORBIDDEN_EXPORT_STRATEGY_CODE = """
from hushine_strategy.notifier import Path
from hushine_strategy import Exchange, Market

class MyStrategy:
    INPUTS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT", "interval": "1m"}]
    ORDER_TARGETS = []

    def on_market_data(self, data, wallet):
        self.leaked = Path(".").exists()
        return None
"""


MISSING_ORDER_TARGETS_STRATEGY_CODE = """
from hushine_strategy import Exchange, Market

class MyStrategy:
    INPUTS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT", "interval": "1m"}]

    def on_market_data(self, data, wallet):
        return None
"""


READ_ONLY_RETURNS_ORDER_STRATEGY_CODE = """
from hushine_strategy import Exchange, Market, OrderDecision, OrderSide, OrderType, PositionSide

class MyStrategy:
    INPUTS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT", "interval": "1m"}]
    ORDER_TARGETS = []

    def on_market_data(self, data, wallet):
        return OrderDecision(
            exchange=Exchange.BINANCE,
            market=Market.PERPETUAL_FUTURES,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            qty="0.01",
            order_type=OrderType.MARKET,
            position_side=PositionSide.BOTH,
        )
"""


TARGET_MISMATCH_STRATEGY_CODE = """
from hushine_strategy import Exchange, Market, OrderDecision, OrderSide, OrderType, PositionSide

class MyStrategy:
    INPUTS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT", "interval": "1m"}]
    ORDER_TARGETS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "ETHUSDT"}]

    def on_market_data(self, data, wallet):
        return OrderDecision(
            exchange=Exchange.BINANCE,
            market=Market.PERPETUAL_FUTURES,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            qty="0.01",
            order_type=OrderType.MARKET,
            position_side=PositionSide.BOTH,
        )
"""


SPOT_ORDER_TARGET_STRATEGY_CODE = """
from hushine_strategy import Exchange, Market

class MyStrategy:
    INPUTS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT", "interval": "1m"}]
    ORDER_TARGETS = [{"exchange": Exchange.BINANCE, "market": Market.SPOT, "symbol": "BTCUSDT"}]

    def on_market_data(self, data, wallet):
        return None
"""


LEGACY_MARKET_ORDER_STRATEGY_CODE = """
from hushine_strategy import Exchange, Market, OrderDecision, OrderSide, OrderType, PositionSide

class MyStrategy:
    INPUTS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT", "interval": "1m"}]
    ORDER_TARGETS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT"}]

    def on_market_data(self, data, wallet):
        return OrderDecision(
            exchange=Exchange.BINANCE,
            market="futures",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            qty="0.01",
            order_type=OrderType.MARKET,
            position_side=PositionSide.BOTH,
        )
"""


def _btcusdt_tick() -> MarketData:
    return MarketData(
        symbol="BTCUSDT",
        price=100.0,
        timestamp=1,
        exchange=Exchange.BINANCE,
        market=Market.PERPETUAL_FUTURES,
        interval="1m",
    )


def test_replay_processes_ticks_and_fills_local_order():
    ticks = [
        MarketData(
            symbol="BTCUSDT",
            price=100.0,
            timestamp=1,
            exchange=Exchange.BINANCE,
            market=Market.PERPETUAL_FUTURES,
            interval="1m",
        ),
        MarketData(
            symbol="BTCUSDT",
            price=101.0,
            timestamp=2,
            exchange=Exchange.BINANCE,
            market=Market.PERPETUAL_FUTURES,
            interval="1m",
        ),
    ]
    wallet = FuturesWallet(initial_balance=1000.0)
    result = run_replay(ReplayConfig(strategy_code=STRATEGY_CODE, ticks=ticks, wallet=wallet))
    assert result.bars_processed == 2
    assert result.orders_filled == 1
    assert wallet.position_qty("BTCUSDT") == 0.01


def test_replay_requires_order_targets_declaration():
    wallet = FuturesWallet(initial_balance=1000.0)
    with pytest.raises(ValueError, match="ORDER_TARGETS must be declared"):
        run_replay(ReplayConfig(strategy_code=MISSING_ORDER_TARGETS_STRATEGY_CODE, ticks=[_btcusdt_tick()], wallet=wallet))


def test_replay_rejects_order_from_read_only_strategy():
    wallet = FuturesWallet(initial_balance=1000.0)
    with pytest.raises(ValueError, match="ORDER_TARGETS is empty"):
        run_replay(ReplayConfig(strategy_code=READ_ONLY_RETURNS_ORDER_STRATEGY_CODE, ticks=[_btcusdt_tick()], wallet=wallet))


def test_replay_rejects_order_target_mismatch():
    wallet = FuturesWallet(initial_balance=1000.0)
    with pytest.raises(ValueError, match="not declared in ORDER_TARGETS"):
        run_replay(ReplayConfig(strategy_code=TARGET_MISMATCH_STRATEGY_CODE, ticks=[_btcusdt_tick()], wallet=wallet))


def test_replay_rejects_non_perpetual_futures_order_target():
    wallet = FuturesWallet(initial_balance=1000.0)
    with pytest.raises(ValueError, match="local replay only supports perpetual_futures ORDER_TARGETS"):
        run_replay(ReplayConfig(strategy_code=SPOT_ORDER_TARGET_STRATEGY_CODE, ticks=[_btcusdt_tick()], wallet=wallet))


def test_replay_rejects_legacy_futures_market_order_decision():
    wallet = FuturesWallet(initial_balance=1000.0)
    with pytest.raises(ValueError, match="local replay only supports perpetual_futures orders"):
        run_replay(ReplayConfig(strategy_code=LEGACY_MARKET_ORDER_STRATEGY_CODE, ticks=[_btcusdt_tick()], wallet=wallet))


def test_replay_ignores_undeclared_ticks_without_wallet_or_strategy_invocation():
    ticks = [
        MarketData(
            symbol="ETHUSDT",
            price=200.0,
            timestamp=1,
            exchange=Exchange.BINANCE,
            market=Market.PERPETUAL_FUTURES,
            interval="1m",
        ),
    ]
    wallet = FuturesWallet(initial_balance=1000.0)
    result = run_replay(ReplayConfig(strategy_code=UNDECLARED_TICK_STRATEGY_CODE, ticks=ticks, wallet=wallet))
    assert result.bars_processed == 0
    assert result.orders_filled == 0
    assert wallet.mark_price("ETHUSDT") is None


def test_replay_does_not_update_mark_price_for_undeclared_cross_market_same_symbol_tick():
    ticks = [
        MarketData(
            symbol="BTCUSDT",
            price=200.0,
            timestamp=1,
            exchange=Exchange.BINANCE,
            market=Market.SPOT,
            interval="1m",
        ),
        MarketData(
            symbol="BTCUSDT",
            price=300.0,
            timestamp=2,
            exchange=Exchange.OKX,
            market=Market.PERPETUAL_FUTURES,
            interval="1m",
        ),
    ]
    wallet = FuturesWallet(initial_balance=1000.0)
    result = run_replay(ReplayConfig(strategy_code=UNDECLARED_TICK_STRATEGY_CODE, ticks=ticks, wallet=wallet))
    assert result.bars_processed == 0
    assert result.orders_filled == 0
    assert wallet.mark_price("BTCUSDT") is None


def test_replay_rejects_forbidden_import_smuggling_before_execution():
    ticks = [
        MarketData(
            symbol="BTCUSDT",
            price=100.0,
            timestamp=1,
            exchange=Exchange.BINANCE,
            market=Market.PERPETUAL_FUTURES,
            interval="1m",
        ),
    ]
    wallet = FuturesWallet(initial_balance=1000.0)
    with pytest.raises(ValueError, match="strategy (import|validation) failed"):
        run_replay(ReplayConfig(strategy_code=SMUGGLED_IMPORT_STRATEGY_CODE, ticks=ticks, wallet=wallet))


def test_replay_rejects_dotted_third_party_module_import_before_execution():
    ticks = [
        MarketData(
            symbol="BTCUSDT",
            price=100.0,
            timestamp=1,
            exchange=Exchange.BINANCE,
            market=Market.PERPETUAL_FUTURES,
            interval="1m",
        ),
    ]
    wallet = FuturesWallet(initial_balance=1000.0)
    with pytest.raises(ValueError, match="strategy (import|validation) failed"):
        run_replay(ReplayConfig(strategy_code=SMUGGLED_DOTTED_MODULE_STRATEGY_CODE, ticks=ticks, wallet=wallet))


def test_replay_rejects_third_party_module_fromlist_import_before_execution():
    ticks = [
        MarketData(
            symbol="BTCUSDT",
            price=100.0,
            timestamp=1,
            exchange=Exchange.BINANCE,
            market=Market.PERPETUAL_FUTURES,
            interval="1m",
        ),
    ]
    wallet = FuturesWallet(initial_balance=1000.0)
    with pytest.raises(ValueError, match="strategy (import|validation) failed"):
        run_replay(ReplayConfig(strategy_code=SMUGGLED_FROMLIST_MODULE_STRATEGY_CODE, ticks=ticks, wallet=wallet))


def test_replay_allows_validated_numpy_imports():
    ticks = [
        MarketData(
            symbol="BTCUSDT",
            price=100.0,
            timestamp=1,
            exchange=Exchange.BINANCE,
            market=Market.PERPETUAL_FUTURES,
            interval="1m",
        ),
    ]
    wallet = FuturesWallet(initial_balance=1000.0)
    result = run_replay(ReplayConfig(strategy_code=NUMPY_IMPORT_STRATEGY_CODE, ticks=ticks, wallet=wallet))
    assert result.orders_filled == 1
    assert wallet.position_qty("BTCUSDT") == 0.01


def test_replay_allows_authoring_imports_for_numpy_pandas_and_order_decision():
    ticks = [
        MarketData(
            symbol="BTCUSDT",
            price=100.0,
            timestamp=1,
            exchange=Exchange.BINANCE,
            market=Market.PERPETUAL_FUTURES,
            interval="1m",
        ),
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
        MarketData(
            symbol="BTCUSDT",
            price=100.0,
            timestamp=1,
            exchange=Exchange.BINANCE,
            market=Market.PERPETUAL_FUTURES,
            interval="1m",
        ),
    ]
    wallet = FuturesWallet(initial_balance=1000.0)
    with pytest.raises((AttributeError, ValueError, ImportError)):
        run_replay(ReplayConfig(strategy_code=strategy_code, ticks=ticks, wallet=wallet))


def test_futures_wallet_rejects_unsupported_side():
    wallet = FuturesWallet(initial_balance=1000.0)
    with pytest.raises(ValueError, match="unsupported side"):
        wallet.fill_order(
            OrderDecision(
                exchange=Exchange.BINANCE,
                market=Market.PERPETUAL_FUTURES,
                symbol="BTCUSDT",
                side="NOPE",
                qty="0.01",
                order_type=OrderType.MARKET,
                position_side=PositionSide.BOTH,
            ),
            price=100.0,
        )


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_futures_wallet_rejects_legacy_position_sides_as_order_side(side):
    wallet = FuturesWallet(initial_balance=1000.0)
    with pytest.raises(ValueError, match="unsupported side"):
        wallet.fill_order(
            OrderDecision(
                exchange=Exchange.BINANCE,
                market=Market.PERPETUAL_FUTURES,
                symbol="BTCUSDT",
                side=side,
                qty="0.01",
                order_type=OrderType.MARKET,
                position_side=PositionSide.BOTH,
            ),
            price=100.0,
        )


@pytest.mark.parametrize("qty", [0, -0.01])
def test_futures_wallet_rejects_non_positive_qty(qty):
    wallet = FuturesWallet(initial_balance=1000.0)
    with pytest.raises(ValueError, match="qty must be positive"):
        wallet.fill_order(
            OrderDecision(
                exchange=Exchange.BINANCE,
                market=Market.PERPETUAL_FUTURES,
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                qty=str(qty),
                order_type=OrderType.MARKET,
                position_side=PositionSide.BOTH,
            ),
            price=100.0,
        )


def test_futures_wallet_flip_entry_price_uses_flip_fill_price():
    wallet = FuturesWallet(initial_balance=1000.0)
    wallet.fill_order(
        OrderDecision(
            exchange=Exchange.BINANCE,
            market=Market.PERPETUAL_FUTURES,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            qty="1",
            order_type=OrderType.MARKET,
            position_side=PositionSide.BOTH,
        ),
        price=100.0,
    )
    wallet.fill_order(
        OrderDecision(
            exchange=Exchange.BINANCE,
            market=Market.PERPETUAL_FUTURES,
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            qty="1.5",
            order_type=OrderType.MARKET,
            position_side=PositionSide.BOTH,
        ),
        price=110.0,
    )
    assert wallet.position_qty("BTCUSDT") == -0.5
    assert wallet.position_entry_price("BTCUSDT") == 110.0
