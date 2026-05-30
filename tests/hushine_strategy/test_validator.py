from hushine_strategy.validator import validate_strategy_code


VALID_CODE = """
from hushine_strategy import Exchange, Market, OrderDecision
import numpy as np

class MyStrategy:
    INPUTS = [{"exchange": Exchange.BINANCE, "market": Market.PERPETUAL_FUTURES, "symbol": "BTCUSDT", "interval": "1m"}]
    ORDER_TARGETS = []

    def on_market_data(self, data, wallet):
        return None
"""


def test_valid_strategy_passes():
    result = validate_strategy_code(VALID_CODE)
    assert result.ok is True
    assert result.issues == []


def test_rejects_network_import():
    result = validate_strategy_code("import requests\nclass MyStrategy:\n    INPUTS=[]\n")
    assert result.ok is False
    assert result.issues[0].code == "forbidden_import"
    assert result.issues[0].module == "requests"


def test_rejects_network_import_from():
    result = validate_strategy_code("from requests import get\nclass MyStrategy:\n    INPUTS=[]\n")
    assert result.ok is False
    assert result.issues[0].code == "forbidden_import"
    assert result.issues[0].module == "requests"


def test_allows_future_annotations_import():
    result = validate_strategy_code("from __future__ import annotations\nclass MyStrategy:\n    INPUTS=[]\n")
    assert result.ok is True
    assert result.issues == []


def test_rejects_third_party_dotted_import():
    result = validate_strategy_code("import pandas.io.common as common\nclass MyStrategy:\n    INPUTS=[]\n")
    assert result.ok is False
    assert result.issues[0].code == "forbidden_import"
    assert result.issues[0].module == "pandas.io.common"


def test_rejects_third_party_dotted_import_from():
    result = validate_strategy_code("from pandas.io import common\nclass MyStrategy:\n    INPUTS=[]\n")
    assert result.ok is False
    assert result.issues[0].code == "forbidden_import"
    assert result.issues[0].module == "pandas.io"


def test_rejects_relative_import():
    result = validate_strategy_code("from .hushine_strategy import OrderDecision\nclass MyStrategy:\n    INPUTS=[]\n")
    assert result.ok is False
    assert any(issue.code == "forbidden_import" for issue in result.issues)


def test_rejects_open_call():
    result = validate_strategy_code(VALID_CODE + "\nopen('/tmp/x', 'w')\n")
    assert result.ok is False
    assert any(issue.code == "forbidden_call" and issue.symbol == "open" for issue in result.issues)


def test_rejects_aliasing_forbidden_builtin():
    result = validate_strategy_code(VALID_CODE + "\nf = open\nf('/tmp/x', 'w')\n")
    assert result.ok is False
    assert any(issue.code == "forbidden_call" and issue.symbol == "open" for issue in result.issues)
    assert any(issue.code == "forbidden_call" and issue.symbol == "f" for issue in result.issues)


def test_rejects_aliasing_import_builtin():
    result = validate_strategy_code(VALID_CODE + "\nimp = __import__\nimp('os')\n")
    assert result.ok is False
    assert any(issue.code == "forbidden_call" and issue.symbol == "__import__" for issue in result.issues)
    assert any(issue.code == "forbidden_call" and issue.symbol == "imp" for issue in result.issues)


def test_rejects_builtins_subscript_call():
    result = validate_strategy_code(VALID_CODE + "\n__builtins__['open']('/tmp/x', 'w')\n")
    assert result.ok is False
    assert any(issue.code == "forbidden_call" and issue.symbol == "open" for issue in result.issues)


def test_rejects_direct_builtins_reference():
    result = validate_strategy_code(VALID_CODE + "\nb = __builtins__\n")
    assert result.ok is False
    assert any(issue.code == "forbidden_builtin_access" and issue.symbol == "__builtins__" for issue in result.issues)


def test_rejects_builtins_alias_subscript_open_call():
    result = validate_strategy_code(VALID_CODE + "\nb = __builtins__\nb['open']('/tmp/x', 'w')\n")
    assert result.ok is False
    assert any(issue.code == "forbidden_call" and issue.symbol == "open" for issue in result.issues)


def test_rejects_builtins_alias_subscript_import_call():
    result = validate_strategy_code(VALID_CODE + "\nb = __builtins__\nb['__import__']('os')\n")
    assert result.ok is False
    assert any(issue.code == "forbidden_call" and issue.symbol == "__import__" for issue in result.issues)


def test_rejects_builtins_dict_alias_subscript_open_call():
    result = validate_strategy_code(VALID_CODE + "\nb = __builtins__.__dict__\nb['open']('/tmp/x', 'w')\n")
    assert result.ok is False
    assert any(issue.code == "forbidden_call" and issue.symbol == "open" for issue in result.issues)


def test_rejects_builtins_dict_get_open_call():
    result = validate_strategy_code(VALID_CODE + "\n__builtins__.__dict__.get('open')('/tmp/x', 'w')\n")
    assert result.ok is False
    assert any(issue.code == "forbidden_call" and issue.symbol == "open" for issue in result.issues)


def test_rejects_builtins_dict_alias_get_open_call():
    result = validate_strategy_code(VALID_CODE + "\nb = __builtins__.__dict__\nb.get('open')('/tmp/x', 'w')\n")
    assert result.ok is False
    assert any(issue.code == "forbidden_call" and issue.symbol == "open" for issue in result.issues)


def test_rejects_tuple_aliasing_forbidden_builtin():
    result = validate_strategy_code(VALID_CODE + "\nf, g = open, len\nf('/tmp/x', 'w')\n")
    assert result.ok is False
    assert any(issue.code == "forbidden_call" and issue.symbol == "open" for issue in result.issues)
    assert any(issue.code == "forbidden_call" and issue.symbol == "f" for issue in result.issues)


def test_rejects_walrus_alias_forbidden_builtin_call():
    result = validate_strategy_code(VALID_CODE + "\n(f := open)('/tmp/x', 'w')\n")
    assert result.ok is False
    assert any(issue.code == "forbidden_call" and issue.symbol == "open" for issue in result.issues)


def test_rejects_builtins_get_alias_probe():
    result = validate_strategy_code(VALID_CODE + "\nb = __builtins__.__dict__\nget = b.get\nget('open')('/tmp/x')\n")
    assert result.ok is False
    assert any(issue.code == "forbidden_builtin_access" and issue.symbol == "__builtins__" for issue in result.issues)


def test_rejects_builtins_dict_getitem_probe():
    result = validate_strategy_code(VALID_CODE + "\n__builtins__.__dict__.__getitem__('open')('/tmp/x')\n")
    assert result.ok is False
    assert any(issue.code == "forbidden_builtin_access" and issue.symbol == "__builtins__" for issue in result.issues)


def test_requires_my_strategy_class():
    result = validate_strategy_code("from hushine_strategy import OrderDecision\n")
    assert result.ok is False
    assert any(issue.code == "missing_my_strategy" for issue in result.issues)


def test_requires_module_level_my_strategy_class():
    result = validate_strategy_code("""
from hushine_strategy import OrderDecision

def factory():
    class MyStrategy:
        INPUTS = []
    return MyStrategy
""")
    assert result.ok is False
    assert any(issue.code == "missing_my_strategy" for issue in result.issues)


def test_syntax_error_reports_line_number():
    result = validate_strategy_code("class MyStrategy:\n    def on_market_data(self:\n")
    assert result.ok is False
    assert result.issues[0].code == "syntax_error"
    assert result.issues[0].line > 0


def test_default_strategy_profile():
    result = validate_strategy_code(VALID_CODE)
    assert result.strategy_profile == "base-futures-v1"
