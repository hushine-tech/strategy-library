from __future__ import annotations

import types
from dataclasses import dataclass
from inspect import getmodule
from typing import Iterable

from hushine_strategy.inputs import InputView, parse_declared_inputs, parse_order_targets
from hushine_strategy.notifier import LocalNotifier
from hushine_strategy.types import Market, MarketData, OrderDecision
from hushine_strategy.validator import ALLOWED_IMPORT_ROOTS, FORBIDDEN_IMPORT_ROOTS, validate_strategy_code
from hushine_strategy.wallet.futures import FuturesWallet


def _root(name: str) -> str:
    return str(name).split(".", 1)[0]


def _is_forbidden_module(module: types.ModuleType) -> bool:
    root = _root(module.__name__)
    return root in FORBIDDEN_IMPORT_ROOTS or root not in ALLOWED_IMPORT_ROOTS


def _value_module_root(value) -> str:
    module_name = getattr(value, "__module__", "") or ""
    if not module_name:
        module = getmodule(value)
        module_name = getattr(module, "__name__", "") if module else ""
    return _root(module_name) if module_name else ""


def _is_forbidden_export(value) -> bool:
    if isinstance(value, types.ModuleType):
        return _is_forbidden_module(value)
    root = _value_module_root(value)
    return bool(root) and root in FORBIDDEN_IMPORT_ROOTS


def _safe_star_names(module: types.ModuleType) -> tuple[str, ...]:
    try:
        declared_names = getattr(module, "__all__")
    except AttributeError:
        candidate_names = sorted(
            name for name in vars(module) if not name.startswith("_")
        )
    else:
        candidate_names = tuple(declared_names)

    safe_names: list[str] = []
    for name in candidate_names:
        if not isinstance(name, str) or name.startswith("_"):
            continue
        try:
            value = getattr(module, name)
            _safe_import_value(value)
        except (AttributeError, ImportError):
            continue
        safe_names.append(name)
    return tuple(safe_names)


class _SafeModule:
    __slots__ = ("_module",)

    def __init__(self, module: types.ModuleType) -> None:
        object.__setattr__(self, "_module", module)

    def __getattribute__(self, name: str):
        if name in {"__name__", "__package__", "__doc__"}:
            module = object.__getattribute__(self, "_module")
            return getattr(module, name)
        if name == "__all__":
            module = object.__getattribute__(self, "_module")
            return _safe_star_names(module)
        if name.startswith("_") or name in {"__builtins__", "__dict__"}:
            raise AttributeError(f"module attribute {name} is not available in replay strategy code")
        module = object.__getattribute__(self, "_module")
        value = getattr(module, name)
        if _is_forbidden_export(value):
            raise AttributeError(f"module attribute {name} is not available in replay strategy code")
        return _safe_import_value(value)

    def __repr__(self) -> str:
        module = object.__getattribute__(self, "_module")
        return f"<safe module {module.__name__!r}>"


def _safe_import_value(value):
    if isinstance(value, types.ModuleType):
        if _is_forbidden_module(value):
            raise ImportError(f"import {value.__name__} is not allowed in replay strategy code")
        return _SafeModule(value)
    if _is_forbidden_export(value):
        module_name = getattr(value, "__module__", "")
        raise ImportError(f"import from {module_name} is not allowed in replay strategy code")
    return value


def _strategy_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = _root(name)
    if level != 0 or root in FORBIDDEN_IMPORT_ROOTS or root not in ALLOWED_IMPORT_ROOTS:
        raise ImportError(f"import {name} is not allowed in replay strategy code")
    module = __import__(name, globals, locals, fromlist, level)
    for item in fromlist or ():
        if item == "*":
            continue
        if _root(item) in FORBIDDEN_IMPORT_ROOTS:
            raise ImportError(f"from {name} import {item} is not allowed in replay strategy code")
        value = getattr(module, item, None)
        if _is_forbidden_export(value):
            raise ImportError(f"from {name} import {item} is not allowed in replay strategy code")
    return _safe_import_value(module)


_SAFE_BUILTINS = {
    "__build_class__": __build_class__,
    "__import__": _strategy_import,
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "float": float,
    "int": int,
    "isinstance": isinstance,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "range": range,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
}


@dataclass
class ReplayConfig:
    strategy_code: str
    ticks: Iterable[MarketData]
    wallet: FuturesWallet
    strategy_path: str = "strategy.py"
    notifier: LocalNotifier | None = None


@dataclass(frozen=True)
class ReplayResult:
    bars_processed: int
    orders_filled: int


def _load_strategy(code: str, strategy_path: str):
    result = validate_strategy_code(code)
    if not result.ok:
        first = result.issues[0]
        raise ValueError(f"strategy validation failed: {first.code}: {first.message}")
    ns: dict = {"__builtins__": _SAFE_BUILTINS, "__name__": "__hushine_replay_strategy__"}
    compiled = compile(code, strategy_path, "exec")
    try:
        exec(compiled, ns)
    except ImportError as exc:
        raise ValueError(f"strategy import failed: {exc}") from exc
    strategy_cls = ns["MyStrategy"]
    return strategy_cls()


def run_replay(config: ReplayConfig) -> ReplayResult:
    strategy = _load_strategy(config.strategy_code, config.strategy_path)
    setattr(strategy, "notify", config.notifier or LocalNotifier())
    inputs = parse_declared_inputs(getattr(strategy, "INPUTS", None))
    order_targets = parse_order_targets(getattr(strategy, "ORDER_TARGETS", None))
    for target in order_targets:
        if target.market != Market.PERPETUAL_FUTURES:
            raise ValueError(f"local replay only supports perpetual_futures ORDER_TARGETS, got {target.market}")
    order_target_keys = {target.key for target in order_targets}
    view = InputView(inputs)
    bars = 0
    orders = 0
    for tick in config.ticks:
        if not view.update(tick):
            continue
        config.wallet.update_mark_price(tick.symbol, tick.price)
        decision = strategy.on_market_data(view, config.wallet)
        bars += 1
        if isinstance(decision, OrderDecision):
            if not order_target_keys:
                raise ValueError("ORDER_TARGETS is empty; strategy cannot return OrderDecision")
            if str(decision.market).strip().lower() != Market.PERPETUAL_FUTURES:
                raise ValueError(f"local replay only supports perpetual_futures orders, got {decision.market}")
            decision_key = (
                str(decision.exchange).strip().lower(),
                str(decision.market).strip().lower(),
                str(decision.symbol).strip().upper(),
            )
            if decision_key not in order_target_keys:
                raise ValueError(f"order target {decision_key} is not declared in ORDER_TARGETS")
            config.wallet.fill_order(decision, float(decision.price or tick.price))
            orders += 1
    return ReplayResult(bars_processed=bars, orders_filled=orders)
