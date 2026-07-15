import subprocess

import pytest

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


DYNAMIC_LOADING_CASES = [
    ('import importlib; importlib.import_module("kafka")', "forbidden_import"),
    ('from importlib import import_module as load; load("psycopg2")', "forbidden_import"),
    ('loader = __import__; loader("cryptography")', "forbidden_call"),
    ('(loader := __import__)("kafka")', "forbidden_call"),
    ('exec("import kafka")', "forbidden_call"),
    ('getattr(__builtins__, "__import__")("kafka")', "forbidden_builtin_access"),
    ('vars(__builtins__)["__import__"]("kafka")', "forbidden_builtin_access"),
    ('globals()["__builtins__"]["__import__"]("kafka")', "forbidden_builtin_access"),
]

PLATFORM_IMPORT_BYPASSES = [
    (
        "from hushine_strategy import runtime_dependencies as rd\n"
        'rd.importlib.import_module("kafka")'
    ),
    (
        "from hushine_strategy.runtime_dependencies import subprocess\n"
        'subprocess.run(["true"])'
    ),
    (
        "from hushine_strategy.notifier import Path\n"
        'Path("/tmp/escape").write_text("x")'
    ),
]

BUILTINS_IMPORT_ALIAS_BYPASSES = [
    (
        "from requests import __builtins__ as b\n"
        'b["__import__"]("kafka")'
    ),
    (
        "from requests import __dict__ as d\n"
        'loader = d.get("__builtins__").get("__import__")\n'
        'loader("kafka")'
    ),
]


def _validate_body(body: str):
    return validate_strategy_code(f"{body}\nclass MyStrategy:\n    INPUTS=[]\n")


def test_valid_strategy_passes():
    result = validate_strategy_code(VALID_CODE)
    assert result.ok is True
    assert result.issues == []


def test_allows_manifest_requests_import():
    result = validate_strategy_code("import requests\nclass MyStrategy:\n    INPUTS=[]\n")
    assert result.ok is True
    assert result.issues == []


def test_allows_manifest_requests_import_from():
    result = validate_strategy_code("from requests import get\nclass MyStrategy:\n    INPUTS=[]\n")
    assert result.ok is True
    assert result.issues == []


def test_allows_future_annotations_import():
    result = validate_strategy_code("from __future__ import annotations\nclass MyStrategy:\n    INPUTS=[]\n")
    assert result.ok is True
    assert result.issues == []


def test_allows_manifest_dotted_import():
    result = validate_strategy_code("import pandas.io.common as common\nclass MyStrategy:\n    INPUTS=[]\n")
    assert result.ok is True
    assert result.issues == []


def test_allows_manifest_dotted_import_from():
    result = validate_strategy_code("from pandas.io import common\nclass MyStrategy:\n    INPUTS=[]\n")
    assert result.ok is True
    assert result.issues == []


@pytest.mark.parametrize(
    "module",
    [
        "scipy",
        "sklearn",
        "statsmodels",
        "pandas_ta",
        "ta",
        "talib",
        "coverage",
        "debugpy",
        "pydevd",
        "pydevd_pycharm",
        "pytest",
        "pyarrow",
        "zstandard",
    ],
)
def test_non_contract_dependency_uses_stable_code(module):
    result = _validate_body(f"import {module}")
    assert [(issue.code, issue.module) for issue in result.issues] == [
        ("UNSUPPORTED_STRATEGY_DEPENDENCY", module)
    ]


def test_dependency_issue_preserves_complete_requested_module():
    result = _validate_body("import talib.child")
    assert [(issue.code, issue.module) for issue in result.issues] == [
        ("UNSUPPORTED_STRATEGY_DEPENDENCY", "talib.child")
    ]


@pytest.mark.parametrize(
    ("source", "module"),
    [
        ("from . import x", "."),
        ("from .hushine_strategy import X", ".hushine_strategy"),
        ("from ..pandas import X", "..pandas"),
    ],
)
def test_rejects_relative_import_with_leading_dots(source, module):
    result = _validate_body(source)
    assert [(issue.code, issue.module) for issue in result.issues] == [
        ("forbidden_import", module)
    ]


@pytest.mark.parametrize("module", ["os", "subprocess", "os.path"])
def test_library_forbidden_roots_emit_exactly_one_safety_issue(module):
    result = _validate_body(f"import {module}")
    assert [(issue.code, issue.module) for issue in result.issues] == [
        ("forbidden_import", module)
    ]


def test_library_allows_limited_stdlib_dotted_alias():
    result = _validate_body("import collections.abc")
    assert result.ok is True
    assert result.issues == []


def test_normal_getattr_remains_valid():
    result = validate_strategy_code(
        "class MyStrategy:\n"
        "    INPUTS=[]\n"
        "    def callback(self, strategy):\n"
        "        return getattr(strategy, 'indicators', None)\n"
    )
    assert result.ok is True
    assert result.issues == []


def test_static_manifest_import_inside_callback_remains_visible_and_allowed():
    result = validate_strategy_code(
        "class MyStrategy:\n"
        "    INPUTS=[]\n"
        "    def callback(self):\n"
        "        import numpy\n"
        "        return numpy.array([1])\n"
    )
    assert result.ok is True
    assert result.issues == []


@pytest.mark.parametrize("source", PLATFORM_IMPORT_BYPASSES)
def test_platform_import_bypasses_are_only_safety_issues(source):
    result = _validate_body(source)
    assert result.issues
    assert {issue.code for issue in result.issues} == {"forbidden_import"}
    assert all(issue.code != "UNSUPPORTED_STRATEGY_DEPENDENCY" for issue in result.issues)


@pytest.mark.parametrize(
    "source",
    [
        "import hushine_strategy",
        "import hushine_strategy.types as sdk",
        "from hushine_strategy import LocalNotifier",
        "from hushine_strategy.types import *",
    ],
)
def test_platform_module_handles_and_non_surface_symbols_are_rejected(source):
    result = _validate_body(source)
    assert result.issues
    assert {issue.code for issue in result.issues} == {"forbidden_import"}


@pytest.mark.parametrize(("source", "expected_code"), DYNAMIC_LOADING_CASES)
def test_dynamic_loading_is_only_a_safety_issue(source, expected_code):
    result = _validate_body(source)
    assert result.issues
    assert expected_code in {issue.code for issue in result.issues}
    assert all(issue.code != "UNSUPPORTED_STRATEGY_DEPENDENCY" for issue in result.issues)


def test_imported_forbidden_call_alias_suppresses_dependency_issue():
    result = _validate_body("from kafka import eval as load\nload('payload')")
    assert result.issues
    assert {issue.code for issue in result.issues} == {"forbidden_call"}


@pytest.mark.parametrize("source", BUILTINS_IMPORT_ALIAS_BYPASSES)
def test_imported_builtins_containers_cannot_bypass_library_safety(source):
    result = _validate_body(source)
    codes = {issue.code for issue in result.issues}
    assert "forbidden_builtin_access" in codes
    assert "forbidden_call" in codes
    assert "UNSUPPORTED_STRATEGY_DEPENDENCY" not in codes


@pytest.mark.parametrize(
    "source",
    PLATFORM_IMPORT_BYPASSES
    + BUILTINS_IMPORT_ALIAS_BYPASSES
    + [source for source, _ in DYNAMIC_LOADING_CASES],
)
def test_library_validator_never_spawns_a_dependency_probe(
    monkeypatch,
    source,
):
    calls = []

    def child_probe(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("static validation must not spawn a child")

    monkeypatch.setattr(subprocess, "Popen", child_probe)
    result = _validate_body(source)
    assert result.issues
    assert calls == []


def test_library_adapter_preserves_same_line_platform_symbols():
    result = _validate_body(
        "from hushine_strategy import LocalNotifier, runtime_dependencies"
    )
    assert [
        issue.symbol
        for issue in result.issues
        if issue.code == "forbidden_import"
    ] == ["LocalNotifier", "runtime_dependencies"]


def test_relative_and_static_issues_are_globally_stable_sorted():
    result = _validate_body("import talib\nfrom . import x")
    keys = [
        (issue.line, issue.module, issue.code)
        for issue in result.issues
    ]
    assert keys == sorted(keys)


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
