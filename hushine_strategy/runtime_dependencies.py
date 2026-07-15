from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
import hashlib
import importlib
import io
import json
from importlib import resources
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib
import warnings


_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "profile_name",
        "profile_version",
        "hosted_python",
        "debugger_python",
        "dependencies",
    }
)
_DEPENDENCY_FIELDS = frozenset(
    {"import_root", "distribution", "probe", "public"}
)
_SEMVER_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
    r"(?:-(?:"
    r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*"
    r"))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_PATH_PATTERN = re.compile(
    r"(?:[A-Za-z]:[\\/]|/)[^\s'\"\]\[(){};,]+"
)


@dataclass(frozen=True)
class RuntimeDependency:
    import_root: str
    distribution: str
    probe: str
    public: bool


@dataclass(frozen=True)
class RuntimeDependencyProfile:
    schema_version: int
    profile_name: str
    profile_version: str
    hosted_python: str
    debugger_python: str
    dependencies: tuple[RuntimeDependency, ...]
    contract_sha256: str

    @property
    def public_import_roots(self) -> tuple[str, ...]:
        return tuple(
            sorted(item.import_root for item in self.dependencies if item.public)
        )

    @property
    def public_distributions(self) -> tuple[str, ...]:
        return tuple(
            sorted(item.distribution for item in self.dependencies if item.public)
        )


@dataclass(frozen=True)
class DependencyProbeFailure:
    import_root: str
    distribution: str
    probe: str
    reason: str


def _require_exact_fields(
    value: object,
    expected: frozenset[str],
    *,
    location: str,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{location} must be a table")
    actual = frozenset(value)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing:
        raise ValueError(f"{location} is missing required field: {missing[0]}")
    if unexpected:
        raise ValueError(f"{location} has unsupported field: {unexpected[0]}")
    return value


def _require_nonempty_string(value: object, *, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location} must be a non-empty string")
    return value


def _normalize_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _failure_sort_key(
    failure: DependencyProbeFailure,
) -> tuple[str, str, str, str]:
    return (
        failure.import_root,
        _normalize_distribution(failure.distribution),
        failure.probe,
        failure.reason,
    )


def _parse_manifest(
    manifest: object,
    *,
    contract_sha256: str,
) -> RuntimeDependencyProfile:
    table = _require_exact_fields(
        manifest,
        _TOP_LEVEL_FIELDS,
        location="runtime dependency profile",
    )

    schema_version = table["schema_version"]
    if type(schema_version) is not int:
        raise ValueError("schema_version must be an integer")
    if schema_version != 1:
        raise ValueError(f"unsupported schema_version: {schema_version}")

    profile_name = _require_nonempty_string(
        table["profile_name"], location="profile_name"
    )
    if profile_name != "platform-python-3.13":
        raise ValueError("schema 1 profile_name must be platform-python-3.13")

    profile_version = _require_nonempty_string(
        table["profile_version"], location="profile_version"
    )
    if _SEMVER_PATTERN.fullmatch(profile_version) is None:
        raise ValueError("profile_version must be a strict SemVer value")

    hosted_python = _require_nonempty_string(
        table["hosted_python"], location="hosted_python"
    )
    debugger_python = _require_nonempty_string(
        table["debugger_python"], location="debugger_python"
    )
    if hosted_python != "3.13":
        raise ValueError('schema 1 hosted_python must be "3.13"')
    if debugger_python != ">=3.12":
        raise ValueError('schema 1 debugger_python must be ">=3.12"')

    raw_dependencies = table["dependencies"]
    if not isinstance(raw_dependencies, list):
        raise ValueError("dependencies must be an array of tables")

    dependencies: list[RuntimeDependency] = []
    seen_import_roots: set[str] = set()
    seen_distributions: set[str] = set()
    seen_probes: set[str] = set()
    for index, raw_dependency in enumerate(raw_dependencies):
        location = f"dependencies[{index}]"
        dependency = _require_exact_fields(
            raw_dependency,
            _DEPENDENCY_FIELDS,
            location=location,
        )
        import_root = _require_nonempty_string(
            dependency["import_root"], location=f"{location}.import_root"
        )
        distribution = _require_nonempty_string(
            dependency["distribution"], location=f"{location}.distribution"
        )
        probe = _require_nonempty_string(
            dependency["probe"], location=f"{location}.probe"
        )
        public = dependency["public"]
        if type(public) is not bool:
            raise ValueError(f"{location}.public must be a boolean")
        if probe.split(".", 1)[0] != import_root:
            raise ValueError(f"{location}.probe must start with import_root")

        normalized_distribution = _normalize_distribution(distribution)
        if probe in seen_probes:
            raise ValueError(f"duplicate probe: {probe}")
        if import_root in seen_import_roots:
            raise ValueError(f"duplicate import_root: {import_root}")
        if normalized_distribution in seen_distributions:
            raise ValueError(f"duplicate distribution: {distribution}")

        seen_probes.add(probe)
        seen_import_roots.add(import_root)
        seen_distributions.add(normalized_distribution)
        dependencies.append(
            RuntimeDependency(
                import_root=import_root,
                distribution=distribution,
                probe=probe,
                public=public,
            )
        )

    if not any(item.public for item in dependencies):
        raise ValueError("runtime dependency profile must have a public dependency")

    return RuntimeDependencyProfile(
        schema_version=schema_version,
        profile_name=profile_name,
        profile_version=profile_version,
        hosted_python=hosted_python,
        debugger_python=debugger_python,
        dependencies=tuple(dependencies),
        contract_sha256=contract_sha256,
    )


def load_runtime_dependency_profile(
    path: str | Path | None = None,
) -> RuntimeDependencyProfile:
    if path is None:
        manifest_bytes = (
            resources.files("hushine_strategy")
            .joinpath("runtime_dependencies.toml")
            .read_bytes()
        )
    else:
        manifest_bytes = Path(path).read_bytes()

    return _parse_manifest(
        tomllib.loads(manifest_bytes.decode("utf-8")),
        contract_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )


def probe_runtime_dependency_profile(
    profile: RuntimeDependencyProfile | None = None,
    *,
    python_executable: str = sys.executable,
    python_constraint: str | None = None,
) -> tuple[DependencyProbeFailure, ...]:
    selected_profile = profile or load_runtime_dependency_profile()
    supported_constraints = {
        selected_profile.hosted_python,
        selected_profile.debugger_python,
    }
    if (
        python_constraint is not None
        and python_constraint not in supported_constraints
    ):
        raise ValueError(
            "python_constraint must match hosted_python or debugger_python"
        )

    result = _run_installed_probe(
        python_executable,
        python_constraint,
        _probe_environment(),
    )
    if not isinstance(result, dict):
        raise ValueError("target dependency probe returned an invalid result")
    if result.get("contract_sha256") != selected_profile.contract_sha256:
        raise ValueError("target dependency profile digest does not match caller")

    raw_failures = result.get("failures")
    if not isinstance(raw_failures, list):
        raise ValueError("target dependency probe returned invalid failures")
    failures: list[DependencyProbeFailure] = []
    expected_fields = frozenset(
        {"import_root", "distribution", "probe", "reason"}
    )
    for index, raw_failure in enumerate(raw_failures):
        failure = _require_exact_fields(
            raw_failure,
            expected_fields,
            location=f"probe failures[{index}]",
        )
        failures.append(
            DependencyProbeFailure(
                import_root=_require_nonempty_string(
                    failure["import_root"],
                    location=f"probe failures[{index}].import_root",
                ),
                distribution=_require_nonempty_string(
                    failure["distribution"],
                    location=f"probe failures[{index}].distribution",
                ),
                probe=_require_nonempty_string(
                    failure["probe"],
                    location=f"probe failures[{index}].probe",
                ),
                reason=_require_nonempty_string(
                    failure["reason"],
                    location=f"probe failures[{index}].reason",
                ),
            )
        )
    return tuple(
        sorted(failures, key=_failure_sort_key)
    )


def require_runtime_dependency_profile(
    profile: RuntimeDependencyProfile | None = None,
    *,
    python_executable: str = sys.executable,
    python_constraint: str | None = None,
) -> RuntimeDependencyProfile:
    selected_profile = profile or load_runtime_dependency_profile()
    failures = probe_runtime_dependency_profile(
        selected_profile,
        python_executable=python_executable,
        python_constraint=python_constraint,
    )
    if failures:
        details = "; ".join(
            f"{failure.import_root} ({failure.distribution}, {failure.probe}): "
            f"{failure.reason}"
            for failure in failures
        )
        raise RuntimeError(
            f"runtime dependency profile verification failed: {details}"
        )
    return selected_profile


def _run_installed_probe(
    executable: str,
    constraint: str | None,
    env: dict[str, str],
) -> dict[str, object]:
    command = [
        executable,
        "-I",
        "-m",
        "hushine_strategy.runtime_dependencies",
        "_probe-installed",
    ]
    if constraint is not None:
        command.extend(["--python-constraint", constraint])
    command.append("--json")
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=30,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("target dependency probe timed out") from error
    if completed.returncode not in {0, 1}:
        raise RuntimeError(
            "target dependency probe exited with status "
            f"{completed.returncode}"
        )
    try:
        result = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as error:
        raise RuntimeError("target dependency probe returned invalid JSON") from error
    if not isinstance(result, dict):
        raise RuntimeError("target dependency probe returned invalid JSON")
    return result


def _probe_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    return environment


def _current_python_version() -> tuple[int, int, int]:
    return (
        sys.version_info.major,
        sys.version_info.minor,
        sys.version_info.micro,
    )


def _python_constraint_matches(
    version: tuple[int, int, int],
    constraint: str | None,
) -> bool:
    if constraint is None:
        return True
    if constraint == "3.13":
        return version[:2] == (3, 13)
    if constraint == ">=3.12":
        return version[:2] >= (3, 12)
    raise ValueError("unsupported python_constraint")


def _safe_exception_reason(error: BaseException) -> str:
    message = str(error)
    redactions = set(sys.path)
    redactions.add(os.getcwd())
    redactions.update(
        value for value in os.environ.values() if len(value) >= 4
    )
    for value in sorted(redactions, key=lambda value: (-len(value), value)):
        if value:
            message = message.replace(value, "<redacted>")
    message = _PATH_PATTERN.sub("<path>", message)
    message = " ".join(message.split())
    if len(message) > 500:
        message = f"{message[:497]}..."
    exception_name = type(error).__name__
    return f"{exception_name}: {message}" if message else exception_name


def _profile_json(profile: RuntimeDependencyProfile) -> dict[str, object]:
    dependencies = sorted(
        profile.dependencies,
        key=lambda item: (item.import_root, item.distribution, item.probe),
    )
    return {
        "schema_version": profile.schema_version,
        "profile_name": profile.profile_name,
        "profile_version": profile.profile_version,
        "hosted_python": profile.hosted_python,
        "debugger_python": profile.debugger_python,
        "contract_sha256": profile.contract_sha256,
        "public_import_roots": list(profile.public_import_roots),
        "public_distributions": list(profile.public_distributions),
        "dependencies": [
            {
                "import_root": item.import_root,
                "distribution": item.distribution,
                "probe": item.probe,
                "public": item.public,
            }
            for item in dependencies
        ],
    }


def _failure_json(failure: DependencyProbeFailure) -> dict[str, str]:
    return {
        "import_root": failure.import_root,
        "distribution": failure.distribution,
        "probe": failure.probe,
        "reason": failure.reason,
    }


def _installed_probe_result(
    profile: RuntimeDependencyProfile,
    python_constraint: str | None,
) -> dict[str, object]:
    if python_constraint not in {
        None,
        profile.hosted_python,
        profile.debugger_python,
    }:
        raise ValueError("unsupported python_constraint")

    from importlib import metadata

    version = _current_python_version()
    failures: list[DependencyProbeFailure] = []
    if not _python_constraint_matches(version, python_constraint):
        actual = ".".join(str(part) for part in version)
        failures.append(
            DependencyProbeFailure(
                import_root="sys",
                distribution="CPython",
                probe="sys.version_info",
                reason=(
                    "PythonVersionMismatch: expected "
                    f"{python_constraint}, got {actual}"
                ),
            )
        )

    for dependency in profile.dependencies:
        try:
            metadata.version(dependency.distribution)
        except Exception as error:
            failures.append(
                DependencyProbeFailure(
                    import_root=dependency.import_root,
                    distribution=dependency.distribution,
                    probe=dependency.probe,
                    reason=_safe_exception_reason(error),
                )
            )

        try:
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
                warnings.catch_warnings(),
            ):
                warnings.simplefilter("ignore")
                importlib.import_module(dependency.probe)
        except (Exception, SystemExit) as error:
            failures.append(
                DependencyProbeFailure(
                    import_root=dependency.import_root,
                    distribution=dependency.distribution,
                    probe=dependency.probe,
                    reason=_safe_exception_reason(error),
                )
            )

    failures.sort(key=_failure_sort_key)
    result = _profile_json(profile)
    result.update(
        {
            "python_version": ".".join(str(part) for part in version),
            "ok": not failures,
            "failures": [_failure_json(failure) for failure in failures],
        }
    )
    return result


def _emit_json(value: dict[str, object]) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _private_probe_main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="runtime_dependencies _probe-installed")
    parser.add_argument(
        "--python-constraint",
        choices=("3.13", ">=3.12"),
    )
    parser.add_argument("--json", action="store_true", required=True)
    options = parser.parse_args(arguments)
    result = _installed_probe_result(
        load_runtime_dependency_profile(),
        options.python_constraint,
    )
    _emit_json(result)
    return 0 if result["ok"] else 1


def _public_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hushine_strategy.runtime_dependencies"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    show = commands.add_parser("show", help="show the packaged profile")
    show.add_argument("--json", action="store_true", required=True)

    verify = commands.add_parser(
        "verify-installed",
        help="verify installed metadata and imports",
    )
    verify.add_argument(
        "--python-constraint",
        choices=("3.13", ">=3.12"),
        required=True,
    )
    verify.add_argument("--json", action="store_true", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "_probe-installed":
        return _private_probe_main(arguments[1:])

    options = _public_parser().parse_args(arguments)
    profile = load_runtime_dependency_profile()
    if options.command == "show":
        _emit_json(_profile_json(profile))
        return 0

    result = _installed_probe_result(profile, options.python_constraint)
    _emit_json(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
