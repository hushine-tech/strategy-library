from __future__ import annotations

import ast
from dataclasses import dataclass
from importlib.machinery import BuiltinImporter, FrozenImporter, PathFinder
from typing import AbstractSet

from hushine_strategy.runtime_dependencies import RuntimeDependencyProfile


HOSTED_DYNAMIC_IMPORT_ROOTS: frozenset[str] = frozenset(
    {
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
    }
)
HOSTED_DYNAMIC_IMPORT_CALLS: frozenset[str] = frozenset(
    {"__import__", "compile", "eval", "exec", "globals", "locals", "vars"}
)


@dataclass(frozen=True)
class ImportedModule:
    module: str
    root: str
    line: int


@dataclass(frozen=True)
class DependencyValidationIssue:
    code: str
    module: str
    line: int
    message: str


@dataclass(frozen=True)
class DynamicImportSafetyIssue:
    code: str
    module: str
    symbol: str
    line: int
    message: str


@dataclass(frozen=True)
class PlatformImportPolicy:
    protected_roots: tuple[str, ...]
    allowed_from_symbols: tuple[tuple[str, tuple[str, ...]], ...]


_SDK_ALLOWED_FROM_SYMBOLS = (
    (
        "hushine_strategy",
        (
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
    ),
    (
        "hushine_strategy.inputs",
        (
            "InputView",
            "StrategyInput",
            "StrategyOrderTarget",
            "StrategyRiskControls",
        ),
    ),
    (
        "hushine_strategy.types",
        (
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
    ),
    ("hushine_strategy.wallet", ("FuturesWallet",)),
    ("hushine_strategy.wallet.futures", ("FuturesWallet",)),
)
_HOSTED_ONLY_ALLOWED_FROM_SYMBOLS = (
    (
        "strategy_service.types",
        (
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
        ),
    ),
)

SDK_PLATFORM_IMPORT_POLICY = PlatformImportPolicy(
    protected_roots=("hushine_strategy", "strategy_service"),
    allowed_from_symbols=_SDK_ALLOWED_FROM_SYMBOLS,
)
DEBUGGER_PLATFORM_IMPORT_POLICY = SDK_PLATFORM_IMPORT_POLICY
HOSTED_PLATFORM_IMPORT_POLICY = PlatformImportPolicy(
    protected_roots=("hushine_strategy", "strategy_service"),
    allowed_from_symbols=tuple(
        sorted(_SDK_ALLOWED_FROM_SYMBOLS + _HOSTED_ONLY_ALLOWED_FROM_SYMBOLS)
    ),
)


def _line(node: ast.AST) -> int:
    return int(getattr(node, "lineno", 0) or 0)


def _root(module: str) -> str:
    return module.split(".", 1)[0]


def _subscript_key(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _deduplicate_dependency_issues(
    issues: list[DependencyValidationIssue],
) -> tuple[DependencyValidationIssue, ...]:
    unique: dict[tuple[int, str, str], DependencyValidationIssue] = {}
    for issue in issues:
        unique.setdefault((issue.line, issue.module, issue.code), issue)
    return tuple(
        sorted(
            unique.values(),
            key=lambda issue: (issue.line, issue.module, issue.code),
        )
    )


def _deduplicate_safety_issues(
    issues: list[DynamicImportSafetyIssue],
) -> tuple[DynamicImportSafetyIssue, ...]:
    unique: dict[tuple[int, str, str, str], DynamicImportSafetyIssue] = {}
    for issue in issues:
        unique.setdefault(
            (issue.line, issue.module, issue.symbol, issue.code),
            issue,
        )
    return tuple(
        sorted(
            unique.values(),
            key=lambda issue: (
                issue.line,
                issue.module,
                issue.symbol,
                issue.code,
                issue.message,
            ),
        )
    )


def iter_imported_modules(tree: ast.AST) -> tuple[ImportedModule, ...]:
    imported: list[tuple[int, ImportedModule]] = []
    sequence = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.append(
                    (
                        sequence,
                        ImportedModule(
                            module=alias.name,
                            root=_root(alias.name),
                            line=_line(node),
                        ),
                    )
                )
                sequence += 1
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
        ):
            imported.append(
                (
                    sequence,
                    ImportedModule(
                        module=node.module,
                        root=_root(node.module),
                        line=_line(node),
                    ),
                )
            )
            sequence += 1
    return tuple(
        item
        for _, item in sorted(
            imported,
            key=lambda entry: (entry[1].line, entry[0]),
        )
    )


def find_spec_without_import(module: str) -> object | None:
    parts = module.split(".")
    if not parts or any(not part for part in parts):
        return None

    search_path = None
    spec = None
    fullname = ""
    try:
        for index, part in enumerate(parts):
            fullname = part if not fullname else f"{fullname}.{part}"
            spec = BuiltinImporter.find_spec(fullname)
            if spec is None:
                spec = FrozenImporter.find_spec(fullname)
            if spec is None:
                spec = PathFinder.find_spec(fullname, search_path)
            if spec is None:
                return None
            if index < len(parts) - 1:
                search_path = spec.submodule_search_locations
                if search_path is None:
                    return None
    except Exception:
        return None
    return spec


def validate_dependency_imports(
    tree: ast.AST,
    *,
    profile: RuntimeDependencyProfile,
    stdlib_roots: AbstractSet[str],
    platform_modules: AbstractSet[str],
) -> tuple[DependencyValidationIssue, ...]:
    public_roots = frozenset(profile.public_import_roots)
    issues: list[DependencyValidationIssue] = []
    for imported in iter_imported_modules(tree):
        if imported.module in platform_modules:
            continue
        if imported.root in stdlib_roots or imported.root in public_roots:
            continue
        issues.append(
            DependencyValidationIssue(
                code="UNSUPPORTED_STRATEGY_DEPENDENCY",
                module=imported.module,
                line=imported.line,
                message=(
                    f"module {imported.module!r} is not part of runtime "
                    f"dependency profile {profile.profile_name!r}"
                ),
            )
        )
    return _deduplicate_dependency_issues(issues)


def validate_platform_import_safety(
    tree: ast.AST,
    *,
    policy: PlatformImportPolicy,
) -> tuple[DynamicImportSafetyIssue, ...]:
    protected_roots = frozenset(policy.protected_roots)
    allowed_from_symbols = {
        module: frozenset(symbols)
        for module, symbols in policy.allowed_from_symbols
    }
    issues: list[DynamicImportSafetyIssue] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _root(alias.name) not in protected_roots:
                    continue
                issues.append(
                    DynamicImportSafetyIssue(
                        code="forbidden_import",
                        module=alias.name,
                        symbol="",
                        line=_line(node),
                        message=(
                            f"import {alias.name} binds a protected platform "
                            "module object"
                        ),
                    )
                )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
            and _root(node.module) in protected_roots
        ):
            allowed_symbols = allowed_from_symbols.get(node.module, frozenset())
            for alias in node.names:
                if alias.name != "*" and alias.name in allowed_symbols:
                    continue
                issues.append(
                    DynamicImportSafetyIssue(
                        code="forbidden_import",
                        module=node.module,
                        symbol=alias.name,
                        line=_line(node),
                        message=(
                            f"from {node.module} import {alias.name} is outside "
                            "the strategy platform surface"
                        ),
                    )
                )
    return _deduplicate_safety_issues(issues)


def _assignment_target_names(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(
            name
            for child in node.elts
            for name in _assignment_target_names(child)
        )
    return ()


def _assignment_pairs(
    target: ast.AST,
    value: ast.expr,
) -> tuple[tuple[tuple[str, ...], ast.expr], ...]:
    if (
        isinstance(target, (ast.Tuple, ast.List))
        and isinstance(value, (ast.Tuple, ast.List))
        and len(target.elts) == len(value.elts)
    ):
        return tuple(
            (_assignment_target_names(target_item), value_item)
            for target_item, value_item in zip(
                target.elts,
                value.elts,
                strict=True,
            )
        )
    return ((_assignment_target_names(target), value),)


def _iter_assignments(
    tree: ast.AST,
) -> tuple[tuple[tuple[str, ...], ast.expr, int], ...]:
    assignments: list[tuple[tuple[str, ...], ast.expr, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                assignments.extend(
                    (names, value, _line(node))
                    for names, value in _assignment_pairs(target, node.value)
                )
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            assignments.extend(
                (names, value, _line(node))
                for names, value in _assignment_pairs(node.target, node.value)
            )
        elif isinstance(node, ast.NamedExpr):
            assignments.extend(
                (names, value, _line(node))
                for names, value in _assignment_pairs(node.target, node.value)
            )
    return tuple(assignments)


def _is_builtins_container(
    node: ast.expr,
    builtins_aliases: AbstractSet[str],
) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "__builtins__" or node.id in builtins_aliases
    if isinstance(node, ast.Attribute):
        if node.attr == "__builtins__":
            return True
        return node.attr == "__dict__" and _is_builtins_container(
            node.value,
            builtins_aliases,
        )
    if isinstance(node, ast.Subscript):
        return _subscript_key(node.slice) == "__builtins__"
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "vars"
        and node.args
    ):
        return _is_builtins_container(node.args[0], builtins_aliases)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and _is_builtins_container(node.func.value, builtins_aliases)
        and node.args
        and _subscript_key(node.args[0]) == "__builtins__"
    ):
        return True
    return False


def _builtins_lookup_symbol(
    node: ast.expr,
    builtins_aliases: AbstractSet[str],
    forbidden_calls: AbstractSet[str],
) -> str:
    if (
        isinstance(node, ast.Subscript)
        and _is_builtins_container(node.value, builtins_aliases)
    ):
        symbol = _subscript_key(node.slice)
        return symbol if symbol in forbidden_calls else ""
    if (
        isinstance(node, ast.Attribute)
        and _is_builtins_container(node.value, builtins_aliases)
        and node.attr in forbidden_calls
    ):
        return node.attr
    if isinstance(node, ast.Call):
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and _is_builtins_container(node.func.value, builtins_aliases)
            and node.args
        ):
            symbol = _subscript_key(node.args[0])
            return symbol if symbol in forbidden_calls else ""
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
        ):
            symbol = _subscript_key(node.args[1])
            if symbol in forbidden_calls:
                return symbol
    return ""


def _forbidden_origin(
    node: ast.expr,
    forbidden_aliases: dict[str, tuple[str, str]],
    builtins_aliases: AbstractSet[str],
    forbidden_calls: AbstractSet[str],
) -> tuple[str, str] | None:
    if isinstance(node, ast.NamedExpr):
        return _forbidden_origin(
            node.value,
            forbidden_aliases,
            builtins_aliases,
            forbidden_calls,
        )
    symbol = _builtins_lookup_symbol(
        node,
        builtins_aliases,
        forbidden_calls,
    )
    if symbol:
        return ("", symbol)
    if isinstance(node, ast.Name):
        if node.id == "getattr":
            return None
        if node.id in forbidden_aliases:
            return forbidden_aliases[node.id]
        if node.id in forbidden_calls:
            return ("", node.id)
    if isinstance(node, ast.Attribute) and node.attr in forbidden_calls:
        return ("", node.attr)
    return None


def _explicit_builtins_symbol(
    node: ast.AST,
    builtins_aliases: AbstractSet[str],
) -> str:
    if isinstance(node, ast.Name) and node.id == "__builtins__":
        return "__builtins__"
    if isinstance(node, ast.Attribute):
        if node.attr == "__builtins__":
            return "__builtins__"
        if node.attr == "__dict__":
            if _is_builtins_container(node.value, builtins_aliases):
                return "__builtins__"
            return "__dict__"
    if (
        isinstance(node, ast.Subscript)
        and _subscript_key(node.slice) == "__builtins__"
    ):
        return "__builtins__"
    return ""


def validate_dynamic_import_safety(
    tree: ast.AST,
    *,
    forbidden_import_roots: AbstractSet[str] = HOSTED_DYNAMIC_IMPORT_ROOTS,
    forbidden_calls: AbstractSet[str] = HOSTED_DYNAMIC_IMPORT_CALLS,
) -> tuple[DynamicImportSafetyIssue, ...]:
    forbidden_roots = frozenset(forbidden_import_roots)
    closed_calls = frozenset(forbidden_calls)
    assignments = _iter_assignments(tree)
    forbidden_aliases: dict[str, tuple[str, str]] = {}
    builtins_aliases: set[str] = set()
    imported_builtins_accesses: list[DynamicImportSafetyIssue] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            for alias in node.names:
                if alias.name in {"__builtins__", "__dict__"}:
                    builtins_aliases.add(alias.asname or alias.name)
                    imported_builtins_accesses.append(
                        DynamicImportSafetyIssue(
                            code="forbidden_builtin_access",
                            module=module,
                            symbol=alias.name,
                            line=_line(node),
                            message=(
                                f"imported {alias.name} access is not allowed "
                                "in strategy code"
                            ),
                        )
                    )
                if alias.name in closed_calls:
                    forbidden_aliases[alias.asname or alias.name] = (
                        module,
                        alias.name,
                    )

    for _ in range(len(assignments) + 1):
        changed = False
        for names, value, _ in assignments:
            if _is_builtins_container(value, builtins_aliases):
                for name in names:
                    if name not in builtins_aliases:
                        builtins_aliases.add(name)
                        changed = True
            origin = _forbidden_origin(
                value,
                forbidden_aliases,
                builtins_aliases,
                closed_calls,
            )
            if origin is not None:
                for name in names:
                    if forbidden_aliases.get(name) != origin:
                        forbidden_aliases[name] = origin
                        changed = True
        if not changed:
            break

    issues: list[DynamicImportSafetyIssue] = list(imported_builtins_accesses)
    for _, value, line in assignments:
        origin = _forbidden_origin(
            value,
            forbidden_aliases,
            builtins_aliases,
            closed_calls,
        )
        if origin is not None:
            module, symbol = origin
            issues.append(
                DynamicImportSafetyIssue(
                    code="forbidden_call",
                    module=module,
                    symbol=symbol,
                    line=line,
                    message=f"alias to {symbol} is not allowed in strategy code",
                )
            )

    for node in ast.walk(tree):
        explicit_symbol = _explicit_builtins_symbol(node, builtins_aliases)
        if explicit_symbol:
            issues.append(
                DynamicImportSafetyIssue(
                    code="forbidden_builtin_access",
                    module="",
                    symbol=explicit_symbol,
                    line=_line(node),
                    message=(
                        f"explicit {explicit_symbol} access is not allowed "
                        "in strategy code"
                    ),
                )
            )

        if isinstance(node, ast.Import):
            for alias in node.names:
                if _root(alias.name) in forbidden_roots:
                    issues.append(
                        DynamicImportSafetyIssue(
                            code="forbidden_import",
                            module=alias.name,
                            symbol="",
                            line=_line(node),
                            message=f"import {alias.name} is not allowed in strategy code",
                        )
                    )
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            if _root(module) in forbidden_roots:
                issues.append(
                    DynamicImportSafetyIssue(
                        code="forbidden_import",
                        module=module,
                        symbol="",
                        line=_line(node),
                        message=f"from {module} import is not allowed in strategy code",
                    )
                )
            for alias in node.names:
                if alias.name in closed_calls:
                    issues.append(
                        DynamicImportSafetyIssue(
                            code="forbidden_call",
                            module=module,
                            symbol=alias.name,
                            line=_line(node),
                            message=(
                                f"imported call {alias.name} is not allowed "
                                "in strategy code"
                            ),
                        )
                    )

        if isinstance(node, ast.Call):
            origin = _forbidden_origin(
                node.func,
                forbidden_aliases,
                builtins_aliases,
                closed_calls,
            )
            if origin is not None:
                module, origin_symbol = origin
                symbol = origin_symbol
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id in forbidden_aliases
                ):
                    symbol = node.func.id
                issues.append(
                    DynamicImportSafetyIssue(
                        code="forbidden_call",
                        module=module,
                        symbol=symbol,
                        line=_line(node),
                        message=f"call {symbol} is not allowed in strategy code",
                    )
                )

    return _deduplicate_safety_issues(issues)
