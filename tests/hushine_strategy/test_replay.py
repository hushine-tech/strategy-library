import os
import types
from decimal import Decimal

import pytest

from hushine_strategy import Exchange, Market, OrderDecision, OrderSide, OrderType, PositionSide
from hushine_strategy.replay.engine import (
    ReplayConfig,
    _load_strategy,
    _SafeModule,
    run_replay,
)
from hushine_strategy.runtime_dependencies import load_runtime_dependency_profile
from hushine_strategy.types import MarketData
from hushine_strategy.validator import ALLOWED_IMPORT_ROOTS
from hushine_strategy.wallet.futures import FuturesWallet
from hushine_strategy.wallet import PortfolioWallet, SpotSymbolMetadata, SpotWallet


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


DOTTED_CONTRACT_IMPORTS_STRATEGY_CODE = """
import collections.abc as collections_abc
import pandas.io.common as pandas_common
import requests.packages.urllib3 as requests_urllib3
from hushine_strategy import Exchange, Market

class MyStrategy:
    INPUTS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT", "interval": "1m"}]
    ORDER_TARGETS = []

    def on_market_data(self, data, wallet):
        self.loaded = (
            collections_abc.Iterable,
            pandas_common.__name__,
            requests_urllib3.__name__,
        )
        return None
"""


def _star_import_strategy_code(import_statement: str, symbol: str) -> str:
    return (
        f"{import_statement}\n"
        "from hushine_strategy import Exchange, Market\n"
        "class MyStrategy:\n"
        "    INPUTS = [{\"exchange\": Exchange.BINANCE, \"market\": "
        "Market.PERPETUAL_FUTURES, \"symbol\": \"BTCUSDT\", "
        "\"interval\": \"1m\"}]\n"
        "    ORDER_TARGETS = []\n"
        f"    STAR_SYMBOL = {symbol}\n"
        "    def on_market_data(self, data, wallet):\n"
        "        return None\n"
    )


def _requests_runtime_alias_strategy_code(
    alias_import: str,
    class_body: str,
) -> str:
    return (
        "import requests as requests_root\n"
        "import requests.packages as requests_packages\n"
        f"{alias_import}\n"
        "class MyStrategy:\n"
        "    INPUTS = []\n"
        "    ORDER_TARGETS = []\n"
        "    ROOT_MODULE = requests_root\n"
        "    PARENT_MODULE = requests_packages\n"
        "    LEAF_MODULE = u\n"
        "    ORDINARY_ATTRIBUTE = u.PoolManager\n"
        f"    {class_body}\n"
    )


STAR_IMPORT_FORBIDDEN_EXPORT_STRATEGY_CODE = """
from pandas.io.common import *
LEAKED_MODULE = os

class MyStrategy:
    INPUTS = []
    ORDER_TARGETS = []

    def on_market_data(self, data, wallet):
        return None
"""


SAFE_GETATTR_STRATEGY_CODE = """
from hushine_strategy import Exchange, Market

class MyStrategy:
    INPUTS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT", "interval": "1m"}]
    ORDER_TARGETS = []

    def on_market_data(self, data, wallet):
        self.indicators = getattr(data, "indicators", None)
        return None
"""


NUMPY_DECLARED_UNDERSCORE_STAR_STRATEGY_CODE = """
from numpy import *
from hushine_strategy import Exchange, Market

class MyStrategy:
    INPUTS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT", "interval": "1m"}]
    ORDER_TARGETS = []
    NUMPY_VERSION = __version__

    def on_market_data(self, data, wallet):
        self.numpy_version = __version__
        return None
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


def test_replay_requires_route_aware_wallet_for_spot_target():
    wallet = FuturesWallet(initial_balance=1000.0)
    with pytest.raises(ValueError, match="PortfolioWallet is required for Spot replay"):
        run_replay(ReplayConfig(strategy_code=SPOT_ORDER_TARGET_STRATEGY_CODE, ticks=[_btcusdt_tick()], wallet=wallet))


def test_replay_rejects_legacy_futures_market_order_decision():
    wallet = FuturesWallet(initial_balance=1000.0)
    with pytest.raises(ValueError, match="does not support order market futures"):
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


def test_replay_rejects_forbidden_export_from_authorized_dotted_import():
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
        run_replay(ReplayConfig(strategy_code=SMUGGLED_DOTTED_MODULE_STRATEGY_CODE, ticks=ticks, wallet=wallet))


def test_replay_rejects_forbidden_export_from_authorized_fromlist_import():
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


def test_replay_allowed_roots_derive_the_public_contract_without_legacy_algorithms():
    public_roots = set(load_runtime_dependency_profile().public_import_roots)
    assert public_roots <= ALLOWED_IMPORT_ROOTS
    assert "hushine_strategy" in ALLOWED_IMPORT_ROOTS
    assert ALLOWED_IMPORT_ROOTS.isdisjoint(
        {"scipy", "sklearn", "statsmodels", "pandas_ta", "ta", "talib"}
    )


def test_replay_executes_authorized_dotted_and_runtime_alias_imports():
    wallet = FuturesWallet(initial_balance=1000.0)
    result = run_replay(
        ReplayConfig(
            strategy_code=DOTTED_CONTRACT_IMPORTS_STRATEGY_CODE,
            ticks=[_btcusdt_tick()],
            wallet=wallet,
        )
    )
    assert result.bars_processed == 1


@pytest.mark.parametrize(
    ("import_statement", "symbol"),
    [
        ("from collections import *", "ChainMap"),
        ("from requests import *", "Request"),
        ("from pandas.io.common import *", "is_fsspec_url"),
    ],
)
def test_replay_executes_authorized_stdlib_and_third_party_star_imports(
    import_statement,
    symbol,
):
    result = run_replay(
        ReplayConfig(
            strategy_code=_star_import_strategy_code(
                import_statement,
                symbol,
            ),
            ticks=[],
            wallet=FuturesWallet(initial_balance=1000.0),
        )
    )
    assert result.bars_processed == 0


def test_replay_executes_normal_getattr_allowed_by_static_validation():
    result = run_replay(
        ReplayConfig(
            strategy_code=SAFE_GETATTR_STRATEGY_CODE,
            ticks=[_btcusdt_tick()],
            wallet=FuturesWallet(initial_balance=1000.0),
        )
    )
    assert result.bars_processed == 1


def test_replay_star_import_keeps_safe_declared_leading_underscore_name():
    result = run_replay(
        ReplayConfig(
            strategy_code=NUMPY_DECLARED_UNDERSCORE_STAR_STRATEGY_CODE,
            ticks=[_btcusdt_tick()],
            wallet=FuturesWallet(initial_balance=1000.0),
        )
    )
    assert result.bars_processed == 1


def test_safe_module_declared_all_filters_only_unsafe_internal_names():
    module = types.ModuleType("requests.synthetic")
    module.__all__ = (
        "__version__",
        "__builtins__",
        "__dict__",
        "_logical_name",
        "_module",
        "forbidden_module",
        "safe_value",
    )
    module.__version__ = "1.2.3"
    module.__builtins__ = {}
    module._logical_name = "leak"
    module._module = "leak"
    module.forbidden_module = os
    module.safe_value = 1
    wrapped = _SafeModule(module, logical_name="requests.synthetic")

    assert wrapped.__all__ == ("__version__", "safe_value")
    assert wrapped.__version__ == "1.2.3"
    for name in ("__builtins__", "__dict__", "_logical_name", "_module"):
        with pytest.raises(AttributeError):
            getattr(wrapped, name)


def test_replay_star_import_wraps_registered_requests_runtime_alias():
    strategy = _load_strategy(
        _star_import_strategy_code(
            "from requests.packages import *",
            "urllib3",
        ),
        "strategy.py",
    )
    assert isinstance(strategy.STAR_SYMBOL, _SafeModule)


@pytest.mark.parametrize(
    "alias_import",
    [
        "import requests.packages.urllib3 as u",
        "from requests.packages import urllib3 as u",
    ],
)
def test_replay_runtime_alias_forms_keep_parent_and_leaf_modules_wrapped(
    alias_import,
):
    strategy = _load_strategy(
        _requests_runtime_alias_strategy_code(alias_import, "SAFE = True"),
        "strategy.py",
    )
    assert isinstance(strategy.ROOT_MODULE, _SafeModule)
    assert isinstance(strategy.PARENT_MODULE, _SafeModule)
    assert isinstance(strategy.LEAF_MODULE, _SafeModule)
    assert strategy.ORDINARY_ATTRIBUTE.__name__ == "PoolManager"


@pytest.mark.parametrize(
    "alias_import",
    [
        "import requests.packages.urllib3 as u",
        "from requests.packages import urllib3 as u",
    ],
)
def test_replay_runtime_alias_forms_cannot_escape_to_raw_os(alias_import):
    with pytest.raises(ValueError, match="import os is not allowed"):
        _load_strategy(
            _requests_runtime_alias_strategy_code(
                alias_import,
                "LEAKED_MODULE = u.util.ssl_.os",
            ),
            "strategy.py",
        )


def test_replay_star_import_does_not_forward_forbidden_module_exports():
    with pytest.raises(NameError, match="os"):
        run_replay(
            ReplayConfig(
                strategy_code=STAR_IMPORT_FORBIDDEN_EXPORT_STRATEGY_CODE,
                ticks=[],
                wallet=FuturesWallet(initial_balance=1000.0),
            )
        )


def test_replay_rejects_unsupported_dotted_root_before_execution():
    strategy_code = (
        "import scipy.sparse\n"
        "class MyStrategy:\n"
        "    INPUTS = []\n"
        "    ORDER_TARGETS = []\n"
        "    def on_market_data(self, data, wallet):\n"
        "        return None\n"
    )
    with pytest.raises(ValueError, match="UNSUPPORTED_STRATEGY_DEPENDENCY"):
        run_replay(
            ReplayConfig(
                strategy_code=strategy_code,
                ticks=[],
                wallet=FuturesWallet(initial_balance=1000.0),
            )
        )


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


def test_run_replay_executes_declared_spot_target_with_immutable_facts():
    strategy_code = """
from hushine_strategy import Exchange, Market, OrderDecision, OrderSide, OrderType

class MyStrategy:
    INPUTS = [{"exchange": Exchange.BINANCE, "market": Market.SPOT, "symbol": "BTCUSDT", "interval": "1m"}]
    ORDER_TARGETS = [{"exchange": Exchange.BINANCE, "market": Market.SPOT, "symbol": "BTCUSDT"}]

    def __init__(self):
        self.done = False

    def on_market_data(self, data, wallet):
        if self.done:
            return None
        self.done = True
        return OrderDecision(
            exchange=Exchange.BINANCE,
            market=Market.SPOT,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            qty="0.01",
            order_type=OrderType.MARKET,
        )
"""
    metadata = SpotSymbolMetadata(
        venue_id=10,
        exchange="binance",
        market="spot",
        symbol="BTCUSDT",
        status="TRADING",
        base_asset="BTC",
        quote_asset="USDT",
        base_asset_precision=8,
        quote_asset_precision=8,
        spot_trading_allowed=True,
        filters=(
            {"filter_type": "LOT_SIZE", "min_qty": "0.00001", "max_qty": "9000", "step_size": "0.00001"},
            {"filter_type": "MIN_NOTIONAL", "min_notional": "5", "apply_to_market": True},
        ),
    )
    spot = SpotWallet.from_assets({"USDT": ("1000", "0")})
    wallet = PortfolioWallet(
        allowed_routes={("binance", "spot")},
        wallets={("binance", "spot", 10): spot},
    )

    result = run_replay(ReplayConfig(
        strategy_code=strategy_code,
        ticks=[MarketData(
            stream_id="spot-btc-1m",
            exchange="binance",
            market="spot",
            kind="kline",
            symbol="BTCUSDT",
            interval="1m",
            price=50_000,
            timestamp=1,
        )],
        wallet=wallet,
        metadata={metadata.route_key: metadata},
        risk_facts={metadata.route_key: {
            "snapshot_id": "facts-1",
            "reference_price_decimal": "50000",
        }},
        default_fee_rate="0.001",
    ))

    assert result.bars_processed == 1
    assert result.orders_filled == 1
    assert spot.assets["BTC"].free == Decimal("0.01")
    assert spot.assets["USDT"].free == Decimal("499.5")


def test_run_replay_keeps_same_route_stream_id_and_kind_visible_to_strategy():
    strategy_code = """
from hushine_strategy import Exchange, Market, OrderDecision, OrderSide, OrderType

class MyStrategy:
    INPUTS = [
        {"stream_id": "btc-kline", "exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "kind": "kline", "symbol": "BTCUSDT", "interval": "1m"},
        {"stream_id": "btc-mark", "exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "kind": "mark_price", "symbol": "BTCUSDT", "interval": "1m"},
    ]
    ORDER_TARGETS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT"}]

    def __init__(self):
        self.done = False

    def on_market_data(self, data, wallet):
        kline = data.get_stream("btc-kline", Exchange.BINANCE, Market.PERPETUAL_FUTURES, "kline", "BTCUSDT", "1m")
        mark = data.get_stream("btc-mark", Exchange.BINANCE, Market.PERPETUAL_FUTURES, "mark_price", "BTCUSDT", "1m")
        if self.done or kline is None or mark is None:
            return None
        self.done = True
        return OrderDecision(
            exchange=Exchange.BINANCE,
            market=Market.PERPETUAL_FUTURES,
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            qty="0.01",
            order_type=OrderType.MARKET,
        )
"""
    ticks = [
        MarketData(
            stream_id="btc-kline",
            exchange="binance",
            market="perpetual_futures",
            kind="kline",
            symbol="BTCUSDT",
            interval="1m",
            price=50_000,
            timestamp=1,
        ),
        MarketData(
            stream_id="btc-mark",
            exchange="binance",
            market="perpetual_futures",
            kind="mark_price",
            symbol="BTCUSDT",
            interval="1m",
            price=50_100,
            timestamp=2,
        ),
    ]
    wallet = FuturesWallet(initial_balance=1000)

    result = run_replay(ReplayConfig(
        strategy_code=strategy_code,
        ticks=ticks,
        wallet=wallet,
    ))

    assert result.bars_processed == 2
    assert result.orders_filled == 1
    assert wallet.position_qty("BTCUSDT") == 0.01
