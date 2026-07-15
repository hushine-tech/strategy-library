from __future__ import annotations

import ast
from dataclasses import dataclass

from hushine_strategy.import_validation import (
    DEBUGGER_PLATFORM_IMPORT_POLICY,
    HOSTED_DYNAMIC_IMPORT_CALLS,
    HOSTED_DYNAMIC_IMPORT_ROOTS,
    validate_dependency_imports,
    validate_dynamic_import_safety,
    validate_platform_import_safety,
)
from hushine_strategy.runtime_dependencies import load_runtime_dependency_profile


_LIMITED_STDLIB_IMPORT_ROOTS: frozenset[str] = frozenset(
    {
        "__future__",
        "bisect",
        "collections",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "functools",
        "heapq",
        "itertools",
        "math",
        "random",
        "statistics",
        "typing",
    }
)
_PROFILE = load_runtime_dependency_profile()
_SDK_PLATFORM_MODULES = frozenset(
    module
    for module, _ in DEBUGGER_PLATFORM_IMPORT_POLICY.allowed_from_symbols
)

# Compatibility export used by replay's runtime import guard. Public third-party
# roots are projected from the packaged dependency contract, never copied here.
ALLOWED_IMPORT_ROOTS: frozenset[str] = frozenset(
    _LIMITED_STDLIB_IMPORT_ROOTS
    | {"hushine_strategy"}
    | set(_PROFILE.public_import_roots)
)

FORBIDDEN_IMPORT_ROOTS: frozenset[str] = frozenset(
    HOSTED_DYNAMIC_IMPORT_ROOTS
    | {
        "aiohttp",
        "asyncio",
        "ctypes",
        "httpx",
        "multiprocessing",
        "os",
        "pathlib",
        "shutil",
        "socket",
        "subprocess",
        "sys",
        "threading",
        "urllib",
        "websocket",
    }
)

FORBIDDEN_CALLS: frozenset[str] = frozenset(
    HOSTED_DYNAMIC_IMPORT_CALLS
    | {
        "delattr",
        "dir",
        "input",
        "open",
        "setattr",
    }
)


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


def _relative_import_issues(tree: ast.AST) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level <= 0:
            continue
        module = f"{'.' * node.level}{node.module or ''}"
        issues.append(
            ValidationIssue(
                code="forbidden_import",
                message=(
                    f"from {module} import is not allowed in base-futures-v1"
                ),
                module=module,
                line=int(node.lineno),
            )
        )
    return issues


def _static_import_issues(tree: ast.AST) -> list[ValidationIssue]:
    shared_safety = (
        validate_platform_import_safety(
            tree,
            policy=DEBUGGER_PLATFORM_IMPORT_POLICY,
        )
        + validate_dynamic_import_safety(
            tree,
            forbidden_import_roots=FORBIDDEN_IMPORT_ROOTS,
            forbidden_calls=FORBIDDEN_CALLS,
        )
    )
    safety_by_key = {}
    for issue in shared_safety:
        safety_by_key.setdefault(
            (issue.line, issue.module, issue.symbol, issue.code),
            issue,
        )
    safety_issues = tuple(
        sorted(
            safety_by_key.values(),
            key=lambda issue: (
                issue.line,
                issue.module,
                issue.symbol,
                issue.code,
            ),
        )
    )
    rejected_imports = {
        (issue.line, issue.module)
        for issue in safety_issues
        if issue.module
    }

    issues = [
        ValidationIssue(
            code=issue.code,
            message=issue.message,
            module=issue.module,
            symbol=issue.symbol,
            line=issue.line,
        )
        for issue in safety_issues
    ]
    for issue in validate_dependency_imports(
        tree,
        profile=_PROFILE,
        stdlib_roots=_LIMITED_STDLIB_IMPORT_ROOTS,
        platform_modules=_SDK_PLATFORM_MODULES,
    ):
        if (issue.line, issue.module) in rejected_imports:
            continue
        issues.append(
            ValidationIssue(
                code=issue.code,
                message=issue.message,
                module=issue.module,
                line=issue.line,
            )
        )
    return sorted(
        issues,
        key=lambda issue: (
            issue.line,
            issue.module,
            issue.symbol,
            issue.code,
            issue.message,
        ),
    )


def validate_strategy_code(code: str) -> ValidationResult:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return ValidationResult(
            ok=False,
            issues=[
                ValidationIssue(
                    "syntax_error",
                    str(exc),
                    line=int(exc.lineno or 0),
                )
            ],
        )

    collected_issues = _relative_import_issues(tree)
    collected_issues.extend(_static_import_issues(tree))
    unique_issues: dict[tuple[int, str, str, str], ValidationIssue] = {}
    for issue in collected_issues:
        unique_issues.setdefault(
            (issue.line, issue.module, issue.symbol, issue.code),
            issue,
        )
    issues = sorted(
        unique_issues.values(),
        key=lambda issue: (
            issue.line,
            issue.module,
            issue.symbol,
            issue.code,
            issue.message,
        ),
    )

    has_my_strategy = any(
        isinstance(node, ast.ClassDef) and node.name == "MyStrategy"
        for node in tree.body
    )
    if not has_my_strategy:
        issues.append(
            ValidationIssue(
                "missing_my_strategy",
                "strategy must define class MyStrategy",
            )
        )
    return ValidationResult(ok=not issues, issues=issues)
