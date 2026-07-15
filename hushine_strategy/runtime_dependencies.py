from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
import hashlib
import importlib
import json
from importlib import resources
import os
from pathlib import Path
import re
import sys
import tomllib
from typing import Mapping
import warnings

import hushine_runtime_import_probe.transport as probe_transport

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
_DEPENDENCY_FIELDS = frozenset({"import_root", "distribution", "probe", "public"})
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
    r"(?:"
    r"(?:\\\\\?\\)?[A-Za-z]:[\\/]"
    r"|\\\\[^\\/\s]+[\\/][^\\/\s]+[\\/]"
    r"|/"
    r")[^\r\n'\"\]\[(){};,]*"
)

PROFILE_PROBE_ENV_KEYS = probe_transport.PROFILE_PROBE_ENV_KEYS
_PROBE_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "profile_name",
        "profile_version",
        "hosted_python",
        "debugger_python",
        "contract_sha256",
        "public_import_roots",
        "public_distributions",
        "dependencies",
        "python_version",
        "ok",
        "failures",
    }
)
_PROBE_DEPENDENCY_FIELDS = frozenset({"import_root", "distribution", "probe", "public"})
_PROBE_FAILURE_FIELDS = frozenset({"import_root", "distribution", "probe", "reason"})


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
    if python_constraint is not None and python_constraint not in supported_constraints:
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
    expected_fields = frozenset({"import_root", "distribution", "probe", "reason"})
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
    return tuple(sorted(failures, key=_failure_sort_key))


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
        raise RuntimeError(f"runtime dependency profile verification failed: {details}")
    return selected_profile


def _run_installed_probe(
    executable: str,
    constraint: str | None,
    env: dict[str, str],
) -> dict[str, object]:
    if constraint not in {None, "3.13", ">=3.12"}:
        raise ValueError("invalid target Python constraint")

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
    if not probe_transport.valid_probe_argv(command):
        raise ValueError("invalid target Python invocation")

    try:
        completed = probe_transport.run_probe(
            command,
            environment_policy=probe_transport.PROFILE_PROBE_POLICY,
            source_environment=env,
            stdin_bytes=None,
            timeout_seconds=probe_transport.PROFILE_PROBE_TIMEOUT_SECONDS,
            thread_prefix="runtime-profile-probe-",
        )
    except probe_transport.ProbeTransportError as error:
        messages = {
            "launch": "target dependency probe could not be started",
            "timeout": "target dependency probe timed out",
            "overflow": "target dependency probe output limit exceeded",
        }
        raise RuntimeError(
            messages.get(
                error.kind,
                "target dependency probe returned an invalid response",
            )
        ) from None

    result = _parse_probe_response(
        completed.stdout,
        completed.stderr,
        completed.returncode,
    )
    if result is None:
        raise RuntimeError(
            "target dependency probe returned an invalid response"
        ) from None
    return result


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_json_constant(_value: str):
    raise ValueError("invalid JSON constant")


def _parse_probe_response(
    stdout: bytes, stderr: bytes, returncode: int
) -> dict[str, object] | None:
    if stderr or returncode not in {0, 1}:
        return None
    try:
        text = stdout.decode("utf-8", "strict")
        result = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
        canonical = (
            json.dumps(
                result,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except Exception:
        return None
    try:
        valid_payload = _valid_probe_payload(result, returncode)
    except Exception:
        return None
    if stdout != canonical or not valid_payload:
        return None
    return result


def _valid_probe_payload(value: object, returncode: int) -> bool:
    if not isinstance(value, dict) or frozenset(value) != _PROBE_TOP_LEVEL_FIELDS:
        return False
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        return False
    for key in (
        "profile_name",
        "profile_version",
        "hosted_python",
        "debugger_python",
        "contract_sha256",
        "python_version",
    ):
        if not _valid_probe_text(value[key]):
            return False
    if value["profile_name"] != "platform-python-3.13":
        return False
    if _SEMVER_PATTERN.fullmatch(value["profile_version"]) is None:
        return False
    if value["hosted_python"] != "3.13" or value["debugger_python"] != ">=3.12":
        return False
    if re.fullmatch(r"[0-9a-f]{64}", value["contract_sha256"]) is None:
        return False
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value["python_version"]) is None:
        return False

    dependencies = value["dependencies"]
    if not isinstance(dependencies, list) or not dependencies:
        return False
    dependency_keys: list[tuple[str, str, str]] = []
    import_roots: list[str] = []
    distributions: list[str] = []
    probes: list[str] = []
    public_roots: list[str] = []
    public_distributions: list[str] = []
    for dependency in dependencies:
        if (
            not isinstance(dependency, dict)
            or frozenset(dependency) != _PROBE_DEPENDENCY_FIELDS
        ):
            return False
        for key in ("import_root", "distribution", "probe"):
            if not _valid_probe_text(dependency[key]):
                return False
        if type(dependency["public"]) is not bool:
            return False
        if dependency["probe"].split(".", 1)[0] != dependency["import_root"]:
            return False
        item_key = (
            dependency["import_root"],
            dependency["distribution"],
            dependency["probe"],
        )
        dependency_keys.append(item_key)
        import_roots.append(dependency["import_root"])
        distributions.append(_normalize_distribution(dependency["distribution"]))
        probes.append(dependency["probe"])
        if dependency["public"]:
            public_roots.append(dependency["import_root"])
            public_distributions.append(dependency["distribution"])
    if dependency_keys != sorted(dependency_keys):
        return False
    if any(
        len(items) != len(set(items)) for items in (import_roots, distributions, probes)
    ):
        return False
    if not isinstance(value["public_import_roots"], list) or not all(
        _valid_probe_text(item) for item in value["public_import_roots"]
    ):
        return False
    if not isinstance(value["public_distributions"], list) or not all(
        _valid_probe_text(item) for item in value["public_distributions"]
    ):
        return False
    if value["public_import_roots"] != sorted(public_roots):
        return False
    if value["public_distributions"] != sorted(public_distributions):
        return False
    if not public_roots:
        return False

    failures = value["failures"]
    if not isinstance(failures, list):
        return False
    failure_keys: list[tuple[str, str, str, str]] = []
    allowed_failure_targets = {
        (dependency["import_root"], dependency["distribution"], dependency["probe"])
        for dependency in dependencies
    }
    allowed_failure_targets.add(("sys", "CPython", "sys.version_info"))
    for failure in failures:
        if not isinstance(failure, dict) or frozenset(failure) != _PROBE_FAILURE_FIELDS:
            return False
        for key in ("import_root", "distribution", "probe", "reason"):
            if not _valid_probe_text(failure[key]):
                return False
        if len(failure["reason"]) > 500:
            return False
        if (
            failure["import_root"],
            failure["distribution"],
            failure["probe"],
        ) not in allowed_failure_targets:
            return False
        failure_keys.append(
            (
                failure["import_root"],
                _normalize_distribution(failure["distribution"]),
                failure["probe"],
                failure["reason"],
            )
        )
    if failure_keys != sorted(failure_keys) or len(failure_keys) != len(
        set(failure_keys)
    ):
        return False
    if type(value["ok"]) is not bool:
        return False
    return (returncode == 0 and value["ok"] is True and not failures) or (
        returncode == 1 and value["ok"] is False and bool(failures)
    )


def _valid_probe_text(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        return False
    return True


def _probe_environment(
    source: Mapping[str, str] | None = None,
    *,
    windows: bool | None = None,
) -> dict[str, str]:
    try:
        return probe_transport.sanitize_probe_environment(
            probe_transport.PROFILE_PROBE_POLICY,
            source,
            windows=windows,
        )
    except ValueError:
        raise ValueError("invalid profile probe environment") from None


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
    redactions.update(value for value in os.environ.values() if value)
    for value in sorted(redactions, key=lambda value: (-len(value), value)):
        if value:
            message = message.replace(value, "<redacted>")
    message = _PATH_PATTERN.sub("<path>", message)
    message = " ".join(message.split())
    exception_name = type(error).__name__[:128]
    if not message:
        return exception_name
    prefix = f"{exception_name}: "
    message_limit = 500 - len(prefix)
    if len(message) > message_limit:
        message = f"{message[: message_limit - 3]}..."
    return f"{prefix}{message}"


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
                    f"PythonVersionMismatch: expected {python_constraint}, got {actual}"
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
                open(os.devnull, "w", encoding="utf-8") as output_sink,
                contextlib.redirect_stdout(output_sink),
                contextlib.redirect_stderr(output_sink),
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
    encoded = (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _private_probe_main(arguments: list[str]) -> int:
    os.set_inheritable(1, False)
    os.set_inheritable(2, False)
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
