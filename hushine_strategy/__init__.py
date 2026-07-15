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
_IMPORT_VALIDATION_EXPORTS = frozenset(
    {
        "DEBUGGER_PLATFORM_IMPORT_POLICY",
        "DependencyValidationIssue",
        "DynamicImportSafetyIssue",
        "HOSTED_PLATFORM_IMPORT_POLICY",
        "ImportedModule",
        "PlatformImportPolicy",
        "SDK_PLATFORM_IMPORT_POLICY",
        "find_spec_without_import",
        "iter_imported_modules",
        "validate_dependency_imports",
        "validate_dynamic_import_safety",
        "validate_platform_import_safety",
    }
)


def __getattr__(name: str):
    if name in _RUNTIME_DEPENDENCY_EXPORTS:
        from hushine_strategy import runtime_dependencies

        return getattr(runtime_dependencies, name)
    if name in _IMPORT_VALIDATION_EXPORTS:
        from hushine_strategy import import_validation

        return getattr(import_validation, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(
        set(globals())
        | _RUNTIME_DEPENDENCY_EXPORTS
        | _IMPORT_VALIDATION_EXPORTS
    )

__all__ = [
    "DEBUGGER_PLATFORM_IMPORT_POLICY",
    "DependencyProbeFailure",
    "DependencyValidationIssue",
    "DynamicImportSafetyIssue",
    "Exchange",
    "HOSTED_PLATFORM_IMPORT_POLICY",
    "InputView",
    "ImportedModule",
    "LocalNotifier",
    "Market",
    "MarketData",
    "OrderDecision",
    "OrderFill",
    "OrderSide",
    "OrderType",
    "PositionSide",
    "PlatformImportPolicy",
    "RuntimeDependency",
    "RuntimeDependencyProfile",
    "SDK_PLATFORM_IMPORT_POLICY",
    "StrategyInput",
    "StrategyOrderTarget",
    "find_spec_without_import",
    "iter_imported_modules",
    "parse_declared_inputs",
    "parse_order_targets",
    "load_runtime_dependency_profile",
    "probe_runtime_dependency_profile",
    "require_runtime_dependency_profile",
    "validate_dependency_imports",
    "validate_dynamic_import_safety",
    "validate_platform_import_safety",
]
