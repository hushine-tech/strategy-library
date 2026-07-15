from hushine_strategy.inputs import (
    InputView,
    StrategyInput,
    StrategyOrderTarget,
    parse_declared_inputs,
    parse_order_targets,
)
from hushine_strategy.notifier import LocalNotifier
from hushine_strategy.types import (
    Exchange,
    Market,
    MarketData,
    OrderDecision,
    OrderFill,
    OrderSide,
    OrderType,
    PositionSide,
)

_RUNTIME_DEPENDENCY_EXPORTS = frozenset(
    {
        "DependencyProbeFailure",
        "RuntimeDependency",
        "RuntimeDependencyProfile",
        "load_runtime_dependency_profile",
        "probe_runtime_dependency_profile",
        "require_runtime_dependency_profile",
    }
)


def __getattr__(name: str):
    if name in _RUNTIME_DEPENDENCY_EXPORTS:
        from hushine_strategy import runtime_dependencies

        return getattr(runtime_dependencies, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | _RUNTIME_DEPENDENCY_EXPORTS)

__all__ = [
    "DependencyProbeFailure",
    "Exchange",
    "InputView",
    "LocalNotifier",
    "Market",
    "MarketData",
    "OrderDecision",
    "OrderFill",
    "OrderSide",
    "OrderType",
    "PositionSide",
    "RuntimeDependency",
    "RuntimeDependencyProfile",
    "StrategyInput",
    "StrategyOrderTarget",
    "parse_declared_inputs",
    "parse_order_targets",
    "load_runtime_dependency_profile",
    "probe_runtime_dependency_profile",
    "require_runtime_dependency_profile",
]
