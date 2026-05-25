from __future__ import annotations

import ast
from dataclasses import dataclass


ALLOWED_IMPORT_ROOTS = {
    "bisect",
    "collections",
    "dataclasses",
    "datetime",
    "decimal",
    "enum",
    "functools",
    "heapq",
    "hushine_strategy",
    "itertools",
    "math",
    "numpy",
    "pandas",
    "pandas_ta",
    "random",
    "scipy",
    "sklearn",
    "statistics",
    "statsmodels",
    "ta",
    "typing",
}

FORBIDDEN_IMPORT_ROOTS = {
    "aiohttp",
    "asyncio",
    "ctypes",
    "httpx",
    "importlib",
    "marshal",
    "multiprocessing",
    "os",
    "pathlib",
    "pickle",
    "requests",
    "shutil",
    "socket",
    "subprocess",
    "sys",
    "threading",
    "urllib",
    "websocket",
}

FORBIDDEN_CALLS = {
    "__import__",
    "compile",
    "delattr",
    "dir",
    "eval",
    "exec",
    "getattr",
    "globals",
    "input",
    "locals",
    "open",
    "setattr",
    "vars",
}


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    module: str = ""
    symbol: str = ""
    line: int = 0


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    issues: list[ValidationIssue]
    strategy_profile: str = "base-futures-v1"


def _root(name: str) -> str:
    return str(name).split(".", 1)[0]


def _subscript_key(node: ast.expr) -> str:
    if isinstance(node, ast.Constant):
        return str(node.value)
    return ""


def _is_builtins_ref(node: ast.expr, builtins_aliases: set[str]) -> bool:
    return isinstance(node, ast.Name) and (node.id == "__builtins__" or node.id in builtins_aliases)


def _is_builtins_dict_ref(node: ast.expr, builtins_aliases: set[str]) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "__dict__"
        and _is_builtins_ref(node.value, builtins_aliases)
    )


def _is_builtins_alias_source(node: ast.expr, builtins_aliases: set[str]) -> bool:
    return _is_builtins_ref(node, builtins_aliases) or _is_builtins_dict_ref(node, builtins_aliases)


def _builtins_get_symbol(node: ast.Call, builtins_aliases: set[str]) -> str:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "get" or not node.args:
        return ""
    if not (
        _is_builtins_ref(func.value, builtins_aliases)
        or _is_builtins_dict_ref(func.value, builtins_aliases)
    ):
        return ""
    symbol = _subscript_key(node.args[0])
    if symbol in FORBIDDEN_CALLS:
        return symbol
    return ""


def _forbidden_call_symbol(node: ast.expr, forbidden_aliases: set[str], builtins_aliases: set[str]) -> str:
    if isinstance(node, ast.NamedExpr):
        return _forbidden_call_symbol(node.value, forbidden_aliases, builtins_aliases)
    if isinstance(node, ast.Call):
        return _builtins_get_symbol(node, builtins_aliases)
    if isinstance(node, ast.Name):
        if node.id in FORBIDDEN_CALLS or node.id in forbidden_aliases:
            return node.id
        return ""
    if isinstance(node, ast.Attribute):
        if node.attr in FORBIDDEN_CALLS:
            return node.attr
        if _is_builtins_ref(node.value, builtins_aliases) and node.attr in FORBIDDEN_CALLS:
            return node.attr
        return ""
    if (
        isinstance(node, ast.Subscript)
        and (
            _is_builtins_ref(node.value, builtins_aliases)
            or _is_builtins_dict_ref(node.value, builtins_aliases)
        )
    ):
        symbol = _subscript_key(node.slice)
        if symbol in FORBIDDEN_CALLS:
            return symbol
    return ""


def _assignment_target_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        names: list[str] = []
        for elt in node.elts:
            names.extend(_assignment_target_names(elt))
        return names
    return []


def _assignment_pairs(target: ast.AST, value: ast.expr) -> list[tuple[list[str], ast.expr]]:
    if (
        isinstance(target, (ast.Tuple, ast.List))
        and isinstance(value, (ast.Tuple, ast.List))
        and len(target.elts) == len(value.elts)
    ):
        return [
            (_assignment_target_names(target_elt), value_elt)
            for target_elt, value_elt in zip(target.elts, value.elts)
        ]
    return [(_assignment_target_names(target), value)]


def validate_strategy_code(code: str) -> ValidationResult:
    issues: list[ValidationIssue] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return ValidationResult(
            ok=False,
            issues=[ValidationIssue("syntax_error", str(exc), line=int(exc.lineno or 0))],
        )

    has_my_strategy = any(isinstance(node, ast.ClassDef) and node.name == "MyStrategy" for node in tree.body)
    forbidden_aliases: set[str] = set()
    builtins_aliases: set[str] = set()

    def record_aliases(targets: list[ast.AST], value: ast.expr, line: int) -> None:
        for target in targets:
            for names, assigned_value in _assignment_pairs(target, value):
                if _is_builtins_alias_source(assigned_value, builtins_aliases):
                    builtins_aliases.update(names)
                symbol = _forbidden_call_symbol(assigned_value, forbidden_aliases, builtins_aliases)
                if symbol:
                    for name in names:
                        forbidden_aliases.add(name)
                        issues.append(ValidationIssue(
                            code="forbidden_call",
                            message=f"alias {name} to {symbol} is not allowed in strategy code",
                            symbol=symbol,
                            line=line,
                        ))

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "__builtins__":
            issues.append(ValidationIssue(
                code="forbidden_builtin_access",
                message="explicit __builtins__ access is not allowed in strategy code",
                symbol="__builtins__",
                line=int(getattr(node, "lineno", 0) or 0),
            ))
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = _root(alias.name)
                if root in FORBIDDEN_IMPORT_ROOTS or root not in ALLOWED_IMPORT_ROOTS:
                    issues.append(ValidationIssue(
                        code="forbidden_import",
                        message=f"import {alias.name} is not allowed in base-futures-v1",
                        module=alias.name,
                        line=int(node.lineno),
                    ))
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = _root(module)
            if node.level > 0 or root in FORBIDDEN_IMPORT_ROOTS or root not in ALLOWED_IMPORT_ROOTS:
                issues.append(ValidationIssue(
                    code="forbidden_import",
                    message=f"from {module} import is not allowed in base-futures-v1",
                    module=module,
                    line=int(node.lineno),
                ))
        if isinstance(node, ast.Assign):
            record_aliases(node.targets, node.value, int(node.lineno))
        if isinstance(node, ast.AnnAssign):
            if node.value:
                record_aliases([node.target], node.value, int(node.lineno))
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.NamedExpr):
                record_aliases(
                    [node.func.target],
                    node.func.value,
                    int(getattr(node.func, "lineno", 0) or 0),
                )
            symbol = _forbidden_call_symbol(node.func, forbidden_aliases, builtins_aliases)
            if symbol in FORBIDDEN_CALLS:
                issues.append(ValidationIssue(
                    code="forbidden_call",
                    message=f"call {symbol} is not allowed in strategy code",
                    symbol=symbol,
                    line=int(getattr(node, "lineno", 0) or 0),
                ))
            elif symbol in forbidden_aliases:
                issues.append(ValidationIssue(
                    code="forbidden_call",
                    message=f"call alias {symbol} is not allowed in strategy code",
                    symbol=symbol,
                    line=int(getattr(node, "lineno", 0) or 0),
                ))
    if not has_my_strategy:
        issues.append(ValidationIssue("missing_my_strategy", "strategy must define class MyStrategy"))
    return ValidationResult(ok=not issues, issues=issues)
