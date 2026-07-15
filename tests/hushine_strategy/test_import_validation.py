import ast
from dataclasses import FrozenInstanceError
import importlib.machinery
import sys

import pytest

import hushine_strategy
import hushine_strategy.import_validation as import_validation
from hushine_strategy.import_validation import (
    DependencyValidationIssue,
    DynamicImportSafetyIssue,
    HOSTED_PLATFORM_IMPORT_POLICY,
    ImportedModule,
    PlatformImportPolicy,
    SDK_PLATFORM_IMPORT_POLICY,
    find_spec_without_import,
    iter_imported_modules,
    validate_dependency_imports,
    validate_dynamic_import_safety,
    validate_platform_import_safety,
)
from hushine_strategy.runtime_dependencies import load_runtime_dependency_profile


PROFILE = load_runtime_dependency_profile()
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
REFLECTIVE_BUILTINS_BYPASSES = [
    (
        "import requests\n"
        'getattr(requests, "__builtins__")["__import__"]("kafka")'
    ),
    (
        "import requests\n"
        'builtins_container = getattr(requests, "__builtins__")\n'
        'loader = builtins_container["__import__"]\n'
        'loader("kafka")'
    ),
    (
        "import requests\n"
        'getattr(requests, "__dict__").get("__builtins__").get('
        '"__import__")("kafka")'
    ),
    (
        "import requests\n"
        'module_dict = getattr(requests, "__dict__")\n'
        'builtins_container = module_dict.get("__builtins__")\n'
        'loader = builtins_container.get("__import__")\n'
        'loader("kafka")'
    ),
]
NESTED_ASSIGNMENT_BYPASSES = [
    '((loader,),) = ((__import__,),)\nloader("kafka")',
    '(safe, (loader,)) = (len, (__import__,))\nloader("kafka")',
    '([loader],) = [(__import__,)]\nloader("kafka")',
    '(loader, safe) = (__import__,)\nloader("kafka")',
]
HANDLE_ACQUISITION_BYPASSES = [
    'def run(loader=__import__): return loader("kafka")',
    'def run(*, loader=__import__): return loader("kafka")',
    'run = lambda loader=__import__: loader("kafka")',
    '(lambda loader: loader("kafka"))(__import__)',
    '[loader("kafka") for loader in [__import__]]',
    'for loader in [__import__]: loader("kafka")',
]
SAFE_HANDLE_USES = [
    "def run(loader=len): return loader([1])",
    "def run(*, loader=len): return loader([1])",
    "run = lambda loader=len: loader([1])",
    "(lambda loader: loader([1]))(len)",
    "[loader([1]) for loader in [len]]",
    "for loader in [len]: loader([1])",
]


def _reverse_forbidden_alias_chain(assignment_count: int) -> str:
    lines = [
        f"alias_{index} = alias_{index + 1}"
        for index in range(assignment_count - 1)
    ]
    lines.extend(
        [
            f"alias_{assignment_count - 1} = __import__",
            'alias_0("kafka")',
        ]
    )
    return "\n".join(lines)


def _dependency_issues(
    source: str,
    *,
    stdlib_roots: frozenset[str] = frozenset(),
    platform_modules: frozenset[str] = frozenset(),
):
    return validate_dependency_imports(
        ast.parse(source),
        profile=PROFILE,
        stdlib_roots=stdlib_roots,
        platform_modules=platform_modules,
    )


@pytest.mark.parametrize(
    "name",
    [
        "DEBUGGER_PLATFORM_IMPORT_POLICY",
        "ImportedModule",
        "DependencyValidationIssue",
        "DynamicImportSafetyIssue",
        "HOSTED_PLATFORM_IMPORT_POLICY",
        "PlatformImportPolicy",
        "SDK_PLATFORM_IMPORT_POLICY",
        "find_spec_without_import",
        "iter_imported_modules",
        "validate_dependency_imports",
        "validate_dynamic_import_safety",
        "validate_platform_import_safety",
    ],
)
def test_shared_import_validation_api_is_exported_from_package_root(name):
    assert getattr(hushine_strategy, name) is getattr(import_validation, name)


def test_debugger_platform_policy_is_exactly_the_sdk_policy():
    assert (
        import_validation.DEBUGGER_PLATFORM_IMPORT_POLICY
        is SDK_PLATFORM_IMPORT_POLICY
    )


@pytest.mark.parametrize("root", PROFILE.public_import_roots)
def test_each_manifest_root_is_allowed(root):
    assert _dependency_issues(
        f"import {root}",
        platform_modules=frozenset({"hushine_strategy"}),
    ) == ()


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
def test_non_contract_modules_are_not_public(module):
    issues = _dependency_issues(f"import {module}")
    assert [(issue.code, issue.module) for issue in issues] == [
        ("UNSUPPORTED_STRATEGY_DEPENDENCY", module)
    ]


@pytest.mark.parametrize(
    "module",
    [
        "pandas.io.common",
        "requests.packages.urllib3",
        "numpy.missing_contract_test_submodule",
    ],
)
def test_manifest_root_authorizes_complete_dotted_module_without_availability_lookup(module):
    assert _dependency_issues(f"import {module}") == ()


@pytest.mark.parametrize("module", ["os.path", "collections.abc"])
def test_stdlib_dotted_aliases_do_not_require_pathfinder_resolution(module):
    assert _dependency_issues(
        f"import {module}",
        stdlib_roots=frozenset({"os", "collections"}),
    ) == ()


def test_standard_library_policy_is_owned_by_caller():
    assert _dependency_issues("import json", stdlib_roots=frozenset({"json"})) == ()
    assert [(issue.code, issue.module) for issue in _dependency_issues("import json")] == [
        ("UNSUPPORTED_STRATEGY_DEPENDENCY", "json")
    ]


def test_import_collector_keeps_requested_modules_and_not_imported_symbols():
    tree = ast.parse(
        "import pandas.io.common as common\n"
        "from pandas.io import common\n"
        "from pandas import DataFrame\n"
    )
    assert iter_imported_modules(tree) == (
        ImportedModule(module="pandas.io.common", root="pandas", line=1),
        ImportedModule(module="pandas.io", root="pandas", line=2),
        ImportedModule(module="pandas", root="pandas", line=3),
    )


def test_import_collector_records_every_plain_import_alias():
    assert iter_imported_modules(ast.parse("import numpy, requests.sessions")) == (
        ImportedModule(module="numpy", root="numpy", line=1),
        ImportedModule(module="requests.sessions", root="requests", line=1),
    )


@pytest.mark.parametrize(
    "source",
    [
        "from . import x",
        "from .hushine_strategy import X",
        "from ..pandas import X",
    ],
)
def test_import_collector_excludes_relative_imports(source):
    assert iter_imported_modules(ast.parse(source)) == ()


@pytest.mark.parametrize(
    ("module", "allowed"),
    [
        ("strategy_service.types", True),
        ("strategy_service", False),
        ("strategy_service.types.child", False),
        ("strategy_service.wallet", False),
        ("strategy_service.types_evil", False),
    ],
)
def test_platform_module_permission_is_exact(module, allowed):
    issues = _dependency_issues(
        f"import {module}",
        platform_modules=frozenset({"strategy_service.types"}),
    )
    assert (issues == ()) is allowed
    if not allowed:
        assert [(issue.code, issue.module) for issue in issues] == [
            ("UNSUPPORTED_STRATEGY_DEPENDENCY", module)
        ]


@pytest.mark.parametrize(
    "source",
    [
        "from hushine_strategy import Exchange, Market, OrderDecision",
        "from hushine_strategy.types import MarketData, OrderFill, OrderUpdateEvent",
        "from hushine_strategy.inputs import InputView, StrategyInput, StrategyRiskControls",
        "from hushine_strategy.wallet import FuturesWallet",
        "from hushine_strategy.wallet.futures import FuturesWallet",
    ],
)
def test_sdk_platform_policy_allows_only_declared_from_import_forms(source):
    assert validate_platform_import_safety(
        ast.parse(source), policy=SDK_PLATFORM_IMPORT_POLICY
    ) == ()


def test_hosted_platform_policy_adds_declared_strategy_service_types():
    assert validate_platform_import_safety(
        ast.parse(
            "from strategy_service.types import "
            "Exchange, ExecutionFeedback, MarketData, OrderDecision"
        ),
        policy=HOSTED_PLATFORM_IMPORT_POLICY,
    ) == ()


def test_platform_policies_are_exact_immutable_symbol_surfaces():
    sdk_modules = dict(SDK_PLATFORM_IMPORT_POLICY.allowed_from_symbols)
    assert SDK_PLATFORM_IMPORT_POLICY.protected_roots == (
        "hushine_strategy",
        "strategy_service",
    )
    assert sdk_modules == {
        "hushine_strategy": (
            "Exchange",
            "InputView",
            "Market",
            "MarketData",
            "OrderDecision",
            "OrderFill",
            "OrderSide",
            "OrderType",
            "PositionSide",
            "StrategyInput",
            "StrategyOrderTarget",
        ),
        "hushine_strategy.inputs": (
            "InputView",
            "StrategyInput",
            "StrategyOrderTarget",
            "StrategyRiskControls",
        ),
        "hushine_strategy.types": (
            "Exchange",
            "Market",
            "MarketData",
            "OrderDecision",
            "OrderFill",
            "OrderSide",
            "OrderType",
            "OrderUpdateEvent",
            "OrderUpdateFill",
            "PositionSide",
        ),
        "hushine_strategy.wallet": ("FuturesWallet",),
        "hushine_strategy.wallet.futures": ("FuturesWallet",),
    }
    hosted_modules = dict(HOSTED_PLATFORM_IMPORT_POLICY.allowed_from_symbols)
    assert {
        module: symbols
        for module, symbols in hosted_modules.items()
        if module != "strategy_service.types"
    } == sdk_modules
    assert hosted_modules["strategy_service.types"] == (
        "Exchange",
        "ExecutionFeedback",
        "Market",
        "MarketData",
        "OrderDecision",
        "OrderFill",
        "OrderResponse",
        "OrderSide",
        "OrderType",
        "OrderUpdateEvent",
        "OrderUpdateFill",
        "PositionSide",
    )


@pytest.mark.parametrize(
    ("source", "module"),
    [
        ("import hushine_strategy", "hushine_strategy"),
        ("import hushine_strategy.types as sdk", "hushine_strategy.types"),
        ("import strategy_service.types", "strategy_service.types"),
        ("from hushine_strategy import LocalNotifier", "hushine_strategy"),
        ("from hushine_strategy import runtime_dependencies as rd", "hushine_strategy"),
        ("from hushine_strategy.runtime_dependencies import subprocess", "hushine_strategy.runtime_dependencies"),
        ("from hushine_strategy.notifier import Path", "hushine_strategy.notifier"),
        ("from hushine_strategy.replay import run_replay", "hushine_strategy.replay"),
        ("from hushine_strategy.validator import validate_strategy_code", "hushine_strategy.validator"),
        ("from strategy_service import StrategyEngine", "strategy_service"),
        ("from strategy_service.types import BaseModel", "strategy_service.types"),
        ("from hushine_strategy.types import *", "hushine_strategy.types"),
    ],
)
def test_platform_policy_rejects_module_handles_and_non_surface_symbols(source, module):
    issues = validate_platform_import_safety(
        ast.parse(source), policy=HOSTED_PLATFORM_IMPORT_POLICY
    )
    assert issues
    assert {issue.code for issue in issues} == {"forbidden_import"}
    assert {issue.module for issue in issues} == {module}


@pytest.mark.parametrize(
    "source",
    [
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
    ],
)
def test_platform_policy_closes_installed_module_handle_bypasses(source):
    issues = validate_platform_import_safety(
        ast.parse(source), policy=HOSTED_PLATFORM_IMPORT_POLICY
    )
    assert issues
    assert {issue.code for issue in issues} == {"forbidden_import"}


def test_dependency_issue_keeps_complete_source_requested_module():
    issues = _dependency_issues("import talib.child")
    assert [(issue.code, issue.module) for issue in issues] == [
        ("UNSUPPORTED_STRATEGY_DEPENDENCY", "talib.child")
    ]


def test_dependency_issues_are_sorted_and_deduplicated_by_source_location():
    issues = _dependency_issues("import zstandard\nimport talib, talib")
    assert [(issue.line, issue.module, issue.code) for issue in issues] == [
        (1, "zstandard", "UNSUPPORTED_STRATEGY_DEPENDENCY"),
        (2, "talib", "UNSUPPORTED_STRATEGY_DEPENDENCY"),
    ]


def test_complete_path_finder_never_imports_parent_package(tmp_path, monkeypatch):
    package = tmp_path / "explosive_parent"
    marker = tmp_path / "parent-imported"
    package.mkdir()
    (package / "__init__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        "raise AssertionError('parent package executed')\n",
        encoding="utf-8",
    )
    (package / "child.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("explosive_parent", None)

    assert find_spec_without_import("explosive_parent.child") is not None
    assert "explosive_parent" not in sys.modules
    assert not marker.exists()


def test_complete_path_finder_supports_builtin_and_missing_modules():
    assert find_spec_without_import("sys") is not None
    assert find_spec_without_import("module_that_does_not_exist_for_hushine") is None


def test_complete_path_finder_returns_none_for_non_package_parent(tmp_path, monkeypatch):
    (tmp_path / "plain_parent.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert find_spec_without_import("plain_parent.child") is None
    assert "plain_parent" not in sys.modules


def test_complete_path_finder_safely_hides_locator_exceptions(monkeypatch):
    def explode(fullname, path=None, target=None):
        raise RuntimeError("secret path: /do/not/expose")

    monkeypatch.setattr(importlib.machinery.PathFinder, "find_spec", explode)
    assert find_spec_without_import("anything") is None


@pytest.mark.parametrize(
    "root",
    [
        "builtins",
        "importlib",
        "marshal",
        "modulefinder",
        "pickle",
        "pkgutil",
        "pydoc",
        "runpy",
        "shelve",
        "zipimport",
    ],
)
def test_each_hosted_dynamic_import_root_is_an_ordinary_safety_issue(root):
    issues = validate_dynamic_import_safety(ast.parse(f"import {root}"))
    assert [(issue.code, issue.module, issue.symbol, issue.line) for issue in issues] == [
        ("forbidden_import", root, "", 1)
    ]


@pytest.mark.parametrize(
    "symbol",
    ["__import__", "compile", "eval", "exec", "globals", "locals", "vars"],
)
def test_each_hosted_dynamic_call_is_an_ordinary_safety_issue(symbol):
    issues = validate_dynamic_import_safety(ast.parse(f"{symbol}('payload')"))
    assert {issue.code for issue in issues} == {"forbidden_call"}
    assert any(issue.symbol == symbol for issue in issues)


@pytest.mark.parametrize(
    "source",
    [
        "from helper import eval as load\nload('payload')",
        "load = __import__\nload('kafka')",
        "(load := __import__)('kafka')",
    ],
)
def test_dynamic_call_aliases_are_rejected_without_execution(source):
    issues = validate_dynamic_import_safety(ast.parse(source))
    assert {issue.code for issue in issues} == {"forbidden_call"}


@pytest.mark.parametrize(
    ("source", "expected_code"),
    [
        ('import importlib; importlib.import_module("kafka")', "forbidden_import"),
        ('from importlib import import_module as load; load("psycopg2")', "forbidden_import"),
        ('loader = __import__; loader("cryptography")', "forbidden_call"),
        ('(loader := __import__)("kafka")', "forbidden_call"),
        ('exec("import kafka")', "forbidden_call"),
        ('getattr(__builtins__, "__import__")("kafka")', "forbidden_builtin_access"),
        ('vars(__builtins__)["__import__"]("kafka")', "forbidden_builtin_access"),
        ('globals()["__builtins__"]["__import__"]("kafka")', "forbidden_builtin_access"),
    ]
    + [
        (source, "forbidden_builtin_access")
        for source in REFLECTIVE_BUILTINS_BYPASSES
    ]
    + [
        (source, "forbidden_call")
        for source in NESTED_ASSIGNMENT_BYPASSES
    ]
    + [
        (source, "forbidden_call")
        for source in HANDLE_ACQUISITION_BYPASSES
    ],
)
def test_dynamic_loading_matrix_has_expected_safety_category(source, expected_code):
    issues = validate_dynamic_import_safety(ast.parse(source))
    assert issues
    assert expected_code in {issue.code for issue in issues}
    assert all(issue.code != "UNSUPPORTED_STRATEGY_DEPENDENCY" for issue in issues)


def test_literal_getattr_forbidden_symbol_is_rejected_but_normal_getattr_is_allowed():
    unsafe = validate_dynamic_import_safety(
        ast.parse('getattr(strategy, "__import__")("kafka")')
    )
    assert {issue.code for issue in unsafe} == {"forbidden_call"}
    assert validate_dynamic_import_safety(
        ast.parse('getattr(data, "indicators", None)')
    ) == ()


@pytest.mark.parametrize("source", REFLECTIVE_BUILTINS_BYPASSES)
def test_literal_getattr_builtins_containers_propagate_to_dynamic_call(source):
    issues = validate_dynamic_import_safety(ast.parse(source))
    assert {"forbidden_builtin_access", "forbidden_call"} <= {
        issue.code for issue in issues
    }


def test_reverse_alias_chain_uses_near_linear_origin_evaluations(monkeypatch):
    assignment_count = 2_000
    source = _reverse_forbidden_alias_chain(assignment_count)
    original_forbidden_origin = import_validation._forbidden_origin
    evaluation_count = 0

    def counting_forbidden_origin(*args, **kwargs):
        nonlocal evaluation_count
        evaluation_count += 1
        return original_forbidden_origin(*args, **kwargs)

    monkeypatch.setattr(
        import_validation,
        "_forbidden_origin",
        counting_forbidden_origin,
    )
    issues = validate_dynamic_import_safety(ast.parse(source))

    assert any(
        issue.code == "forbidden_call" and issue.symbol == "alias_0"
        for issue in issues
    )
    assert evaluation_count <= 12 * assignment_count + 100


def test_alias_cycle_terminates_and_propagates_forbidden_origin(monkeypatch):
    source = (
        "left = right\n"
        "right = left\n"
        "right = __import__\n"
        'left("kafka")'
    )
    original_forbidden_origin = import_validation._forbidden_origin
    evaluation_count = 0

    def counting_forbidden_origin(*args, **kwargs):
        nonlocal evaluation_count
        evaluation_count += 1
        return original_forbidden_origin(*args, **kwargs)

    monkeypatch.setattr(
        import_validation,
        "_forbidden_origin",
        counting_forbidden_origin,
    )
    issues = validate_dynamic_import_safety(ast.parse(source))

    assert any(
        issue.code == "forbidden_call" and issue.symbol == "left"
        for issue in issues
    )
    assert evaluation_count <= 12 * 3 + 100


def test_multiple_assignments_keep_first_deterministic_forbidden_origin():
    source = (
        "loader = eval\n"
        "loader = exec\n"
        "child = loader\n"
        'child("payload")'
    )
    first = validate_dynamic_import_safety(ast.parse(source))
    second = validate_dynamic_import_safety(ast.parse(source))

    assert first == second
    line_three_symbols = [
        (issue.line, issue.symbol)
        for issue in first
        if issue.code == "forbidden_call" and issue.line == 3
    ]
    assert (3, "eval") in line_three_symbols
    assert (3, "exec") not in line_three_symbols


@pytest.mark.parametrize("source", NESTED_ASSIGNMENT_BYPASSES)
def test_nested_assignment_forbidden_leaf_is_never_dropped(source):
    issues = validate_dynamic_import_safety(ast.parse(source))
    assert any(
        issue.code == "forbidden_call" and issue.symbol == "loader"
        for issue in issues
    )


def test_matching_nested_assignment_only_propagates_corresponding_leaf():
    issues = validate_dynamic_import_safety(
        ast.parse(
            "(safe, (loader,)) = (len, (__import__,))\n"
            "safe([1])\n"
            'loader("kafka")'
        )
    )
    assert not any(issue.symbol == "safe" for issue in issues)
    assert any(
        issue.code == "forbidden_call" and issue.symbol == "loader"
        for issue in issues
    )


def test_normal_nested_assignment_remains_valid():
    assert validate_dynamic_import_safety(
        ast.parse("((safe,),) = ((len,),)\nsafe([1])")
    ) == ()


@pytest.mark.parametrize("source", HANDLE_ACQUISITION_BYPASSES)
def test_closed_callable_handle_acquisition_is_forbidden(source):
    issues = validate_dynamic_import_safety(ast.parse(source))
    assert any(issue.code == "forbidden_call" for issue in issues)


@pytest.mark.parametrize("source", SAFE_HANDLE_USES)
def test_ordinary_safe_defaults_and_arguments_remain_valid(source):
    assert validate_dynamic_import_safety(ast.parse(source)) == ()


def test_known_forbidden_alias_load_as_argument_is_forbidden():
    issues = validate_dynamic_import_safety(
        ast.parse("loader = __import__\nconsume(loader)")
    )
    assert any(
        issue.code == "forbidden_call" and issue.symbol == "loader"
        for issue in issues
    )


def test_direct_closed_call_deduplicates_name_load_and_call_issue():
    issues = validate_dynamic_import_safety(ast.parse('__import__("kafka")'))
    assert [
        issue
        for issue in issues
        if issue.code == "forbidden_call" and issue.symbol == "__import__"
    ] == [issues[0]]


@pytest.mark.parametrize("source", BUILTINS_IMPORT_ALIAS_BYPASSES)
def test_imported_builtins_containers_cannot_smuggle_dynamic_import(source):
    issues = validate_dynamic_import_safety(ast.parse(source))
    assert "forbidden_builtin_access" in {issue.code for issue in issues}
    assert "forbidden_call" in {issue.code for issue in issues}
    assert all(issue.code != "UNSUPPORTED_STRATEGY_DEPENDENCY" for issue in issues)


def test_dynamic_safety_dedup_preserves_distinct_same_line_symbols():
    issues = validate_dynamic_import_safety(
        ast.parse("from requests import eval, exec")
    )
    assert [issue.symbol for issue in issues if issue.code == "forbidden_call"] == [
        "eval",
        "exec",
    ]


def test_platform_safety_dedup_preserves_distinct_same_line_symbols():
    issues = validate_platform_import_safety(
        ast.parse(
            "from hushine_strategy import LocalNotifier, runtime_dependencies"
        ),
        policy=SDK_PLATFORM_IMPORT_POLICY,
    )
    assert [issue.symbol for issue in issues] == [
        "LocalNotifier",
        "runtime_dependencies",
    ]
    keys = [
        (issue.line, issue.module, issue.symbol, issue.code)
        for issue in issues
    ]
    assert keys == sorted(keys)


@pytest.mark.parametrize(
    "value",
    [
        ImportedModule("numpy", "numpy", 1),
        DependencyValidationIssue("code", "module", 1, "message"),
        DynamicImportSafetyIssue("code", "module", "symbol", 1, "message"),
        PlatformImportPolicy(("root",), (("module", ("Symbol",)),)),
    ],
)
def test_shared_issue_values_are_immutable(value):
    with pytest.raises(FrozenInstanceError):
        value.line = 2
