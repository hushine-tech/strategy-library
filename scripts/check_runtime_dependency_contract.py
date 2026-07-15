from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib

from hushine_strategy.runtime_dependencies import (
    RuntimeDependencyProfile,
    _parse_manifest,
    _probe_environment,
    _run_installed_probe,
    load_runtime_dependency_profile,
)

BEGIN_MARKER = "# BEGIN GENERATED RUNTIME DEPENDENCY PROJECTION"
END_MARKER = "# END GENERATED RUNTIME DEPENDENCY PROJECTION"
MANIFEST_PATH = "hushine_strategy/runtime_dependencies.toml"
INITIAL_CONTRACT_SHA256 = (
    "8457b3c35618558fc8bfc74d4135b7eb52e00c33a8c9a49d202830f3fd5b62c5"
)
_REQUIREMENT_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


@dataclass(frozen=True)
class ContractViolation:
    code: str
    project: str
    distribution: str = ""
    message: str = ""


@dataclass(frozen=True)
class ContractNotice:
    code: str
    project: str
    distribution: str = ""
    message: str = ""


@dataclass(frozen=True)
class BaselineCheckResult:
    ref: str
    commit: str
    state: str
    violations: tuple[ContractViolation, ...]
    notices: tuple[ContractNotice, ...]


class ContractConfigurationError(ValueError):
    """Raised when the checker cannot evaluate its configured inputs."""


@dataclass(frozen=True)
class _ProjectionRegion:
    lines: tuple[str, ...]
    begin: int
    end: int


def _normalize_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _record_sort_key(record) -> tuple[str, str, str, str]:
    return (
        record.code,
        record.project,
        _normalize_distribution(record.distribution),
        record.message,
    )


def _sorted_unique(records):
    unique = {
        (item.code, item.project, item.distribution, item.message): item
        for item in records
    }
    return tuple(sorted(unique.values(), key=_record_sort_key))


def _violation(
    code: str,
    project: str,
    distribution: str = "",
    message: str = "",
) -> ContractViolation:
    return ContractViolation(code, project, distribution, message)


def _projection_region(
    pyproject_path: Path,
) -> tuple[_ProjectionRegion | None, tuple[ContractViolation, ...]]:
    try:
        text = pyproject_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return None, (
            _violation(
                "MALFORMED_PROJECT",
                str(pyproject_path),
                message=str(error),
            ),
        )
    lines = tuple(text.splitlines(keepends=True))
    begins = [index for index, line in enumerate(lines) if line.strip() == BEGIN_MARKER]
    ends = [index for index, line in enumerate(lines) if line.strip() == END_MARKER]
    if not begins and not ends:
        return None, (_violation("PROJECTION_MARKERS_MISSING", str(pyproject_path)),)
    if len(begins) > 1 or len(ends) > 1:
        return None, (_violation("PROJECTION_MARKERS_DUPLICATE", str(pyproject_path)),)
    if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
        return None, (_violation("PROJECTION_MARKERS_CORRUPT", str(pyproject_path)),)

    begin = begins[0]
    end = ends[0]
    dependency_start = next(
        (
            index
            for index in range(begin - 1, -1, -1)
            if re.match(r"^\s*dependencies\s*=\s*\[", lines[index])
        ),
        None,
    )
    if dependency_start is None:
        return None, (_violation("PROJECTION_MARKERS_CORRUPT", str(pyproject_path)),)
    dependency_end = next(
        (
            index
            for index in range(dependency_start + 1, len(lines))
            if lines[index].strip() == "]"
        ),
        None,
    )
    if (
        dependency_end is None
        or not dependency_start < begin < end < dependency_end
        or any(
            lines[index].strip().startswith("[")
            for index in range(dependency_start + 1, dependency_end)
        )
    ):
        return None, (_violation("PROJECTION_MARKERS_CORRUPT", str(pyproject_path)),)
    section_start = next(
        (
            index
            for index in range(dependency_start - 1, -1, -1)
            if lines[index].strip().startswith("[")
        ),
        None,
    )
    if section_start is None or lines[section_start].strip() != "[project]":
        return None, (_violation("PROJECTION_MARKERS_CORRUPT", str(pyproject_path)),)
    return _ProjectionRegion(lines, begin, end), ()


def _render_projection(
    profile: RuntimeDependencyProfile,
    region: _ProjectionRegion,
) -> tuple[str, ...]:
    marker_line = region.lines[region.begin]
    indent = marker_line[: len(marker_line) - len(marker_line.lstrip())]
    newline = "\r\n" if marker_line.endswith("\r\n") else "\n"
    return tuple(
        f'{indent}"{distribution}",{newline}'
        for distribution in profile.public_distributions
    )


def sync_project_projection(
    profile: RuntimeDependencyProfile,
    pyproject_path: Path,
    *,
    write: bool = False,
) -> tuple[ContractViolation, ...]:
    path = Path(pyproject_path)
    region, violations = _projection_region(path)
    if region is None:
        return violations
    expected = _render_projection(profile, region)
    current = region.lines[region.begin + 1 : region.end]
    if current == expected:
        return ()
    if not write:
        return (_violation("PROJECTION_NOT_GENERATED", str(path)),)
    updated = region.lines[: region.begin + 1] + expected + region.lines[region.end :]
    path.write_text("".join(updated), encoding="utf-8", newline="")
    reparsed, errors = _projection_region(path)
    if reparsed is None:
        return errors
    if reparsed.lines[reparsed.begin + 1 : reparsed.end] != expected:
        return (_violation("PROJECTION_WRITE_FAILED", str(path)),)
    return ()


def _requirement_name(requirement: object) -> str | None:
    if not isinstance(requirement, str):
        return None
    match = _REQUIREMENT_NAME.match(requirement)
    return _normalize_distribution(match.group(1)) if match else None


def _parse_project_dependencies(
    pyproject_path: Path,
) -> tuple[tuple[str, ...], tuple[ContractViolation, ...]]:
    try:
        value = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        dependencies = value["project"]["dependencies"]
        if not isinstance(dependencies, list):
            raise TypeError("project.dependencies must be an array")
        names = tuple(_requirement_name(item) for item in dependencies)
        if any(name is None for name in names):
            raise ValueError("project.dependencies contains an invalid requirement")
    except (
        OSError,
        UnicodeError,
        tomllib.TOMLDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        return (), (
            _violation(
                "MALFORMED_PROJECT",
                str(pyproject_path),
                message=str(error),
            ),
        )
    return tuple(name for name in names if name is not None), ()


def _generated_dependency_names(region: _ProjectionRegion) -> tuple[str, ...]:
    requirements: list[str] = []
    for line in region.lines[region.begin + 1 : region.end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith(","):
            stripped = stripped[:-1]
        if (
            len(stripped) >= 2
            and stripped[0] == stripped[-1]
            and stripped[0] in {'"', "'"}
        ):
            stripped = stripped[1:-1]
        name = _requirement_name(stripped)
        if name is not None:
            requirements.append(name)
    return tuple(requirements)


def _locked_distribution_names(
    project_name: str,
    lock_path: Path,
) -> tuple[tuple[str, ...], tuple[ContractViolation, ...]]:
    try:
        value = tomllib.loads(lock_path.read_text(encoding="utf-8"))
        packages = value.get("package", [])
        if not isinstance(packages, list):
            raise TypeError("top-level package must be an array")
        names = []
        for package in packages:
            if not isinstance(package, dict) or not isinstance(
                package.get("name"), str
            ):
                raise TypeError("every lock package must contain a name")
            names.append(_normalize_distribution(package["name"]))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, TypeError) as error:
        return (), (
            _violation(
                "MALFORMED_LOCK",
                project_name,
                message=str(error),
            ),
        )
    return tuple(names), ()


def _run_uv_lock_check(
    project_name: str,
    pyproject_path: Path,
) -> tuple[ContractViolation, ...]:
    try:
        completed = subprocess.run(
            [
                "uv",
                "lock",
                "--check",
                "--project",
                str(pyproject_path.parent),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return (_violation("STALE_LOCK", project_name, message=str(error)),)
    if completed.returncode == 0:
        return ()
    message = " ".join((completed.stderr or completed.stdout).split())
    return (_violation("STALE_LOCK", project_name, message=message),)


def check_project_projection(
    profile: RuntimeDependencyProfile,
    project_name: str,
    pyproject_path: Path,
    lock_path: Path,
) -> tuple[ContractViolation, ...]:
    project = Path(pyproject_path)
    lock = Path(lock_path)
    region, marker_violations = _projection_region(project)
    if region is None:
        return _sorted_unique(marker_violations)

    all_direct, project_violations = _parse_project_dependencies(project)
    if project_violations:
        return _sorted_unique(project_violations)
    generated = _generated_dependency_names(region)
    outside = list(all_direct)
    for name in generated:
        if name in outside:
            outside.remove(name)

    expected = tuple(
        _normalize_distribution(name) for name in profile.public_distributions
    )
    violations: list[ContractViolation] = []
    generated_counts = {name: generated.count(name) for name in set(generated)}
    for distribution in expected:
        if distribution not in all_direct:
            violations.append(
                _violation(
                    "MISSING_DIRECT_DISTRIBUTION",
                    project_name,
                    distribution,
                )
            )
        elif distribution in outside:
            violations.append(
                _violation(
                    "PUBLIC_DISTRIBUTION_OUTSIDE_PROJECTION",
                    project_name,
                    distribution,
                )
            )
        elif generated_counts.get(distribution, 0) > 1:
            violations.append(
                _violation(
                    "DUPLICATE_GENERATED_DISTRIBUTION",
                    project_name,
                    distribution,
                )
            )

    if not violations:
        violations.extend(sync_project_projection(profile, project))

    locked, lock_violations = _locked_distribution_names(project_name, lock)
    violations.extend(lock_violations)
    if not lock_violations:
        for distribution in expected:
            if distribution not in locked:
                violations.append(
                    _violation(
                        "DISTRIBUTION_NOT_LOCKED",
                        project_name,
                        distribution,
                    )
                )
    violations.extend(_run_uv_lock_check(project_name, project))
    return _sorted_unique(violations)


def _load_profile_bytes(value: bytes) -> RuntimeDependencyProfile:
    decoded = tomllib.loads(value.decode("utf-8"))
    return _parse_manifest(
        decoded,
        contract_sha256=hashlib.sha256(value).hexdigest(),
    )


def _semver_parts(value: str) -> tuple[int, int, int, tuple[str, ...] | None]:
    match = _SEMVER.fullmatch(value)
    if match is None:
        raise ValueError("profile_version must be strict SemVer")
    prerelease = tuple(match.group(4).split(".")) if match.group(4) else None
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), prerelease


def _compare_semver(left: str, right: str) -> int:
    left_major, left_minor, left_patch, left_pre = _semver_parts(left)
    right_major, right_minor, right_patch, right_pre = _semver_parts(right)
    left_core = (left_major, left_minor, left_patch)
    right_core = (right_major, right_minor, right_patch)
    if left_core != right_core:
        return 1 if left_core > right_core else -1
    if left_pre is None or right_pre is None:
        if left_pre is right_pre:
            return 0
        return 1 if left_pre is None else -1
    for left_item, right_item in zip(left_pre, right_pre):
        if left_item == right_item:
            continue
        left_numeric = left_item.isdigit()
        right_numeric = right_item.isdigit()
        if left_numeric and right_numeric:
            return 1 if int(left_item) > int(right_item) else -1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return 1 if left_item > right_item else -1
    if len(left_pre) == len(right_pre):
        return 0
    return 1 if len(left_pre) > len(right_pre) else -1


def check_profile_change(
    baseline_manifest: bytes,
    current_manifest: bytes,
) -> tuple[ContractViolation, ...]:
    try:
        baseline = _load_profile_bytes(baseline_manifest)
    except (UnicodeError, tomllib.TOMLDecodeError, ValueError) as error:
        return (_violation("INVALID_BASELINE_CONTRACT", "profile", message=str(error)),)
    try:
        current = _load_profile_bytes(current_manifest)
    except (UnicodeError, tomllib.TOMLDecodeError, ValueError) as error:
        return (_violation("INVALID_CURRENT_CONTRACT", "profile", message=str(error)),)
    if baseline_manifest == current_manifest:
        return ()
    if current.profile_version == baseline.profile_version:
        return (_violation("PROFILE_VERSION_NOT_BUMPED", "profile"),)
    if _compare_semver(current.profile_version, baseline.profile_version) <= 0:
        return (_violation("PROFILE_VERSION_NOT_GREATER", "profile"),)
    return ()


def _git(
    repository: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


def _git_bytes(
    repository: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        timeout=30,
    )


def check_baseline(
    repository: Path,
    baseline_ref: str,
    current_manifest: bytes,
) -> BaselineCheckResult:
    root_result = _git(Path(repository), "rev-parse", "--show-toplevel")
    if root_result.returncode != 0:
        raise ContractConfigurationError("cannot discover manifest repository root")
    root = Path(root_result.stdout.strip())
    resolve = _git(root, "rev-parse", "--verify", f"{baseline_ref}^{{commit}}")
    if resolve.returncode != 0:
        raise ContractConfigurationError(f"cannot resolve baseline ref: {baseline_ref}")
    commit = resolve.stdout.strip()
    tree = _git(
        root,
        "ls-tree",
        "--full-tree",
        "--name-only",
        commit,
        "--",
        MANIFEST_PATH,
    )
    if tree.returncode != 0:
        raise ContractConfigurationError("cannot inspect baseline manifest tree")
    tree_paths = tuple(tree.stdout.splitlines())
    if tree_paths not in ((), (MANIFEST_PATH,)):
        raise ContractConfigurationError("cannot inspect baseline manifest tree")

    if tree_paths:
        baseline = _git_bytes(root, "show", f"{commit}:{MANIFEST_PATH}")
        if baseline.returncode != 0:
            raise ContractConfigurationError("cannot read baseline manifest")
        violations = check_profile_change(baseline.stdout, current_manifest)
        return BaselineCheckResult(
            ref=baseline_ref,
            commit=commit,
            state="present",
            violations=violations,
            notices=(),
        )

    notices = (
        ContractNotice(
            "BASELINE_MANIFEST_ABSENT",
            "profile",
            message="baseline commit predates the runtime dependency contract",
        ),
    )
    digest = hashlib.sha256(current_manifest).hexdigest()
    violations: tuple[ContractViolation, ...] = ()
    try:
        _load_profile_bytes(current_manifest)
    except (UnicodeError, tomllib.TOMLDecodeError, ValueError):
        digest = "invalid"
    if digest != INITIAL_CONTRACT_SHA256:
        violations = (_violation("INVALID_INITIAL_CONTRACT", "profile"),)
    return BaselineCheckResult(
        ref=baseline_ref,
        commit=commit,
        state="introduced",
        violations=violations,
        notices=notices,
    )


def _version_matches(actual: str, expected: str) -> bool:
    try:
        parts = tuple(int(item) for item in actual.split("."))
    except ValueError:
        return False
    if len(parts) < 2:
        return False
    if expected == "3.13":
        return parts[:2] == (3, 13)
    if expected == ">=3.12":
        return parts[:2] >= (3, 12)
    return False


def check_installed_projection(
    profile: RuntimeDependencyProfile,
    python_executable: str,
    expected_python: str,
) -> tuple[ContractViolation, ...]:
    try:
        result = _run_installed_probe(
            python_executable,
            expected_python,
            _probe_environment(),
        )
    except (OSError, RuntimeError, ValueError):
        return (
            _violation(
                "TARGET_PROBE_FAILED",
                "installed-runtime",
                message="target dependency probe failed",
            ),
        )
    required = {
        "schema_version",
        "profile_name",
        "profile_version",
        "hosted_python",
        "debugger_python",
        "contract_sha256",
        "python_version",
        "failures",
    }
    if not isinstance(result, dict) or not required.issubset(result):
        return (_violation("TARGET_METADATA_MISSING", "installed-runtime"),)

    violations: list[ContractViolation] = []
    caller_metadata = {
        "schema_version": profile.schema_version,
        "profile_name": profile.profile_name,
        "profile_version": profile.profile_version,
        "hosted_python": profile.hosted_python,
        "debugger_python": profile.debugger_python,
        "contract_sha256": profile.contract_sha256,
    }
    if any(result.get(key) != value for key, value in caller_metadata.items()):
        violations.append(
            _violation("CALLER_TARGET_PROFILE_MISMATCH", "installed-runtime")
        )
    actual_python = result.get("python_version")
    if not isinstance(actual_python, str) or not _version_matches(
        actual_python, expected_python
    ):
        violations.append(_violation("PYTHON_VERSION_MISMATCH", "installed-runtime"))

    failures = result.get("failures")
    if not isinstance(failures, list):
        violations.append(_violation("TARGET_METADATA_MISSING", "installed-runtime"))
        return _sorted_unique(violations)
    for failure in failures:
        if not isinstance(failure, dict):
            violations.append(
                _violation("TARGET_METADATA_MISSING", "installed-runtime")
            )
            continue
        distribution = failure.get("distribution", "")
        reason = failure.get("reason", "")
        probe = failure.get("probe", "")
        if probe == "sys.version_info":
            violations.append(
                _violation("PYTHON_VERSION_MISMATCH", "installed-runtime")
            )
        elif isinstance(reason, str) and "PackageNotFoundError" in reason:
            violations.append(
                _violation(
                    "INSTALLED_METADATA_MISSING",
                    "installed-runtime",
                    str(distribution),
                    reason,
                )
            )
        else:
            violations.append(
                _violation(
                    "INSTALLED_PROBE_FAILED",
                    "installed-runtime",
                    str(distribution),
                    str(reason),
                )
            )
    return _sorted_unique(violations)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="check_runtime_dependency_contract.py")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write-projections", action="store_true")
    mode.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--service-project", type=Path)
    parser.add_argument("--service-lock", type=Path)
    parser.add_argument("--debugger-project", type=Path)
    parser.add_argument("--debugger-lock", type=Path)
    parser.add_argument("--baseline-ref")
    parser.add_argument("--installed-python", action="append", default=[])
    parser.add_argument("--installed-python-version", action="append", default=[])
    parser.add_argument("--json", action="store_true", required=True)
    return parser


def _record_json(record) -> dict[str, str]:
    return {
        "code": record.code,
        "distribution": record.distribution,
        "message": record.message,
        "project": record.project,
    }


def _base_payload(profile: RuntimeDependencyProfile) -> dict[str, object]:
    return {
        "baseline": {"commit": "", "ref": "", "state": "not_checked"},
        "checked_interpreters": [],
        "checked_projects": [],
        "digest": profile.contract_sha256,
        "notices": [],
        "ok": True,
        "profile": {
            "debugger_python": profile.debugger_python,
            "hosted_python": profile.hosted_python,
            "name": profile.profile_name,
            "schema_version": profile.schema_version,
            "version": profile.profile_version,
        },
        "violations": [],
    }


def _emit_payload(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _configuration_failure(payload: dict[str, object], message: str) -> int:
    payload["ok"] = False
    payload["error"] = {
        "code": "CONFIGURATION_ERROR",
        "message": " ".join(message.split()),
    }
    _emit_payload(payload)
    return 2


def _parse_assignments(values: list[str], option: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ContractConfigurationError(f"{option} must use NAME=VALUE")
        name, item = value.split("=", 1)
        if not name or not item:
            raise ContractConfigurationError(f"{option} must use non-empty NAME=VALUE")
        if name in parsed:
            raise ContractConfigurationError(
                f"{option} contains duplicate name: {name}"
            )
        parsed[name] = item
    return parsed


def _configured_projects(options) -> list[tuple[str, Path, Path]]:
    projects: list[tuple[str, Path, Path]] = []
    for name in ("service", "debugger"):
        project = getattr(options, f"{name}_project")
        lock = getattr(options, f"{name}_lock")
        if (project is None) != (lock is None):
            raise ContractConfigurationError(
                f"--{name}-project and --{name}-lock must be supplied together"
            )
        if project is not None:
            projects.append((name, project, lock))
    return projects


def _configured_interpreters(
    options,
    profile: RuntimeDependencyProfile,
) -> list[tuple[str, str, str]]:
    interpreters = _parse_assignments(options.installed_python, "--installed-python")
    versions = _parse_assignments(
        options.installed_python_version, "--installed-python-version"
    )
    if set(interpreters) != set(versions):
        raise ContractConfigurationError(
            "every --installed-python NAME requires exactly one matching "
            "--installed-python-version NAME"
        )
    expected_by_name = {
        "service": profile.hosted_python,
        "debugger": profile.debugger_python,
    }
    allowed = {profile.hosted_python, profile.debugger_python}
    configured: list[tuple[str, str, str]] = []
    for name in sorted(interpreters):
        constraint = versions[name]
        if constraint not in allowed:
            raise ContractConfigurationError(
                f"unsupported Python constraint for {name}: {constraint}"
            )
        named_constraint = expected_by_name.get(name)
        if named_constraint is not None and constraint != named_constraint:
            raise ContractConfigurationError(
                f"{name} requires Python constraint {named_constraint}"
            )
        executable = os.path.abspath(os.path.normpath(interpreters[name]))
        configured.append((name, executable, constraint))
    return configured


def main(argv: list[str] | None = None) -> int:
    options = _parser().parse_args(argv)
    try:
        profile = load_runtime_dependency_profile()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError) as error:
        payload = {
            "ok": False,
            "error": {
                "code": "CONFIGURATION_ERROR",
                "message": " ".join(str(error).split()),
            },
        }
        _emit_payload(payload)
        return 2

    payload = _base_payload(profile)
    try:
        projects = _configured_projects(options)
        interpreters = _configured_interpreters(options, profile)
        has_product_options = bool(projects or interpreters)
        if options.baseline_only:
            if not options.baseline_ref:
                raise ContractConfigurationError(
                    "--baseline-only requires --baseline-ref"
                )
            if has_product_options:
                raise ContractConfigurationError(
                    "--baseline-only does not accept product or interpreter paths"
                )
        elif options.write_projections:
            if not projects:
                raise ContractConfigurationError(
                    "--write-projections requires at least one product project"
                )
            if interpreters or options.baseline_ref:
                raise ContractConfigurationError(
                    "--write-projections accepts only product project/lock pairs"
                )
        elif not has_product_options and not options.baseline_ref:
            raise ContractConfigurationError(
                "configure at least one project, interpreter, or baseline ref"
            )
    except ContractConfigurationError as error:
        return _configuration_failure(payload, str(error))

    current_manifest_path = Path(__file__).resolve().parents[1] / MANIFEST_PATH
    try:
        current_manifest = current_manifest_path.read_bytes()
    except OSError as error:
        return _configuration_failure(payload, str(error))

    violations: list[ContractViolation] = []
    notices: list[ContractNotice] = []
    if options.baseline_ref:
        try:
            baseline = check_baseline(
                current_manifest_path.parent.parent,
                options.baseline_ref,
                current_manifest,
            )
        except ContractConfigurationError as error:
            return _configuration_failure(payload, str(error))
        payload["baseline"] = {
            "commit": baseline.commit,
            "ref": baseline.ref,
            "state": baseline.state,
        }
        violations.extend(baseline.violations)
        notices.extend(baseline.notices)

    checked_projects: list[str] = []
    if options.write_projections:
        for name, project, _lock in projects:
            checked_projects.append(name)
            violations.extend(sync_project_projection(profile, project, write=True))
            if not violations:
                violations.extend(sync_project_projection(profile, project))
    elif not options.baseline_only:
        for name, project, lock in projects:
            checked_projects.append(name)
            violations.extend(check_project_projection(profile, name, project, lock))
        for name, executable, constraint in interpreters:
            violations.extend(
                check_installed_projection(profile, executable, constraint)
            )

    payload["checked_projects"] = sorted(checked_projects)
    payload["checked_interpreters"] = [
        {
            "expected_python": constraint,
            "name": name,
            "path": executable,
        }
        for name, executable, constraint in interpreters
        if not options.baseline_only and not options.write_projections
    ]
    sorted_notices = _sorted_unique(notices)
    sorted_violations = _sorted_unique(violations)
    payload["notices"] = [_record_json(item) for item in sorted_notices]
    payload["violations"] = [_record_json(item) for item in sorted_violations]
    payload["ok"] = not sorted_violations
    _emit_payload(payload)
    return 0 if not sorted_violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
