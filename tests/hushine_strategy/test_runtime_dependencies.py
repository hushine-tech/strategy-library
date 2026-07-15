from dataclasses import FrozenInstanceError
import importlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import tomllib

import pytest

import hushine_strategy
import hushine_strategy.runtime_dependencies as runtime_dependencies
from hushine_strategy.runtime_dependencies import (
    RuntimeDependencyProfile,
    load_runtime_dependency_profile,
    probe_runtime_dependency_profile,
    require_runtime_dependency_profile,
)


_VALID_FIXTURE = """\
schema_version = 1
profile_name = "platform-python-3.13"
profile_version = "1.0.0"
hosted_python = "3.13"
debugger_python = ">=3.12"

[[dependencies]]
import_root = "alpha"
distribution = "Alpha_Pkg"
probe = "alpha.first"
public = true
"""


def _write_fixture(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "runtime_dependencies.toml"
    path.write_text(text)
    return path


def _append_dependency(
    text: str,
    *,
    import_root: str,
    distribution: str,
    probe: str,
    public: bool = True,
) -> str:
    return (
        text
        + f"""
[[dependencies]]
import_root = "{import_root}"
distribution = "{distribution}"
probe = "{probe}"
public = {str(public).lower()}
"""
    )


def installed_probe_result(
    profile: RuntimeDependencyProfile,
    *,
    failures: list[tuple[str, str, str, str]] | None = None,
) -> dict[str, object]:
    return {
        "contract_sha256": profile.contract_sha256,
        "failures": [
            {
                "import_root": import_root,
                "distribution": distribution,
                "probe": probe,
                "reason": reason,
            }
            for import_root, distribution, probe, reason in failures or []
        ],
    }


def profile_json(profile: RuntimeDependencyProfile) -> dict[str, object]:
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
            for item in sorted(
                profile.dependencies,
                key=lambda item: (item.import_root, item.distribution, item.probe),
            )
        ],
    }


def complete_probe_result(
    profile: RuntimeDependencyProfile,
    *,
    failures: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    selected_failures = failures or []
    return {
        **profile_json(profile),
        "python_version": ".".join(str(part) for part in sys.version_info[:3]),
        "ok": not selected_failures,
        "failures": selected_failures,
    }


def canonical_probe_bytes(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def install_helper_popen(monkeypatch, helper_source: str):
    real_popen = subprocess.Popen
    calls = []

    def helper_popen(argv, **kwargs):
        private_cwd = Path(kwargs["cwd"])
        private_root = private_cwd.parent
        call = {
            "argv": list(argv),
            "kwargs": kwargs,
            "process": None,
            "root_mode": private_root.stat().st_mode & 0o777,
            "cwd_entries": tuple(private_cwd.iterdir()),
        }
        calls.append(call)
        process = real_popen([sys.executable, "-c", helper_source], **kwargs)
        call["process"] = process
        return process

    monkeypatch.setattr(runtime_dependencies.subprocess, "Popen", helper_popen)
    return calls


def test_packaged_schema_1_profile_is_exact():
    profile = load_runtime_dependency_profile()
    assert profile.schema_version == 1
    assert profile.profile_name == "platform-python-3.13"
    assert profile.profile_version == "1.0.0"
    assert profile.hosted_python == "3.13"
    assert profile.debugger_python == ">=3.12"
    public = tuple(item for item in profile.dependencies if item.public)
    assert len(public) == 8
    assert profile.public_import_roots == tuple(
        sorted(item.import_root for item in public)
    )
    assert all(item.probe.split(".", 1)[0] == item.import_root for item in public)
    assert (
        profile.contract_sha256
        == "8457b3c35618558fc8bfc74d4135b7eb52e00c33a8c9a49d202830f3fd5b62c5"
    )
    assert profile.public_distributions == tuple(
        sorted(item.distribution for item in public)
    )


def test_packaged_resource_is_loaded_independently_of_working_directory(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    assert load_runtime_dependency_profile().profile_name == "platform-python-3.13"


def test_profile_value_objects_are_frozen(tmp_path):
    profile = load_runtime_dependency_profile(_write_fixture(tmp_path, _VALID_FIXTURE))
    with pytest.raises(FrozenInstanceError):
        profile.profile_name = "changed"
    with pytest.raises(FrozenInstanceError):
        profile.dependencies[0].public = False


@pytest.mark.parametrize(
    "required_line",
    [
        "schema_version = 1",
        'profile_name = "platform-python-3.13"',
        'profile_version = "1.0.0"',
        'hosted_python = "3.13"',
        'debugger_python = ">=3.12"',
        'import_root = "alpha"',
        'distribution = "Alpha_Pkg"',
        'probe = "alpha.first"',
        "public = true",
    ],
)
def test_missing_required_field_is_rejected(tmp_path, required_line):
    manifest = _VALID_FIXTURE.replace(f"{required_line}\n", "", 1)
    with pytest.raises(ValueError):
        load_runtime_dependency_profile(_write_fixture(tmp_path, manifest))


def test_missing_dependencies_is_rejected(tmp_path):
    manifest = _VALID_FIXTURE.split("[[dependencies]]", 1)[0]
    with pytest.raises(ValueError):
        load_runtime_dependency_profile(_write_fixture(tmp_path, manifest))


@pytest.mark.parametrize(
    "manifest",
    [
        _VALID_FIXTURE.replace("schema_version = 1", "schema_version = 2"),
        _VALID_FIXTURE.replace(
            'profile_name = "platform-python-3.13"', 'profile_name = ""'
        ),
        _VALID_FIXTURE.replace('profile_version = "1.0.0"', 'profile_version = ""'),
        _VALID_FIXTURE.replace('hosted_python = "3.13"', 'hosted_python = ""'),
        _VALID_FIXTURE.replace('debugger_python = ">=3.12"', 'debugger_python = ""'),
        _VALID_FIXTURE.replace('import_root = "alpha"', 'import_root = ""'),
        _VALID_FIXTURE.replace('distribution = "Alpha_Pkg"', 'distribution = ""'),
        _VALID_FIXTURE.replace('probe = "alpha.first"', 'probe = ""'),
        _append_dependency(
            _VALID_FIXTURE,
            import_root="alpha",
            distribution="beta-dist",
            probe="alpha.second",
        ),
        _append_dependency(
            _VALID_FIXTURE,
            import_root="beta",
            distribution="alpha.pkg",
            probe="beta",
        ),
        _append_dependency(
            _VALID_FIXTURE,
            import_root="alpha",
            distribution="beta-dist",
            probe="alpha.first",
        ),
        _VALID_FIXTURE.replace("public = true", 'public = "true"'),
        _VALID_FIXTURE.replace("public = true", "public = false"),
        _VALID_FIXTURE + "this is not = valid TOML\n",
        _VALID_FIXTURE.replace('probe = "alpha.first"', 'probe = "beta.first"'),
    ],
    ids=[
        "unsupported-schema",
        "empty-profile-name",
        "empty-profile-version",
        "empty-hosted-python",
        "empty-debugger-python",
        "empty-import-root",
        "empty-distribution",
        "empty-probe",
        "duplicate-import-root",
        "duplicate-normalized-distribution",
        "duplicate-probe",
        "non-boolean-public",
        "no-public-entries",
        "malformed-toml",
        "probe-root-mismatch",
    ],
)
def test_invalid_schema_fixture_is_rejected(tmp_path, manifest):
    with pytest.raises(ValueError):
        load_runtime_dependency_profile(_write_fixture(tmp_path, manifest))


@pytest.mark.parametrize(
    "version",
    [
        "1",
        "1.0",
        "v1.0.0",
        "01.0.0",
        "1.01.0",
        "1.0.01",
        "1.0.0-01",
        "1١.0.0",
    ],
)
def test_profile_version_must_be_strict_semver(tmp_path, version):
    manifest = _VALID_FIXTURE.replace(
        'profile_version = "1.0.0"', f'profile_version = "{version}"'
    )
    with pytest.raises(ValueError):
        load_runtime_dependency_profile(_write_fixture(tmp_path, manifest))


@pytest.mark.parametrize(
    ("field", "value"),
    [("hosted_python", "3.14"), ("debugger_python", ">=3.13")],
)
def test_schema_1_python_constraints_are_exact(tmp_path, field, value):
    expected = "3.13" if field == "hosted_python" else ">=3.12"
    manifest = _VALID_FIXTURE.replace(f'{field} = "{expected}"', f'{field} = "{value}"')
    with pytest.raises(ValueError):
        load_runtime_dependency_profile(_write_fixture(tmp_path, manifest))


def test_probe_uses_one_target_process_without_importing_in_caller(monkeypatch):
    profile = load_runtime_dependency_profile()
    calls = []
    monkeypatch.setattr(
        "hushine_strategy.runtime_dependencies._run_installed_probe",
        lambda executable, constraint, env: (
            calls.append((executable, constraint, env))
            or installed_probe_result(
                profile,
                failures=[("grpc", "grpcio", "grpc", "ModuleNotFoundError: grpc")],
            )
        ),
    )
    failures = probe_runtime_dependency_profile(
        profile,
        python_executable="/venv/bin/python",
        python_constraint="3.13",
    )
    assert [(f.import_root, f.distribution, f.probe) for f in failures] == [
        ("grpc", "grpcio", "grpc")
    ]
    assert [(executable, constraint) for executable, constraint, _ in calls] == [
        ("/venv/bin/python", "3.13")
    ]


def test_probe_returns_sorted_frozen_failures(monkeypatch):
    profile = load_runtime_dependency_profile()
    monkeypatch.setattr(
        "hushine_strategy.runtime_dependencies._run_installed_probe",
        lambda executable, constraint, env: installed_probe_result(
            profile,
            failures=[
                ("yaml", "PyYAML", "yaml", "ImportError: yaml"),
                ("grpc", "grpcio", "grpc", "PackageNotFoundError: grpcio"),
            ],
        ),
    )
    failures = probe_runtime_dependency_profile(profile, python_constraint=">=3.12")
    assert [failure.import_root for failure in failures] == ["grpc", "yaml"]
    with pytest.raises(FrozenInstanceError):
        failures[0].reason = "changed"


def test_probe_rejects_target_profile_digest_mismatch(monkeypatch):
    profile = load_runtime_dependency_profile()
    result = installed_probe_result(profile)
    result["contract_sha256"] = "0" * 64
    monkeypatch.setattr(
        "hushine_strategy.runtime_dependencies._run_installed_probe",
        lambda executable, constraint, env: result,
    )
    with pytest.raises(ValueError, match="digest"):
        probe_runtime_dependency_profile(profile, python_constraint="3.13")


def test_probe_rejects_unknown_python_constraint_before_launch(monkeypatch):
    monkeypatch.setattr(
        "hushine_strategy.runtime_dependencies._run_installed_probe",
        lambda executable, constraint, env: pytest.fail("target process was launched"),
    )
    with pytest.raises(ValueError, match="python_constraint"):
        probe_runtime_dependency_profile(python_constraint=">=3.11")


def test_require_returns_profile_or_raises_for_failures(monkeypatch):
    profile = load_runtime_dependency_profile()
    result = installed_probe_result(profile)
    monkeypatch.setattr(
        "hushine_strategy.runtime_dependencies._run_installed_probe",
        lambda executable, constraint, env: result,
    )
    assert (
        require_runtime_dependency_profile(profile, python_constraint=">=3.12")
        is profile
    )

    result["failures"] = [
        {
            "import_root": "grpc",
            "distribution": "grpcio",
            "probe": "grpc",
            "reason": "ModuleNotFoundError: grpc",
        }
    ]
    with pytest.raises(RuntimeError, match="grpc"):
        require_runtime_dependency_profile(profile, python_constraint=">=3.12")


def test_probe_cannot_borrow_caller_python_or_distribution_metadata(
    monkeypatch, tmp_path
):
    profile = load_runtime_dependency_profile()
    dependency = profile.dependencies[0]
    target_result = complete_probe_result(
        profile,
        failures=[
            {
                "import_root": dependency.import_root,
                "distribution": dependency.distribution,
                "probe": dependency.probe,
                "reason": f"PackageNotFoundError: {dependency.distribution}",
            },
            {
                "import_root": dependency.import_root,
                "distribution": dependency.distribution,
                "probe": dependency.probe,
                "reason": "PythonVersionMismatch: expected 3.13, got 3.12",
            },
        ],
    )
    target_bytes = canonical_probe_bytes(target_result)
    target = tmp_path / "target-python"
    target.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import sys\n"
        "expected = ['-I', '-m', 'hushine_strategy.runtime_dependencies', "
        "'_probe-installed', '--python-constraint', '3.13', '--json']\n"
        "if sys.argv[1:] != expected:\n"
        "    raise SystemExit(9)\n"
        f"os.write(1, {target_bytes!r})\n"
        "raise SystemExit(1)\n"
    )
    target.chmod(0o755)
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda distribution: pytest.fail(
            f"caller metadata was consulted for {distribution}"
        ),
    )

    failures = probe_runtime_dependency_profile(
        profile,
        python_executable=str(target),
        python_constraint="3.13",
    )

    assert [failure.reason for failure in failures] == [
        f"PackageNotFoundError: {dependency.distribution}",
        "PythonVersionMismatch: expected 3.13, got 3.12",
    ]


def test_pyproject_does_not_advertise_unsupported_pandas_ta_extra():
    project_root = Path(__file__).resolve().parents[2]
    pyproject = tomllib.loads((project_root / "pyproject.toml").read_text())
    assert "algo" not in pyproject["project"].get("optional-dependencies", {})


def test_pyproject_packages_authoritative_manifest_resource():
    project_root = Path(__file__).resolve().parents[2]
    pyproject = tomllib.loads((project_root / "pyproject.toml").read_text())
    assert pyproject["tool"]["setuptools"]["package-data"] == {
        "hushine_strategy": ["runtime_dependencies.toml"]
    }


def test_installed_probe_checks_metadata_and_imports_in_manifest_order(
    monkeypatch, tmp_path
):
    manifest = _append_dependency(
        _VALID_FIXTURE,
        import_root="beta",
        distribution="beta-dist",
        probe="beta",
    )
    profile = load_runtime_dependency_profile(_write_fixture(tmp_path, manifest))
    events = []
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda distribution: events.append(("metadata", distribution)) or "1.0.0",
    )
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda probe: events.append(("import", probe)) or object(),
    )

    result = runtime_dependencies._installed_probe_result(profile, ">=3.12")

    assert events == [
        ("metadata", "Alpha_Pkg"),
        ("import", "alpha.first"),
        ("metadata", "beta-dist"),
        ("import", "beta"),
    ]
    assert result["ok"] is True
    assert result["failures"] == []


def test_installed_probe_sanitizes_failures_and_import_output(
    monkeypatch, tmp_path, capsys
):
    profile = load_runtime_dependency_profile(_write_fixture(tmp_path, _VALID_FIXTURE))
    secret = "probe-super-secret-value"
    leaked_path = str(tmp_path / "private" / "module.py")
    monkeypatch.setenv("PROBE_SECRET_CANARY", secret)

    def missing_metadata(distribution):
        raise importlib.metadata.PackageNotFoundError(
            f"{distribution} {secret} {leaked_path}"
        )

    def broken_import(probe):
        print(f"do-not-emit {secret} {leaked_path}")
        raise RuntimeError(f"{probe} {secret} {leaked_path}")

    monkeypatch.setattr(importlib.metadata, "version", missing_metadata)
    monkeypatch.setattr(importlib, "import_module", broken_import)

    result = runtime_dependencies._installed_probe_result(profile, ">=3.12")

    captured = capsys.readouterr()
    encoded = json.dumps(result)
    assert captured.out == ""
    assert captured.err == ""
    assert result["ok"] is False
    assert len(result["failures"]) == 2
    assert "PackageNotFoundError" in encoded
    assert "RuntimeError" in encoded
    assert secret not in encoded
    assert leaked_path not in encoded
    assert "sys.path" not in encoded
    assert "environment" not in encoded


def test_safe_exception_redaction_is_order_independent_for_equal_length_overlaps(
    monkeypatch,
):
    class OrderedRedactions:
        def __init__(self, values):
            self._values = []
            self.update(values)

        def add(self, value):
            if value not in self._values:
                self._values.append(value)

        def update(self, values):
            for value in values:
                self.add(value)

        def __iter__(self):
            return iter(self._values)

    monkeypatch.setattr(runtime_dependencies, "set", OrderedRedactions, raising=False)
    monkeypatch.setattr(runtime_dependencies.os, "getcwd", lambda: "/unrelated")
    monkeypatch.setattr(runtime_dependencies.os, "environ", {})

    reasons = []
    for paths in (["abcd", "bcde"], ["bcde", "abcd"]):
        monkeypatch.setattr(runtime_dependencies.sys, "path", paths)
        reasons.append(
            runtime_dependencies._safe_exception_reason(RuntimeError("abcde"))
        )

    assert reasons == ["RuntimeError: <redacted>e"] * 2


def test_installed_probe_reports_target_python_version_mismatch(monkeypatch, tmp_path):
    profile = load_runtime_dependency_profile(_write_fixture(tmp_path, _VALID_FIXTURE))
    monkeypatch.setattr(
        runtime_dependencies,
        "_current_python_version",
        lambda: (3, 12, 9),
    )
    monkeypatch.setattr(importlib.metadata, "version", lambda distribution: "1.0")
    monkeypatch.setattr(importlib, "import_module", lambda probe: object())

    result = runtime_dependencies._installed_probe_result(profile, "3.13")

    assert result["python_version"] == "3.12.9"
    assert result["ok"] is False
    assert any("expected 3.13" in item["reason"] for item in result["failures"])


@pytest.mark.parametrize(
    ("failures", "expected_exit"),
    [
        ([], 0),
        (
            [
                {
                    "import_root": "grpc",
                    "distribution": "grpcio",
                    "probe": "grpc",
                    "reason": "ModuleNotFoundError: grpc",
                }
            ],
            1,
        ),
    ],
)
def test_verify_installed_cli_exit_matches_json_ok(
    monkeypatch, capsys, failures, expected_exit
):
    profile = load_runtime_dependency_profile()
    payload = profile_json(profile)
    payload.update(
        {
            "python_version": "3.13.5",
            "ok": not failures,
            "failures": failures,
        }
    )
    calls = []
    monkeypatch.setattr(
        runtime_dependencies,
        "_installed_probe_result",
        lambda selected_profile, constraint: (
            calls.append((selected_profile, constraint)) or payload
        ),
    )

    exit_code = runtime_dependencies.main(
        ["verify-installed", "--python-constraint", "3.13", "--json"]
    )

    body = json.loads(capsys.readouterr().out)
    assert exit_code == expected_exit
    assert body == payload
    assert calls == [(profile, "3.13")]


def test_module_show_json_is_exact_and_private_command_is_undocumented():
    profile = load_runtime_dependency_profile()
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "hushine_strategy.runtime_dependencies",
            "show",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    help_result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "hushine_strategy.runtime_dependencies",
            "--help",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == profile_json(profile)
    assert help_result.returncode == 0
    assert "show" in help_result.stdout
    assert "verify-installed" in help_result.stdout
    assert "_probe-installed" not in help_result.stdout


def test_private_target_command_returns_parseable_probe_json():
    profile = load_runtime_dependency_profile()
    environment = runtime_dependencies._probe_environment()
    result = runtime_dependencies._run_installed_probe(
        sys.executable,
        ">=3.12",
        environment,
    )
    assert result["contract_sha256"] == profile.contract_sha256
    assert isinstance(result["failures"], list)
    assert isinstance(result["ok"], bool)


def test_profile_probe_environment_is_an_exact_allowlist(monkeypatch):
    allowed = {
        "PATH": "/safe/bin",
        "SOURCE_DATE_EPOCH": "1700000000",
        "LANG": "C.UTF-8",
        "LANGUAGE": "en_US:en",
        "LC_ADDRESS": "C",
        "LC_ALL": "C.UTF-8",
        "LC_COLLATE": "C",
        "LC_CTYPE": "C.UTF-8",
        "LC_IDENTIFICATION": "C",
        "LC_MEASUREMENT": "C",
        "LC_MESSAGES": "C.UTF-8",
        "LC_MONETARY": "C",
        "LC_NAME": "C",
        "LC_NUMERIC": "C",
        "LC_PAPER": "C",
        "LC_TELEPHONE": "C",
        "LC_TIME": "C",
        "TZ": "UTC",
    }
    poisoned = {
        "PYTHONPATH": "probe-pythonpath-canary",
        "PYTHONHOME": "probe-pythonhome-canary",
        "VIRTUAL_ENV": "probe-venv-canary",
        "UV_PROJECT_ENVIRONMENT": "probe-uv-canary",
        "DATABASE_URL": "probe-database-canary",
        "POSTGRES_PASSWORD": "probe-postgres-canary",
        "KAFKA_BROKERS": "probe-kafka-canary",
        "CORE_SERVICE_ADDR": "probe-core-canary",
        "ORDER_SERVICE_ADDR": "probe-order-canary",
        "CONTROL_PANEL_ADDR": "probe-control-canary",
        "RUNTIME_TOKEN": "probe-runtime-canary",
        "VENUE_API_SECRET": "probe-venue-canary",
        "AUTH_TOKEN": "probe-auth-canary",
        "AWS_SECRET_ACCESS_KEY": "probe-cloud-canary",
        "HTTPS_PROXY": "probe-proxy-canary",
        "GIT_CONFIG_COUNT": "1",
        "LC_TOKEN": "probe-locale-lookalike-canary",
        "HOME": "/inherited/home",
        "USERPROFILE": "C:\\inherited-home",
        "TMP": "/inherited/tmp",
        "TEMP": "/inherited/temp",
        "TMPDIR": "/inherited/tmpdir",
        "HUSHINE_RUNTIME_IMAGE_BUILD_ID": "probe-build-id-canary",
        "HUSHINE_RUNTIME_STRATEGY_LIBRARY_COMMIT": "probe-library-canary",
        "HUSHINE_RUNTIME_STRATEGY_SERVICE_COMMIT": "probe-service-canary",
    }
    for key, value in {**allowed, **poisoned}.items():
        monkeypatch.setenv(key, value)

    environment = runtime_dependencies._probe_environment()

    assert environment == allowed
    assert runtime_dependencies.PROFILE_PROBE_ENV_KEYS == frozenset(
        {*allowed, "SYSTEMROOT", "WINDIR"}
    )


def test_profile_probe_environment_is_case_insensitive_only_on_windows():
    source = {
        "Path": "C:\\safe-bin",
        "lAnG": "C.UTF-8",
        "systemRoot": "C:\\Windows",
        "windir": "C:\\Windows",
        "lc_token": "probe-lookalike-canary",
        "PythonPath": "probe-python-canary",
    }

    assert runtime_dependencies._probe_environment(source, windows=True) == {
        "PATH": "C:\\safe-bin",
        "LANG": "C.UTF-8",
        "SYSTEMROOT": "C:\\Windows",
        "WINDIR": "C:\\Windows",
    }
    assert runtime_dependencies._probe_environment(source, windows=False) == {}
    with pytest.raises(ValueError, match="invalid profile probe environment"):
        runtime_dependencies._probe_environment(
            {"PATH": "one", "Path": "two"}, windows=True
        )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "-1",
        "+1",
        "1.0",
        "1e3",
        "１",
        "1\n",
        "1\0",
        "1" * 21,
    ],
)
def test_source_date_epoch_must_be_bounded_ascii_digits(value):
    assert (
        runtime_dependencies._probe_environment(
            {"SOURCE_DATE_EPOCH": value}, windows=False
        )
        == {}
    )


def test_source_date_epoch_accepts_twenty_ascii_digits():
    assert runtime_dependencies._probe_environment(
        {"SOURCE_DATE_EPOCH": "1" * 20}, windows=False
    ) == {"SOURCE_DATE_EPOCH": "1" * 20}


def test_installed_probe_uses_popen_not_subprocess_run(monkeypatch):
    monkeypatch.setattr(
        runtime_dependencies.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("subprocess.run must not be used"),
    )

    result = runtime_dependencies._run_installed_probe(
        os.path.abspath(os.path.normpath(sys.executable)),
        ">=3.12",
        runtime_dependencies._probe_environment(),
    )

    assert (
        result["contract_sha256"] == load_runtime_dependency_profile().contract_sha256
    )


def test_installed_probe_import_capture_never_uses_stringio(monkeypatch, tmp_path):
    profile = load_runtime_dependency_profile(_write_fixture(tmp_path, _VALID_FIXTURE))
    monkeypatch.setattr(importlib.metadata, "version", lambda _distribution: "1.0")
    monkeypatch.setattr(importlib, "import_module", lambda _probe: object())
    monkeypatch.setattr(
        io,
        "StringIO",
        lambda *_args, **_kwargs: pytest.fail("unbounded StringIO must not be used"),
    )

    assert runtime_dependencies._installed_probe_result(profile, ">=3.12")["ok"] is True


def test_installed_probe_bounds_oversized_import_output_and_failure(
    monkeypatch, tmp_path, capsys
):
    profile = load_runtime_dependency_profile(_write_fixture(tmp_path, _VALID_FIXTURE))
    printed = "printed-import-canary-" * 5000
    raised = "oversized-import-failure-" * 5000
    monkeypatch.setattr(importlib.metadata, "version", lambda _distribution: "1.0")

    def noisy_import(_probe):
        print(printed)
        raise RuntimeError(raised)

    monkeypatch.setattr(importlib, "import_module", noisy_import)
    monkeypatch.setattr(
        io,
        "StringIO",
        lambda *_args, **_kwargs: pytest.fail("unbounded StringIO must not be used"),
    )

    result = runtime_dependencies._installed_probe_result(profile, ">=3.12")

    captured = capsys.readouterr()
    encoded = json.dumps(result)
    assert captured.out == ""
    assert captured.err == ""
    assert printed not in encoded
    assert result["ok"] is False
    assert len(result["failures"]) == 1
    assert len(result["failures"][0]["reason"]) <= 500
    assert len(encoded.encode("utf-8")) < 2048


def test_runner_resanitizes_environment_and_uses_private_directories(
    monkeypatch, tmp_path
):
    profile = load_runtime_dependency_profile()
    payload = canonical_probe_bytes(complete_probe_result(profile))
    calls = install_helper_popen(
        monkeypatch,
        f"import os\nos.write(1, {payload!r})\n",
    )
    executable = os.path.abspath(os.path.normpath(sys.executable))
    supplied = {
        "PATH": os.environ.get("PATH", ""),
        "LANG": "C.UTF-8",
        "LC_TOKEN": "probe-locale-canary",
        "PythonPath": "probe-python-canary",
        "DATABASE_URL": "probe-db-canary",
        "HOME": "/inherited/home",
        "TMPDIR": "/inherited/tmp",
        "HUSHINE_RUNTIME_IMAGE_BUILD_ID": "probe-build-canary",
    }

    result = runtime_dependencies._run_installed_probe(executable, ">=3.12", supplied)

    assert result == complete_probe_result(profile)
    assert len(calls) == 1
    call = calls[0]
    assert call["argv"][0] == executable
    kwargs = call["kwargs"]
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert kwargs["close_fds"] is True
    assert kwargs["bufsize"] == 0
    environment = kwargs["env"]
    assert environment["PATH"] == supplied["PATH"]
    assert environment["LANG"] == "C.UTF-8"
    assert not {
        "LC_TOKEN",
        "PythonPath",
        "DATABASE_URL",
        "HUSHINE_RUNTIME_IMAGE_BUILD_ID",
    }.intersection(environment)
    private_cwd = Path(kwargs["cwd"])
    private_root = private_cwd.parent
    assert private_root != Path("/inherited/home")
    assert environment["HOME"] == str(private_root / "home")
    assert environment["TMP"] == str(private_root / "tmp")
    assert environment["TEMP"] == str(private_root / "tmp")
    assert environment["TMPDIR"] == str(private_root / "tmp")
    if os.name != "nt":
        assert call["root_mode"] == 0o700
    assert call["cwd_entries"] == ()
    assert not private_root.exists()


def test_runner_preserves_final_python_symlink_in_argv(monkeypatch, tmp_path):
    profile = load_runtime_dependency_profile()
    payload = canonical_probe_bytes(complete_probe_result(profile))
    calls = install_helper_popen(
        monkeypatch,
        f"import os\nos.write(1, {payload!r})\n",
    )
    invocation = tmp_path / "venv" / "bin" / "python"
    invocation.parent.mkdir(parents=True)
    invocation.symlink_to(sys.executable)
    normalized = os.path.abspath(os.path.normpath(str(invocation)))

    runtime_dependencies._run_installed_probe(
        normalized, ">=3.12", runtime_dependencies._probe_environment()
    )

    assert calls[0]["argv"][0] == normalized
    assert calls[0]["argv"][0] != os.path.realpath(normalized)


@pytest.mark.parametrize(
    "executable",
    ["python", "/tmp/../tmp/python", "/tmp/python\x00canary", "/" + "é" * 4097],
)
def test_runner_rejects_noncanonical_or_oversized_invocation_before_launch(
    monkeypatch, executable
):
    monkeypatch.setattr(
        runtime_dependencies.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("invalid invocation was launched"),
    )

    with pytest.raises(ValueError, match="invalid target Python invocation") as caught:
        runtime_dependencies._run_installed_probe(executable, ">=3.12", {})

    assert "canary" not in str(caught.value)


def test_probe_argv_limit_is_measured_in_utf8_bytes_at_exact_boundary():
    fixed = ["-I", "-m", "probe", "_probe-installed", "--json"]
    fixed_size = sum(len(item.encode("utf-8")) + 1 for item in fixed)
    executable_bytes = runtime_dependencies._PROBE_ARGV_LIMIT - fixed_size - 1
    executable = "/" + ("a" * (executable_bytes - 1))

    assert runtime_dependencies._valid_probe_argv([executable, *fixed]) is True
    assert runtime_dependencies._valid_probe_argv([executable + "a", *fixed]) is False
    unicode_executable = "/" + ("é" * ((executable_bytes - 1) // 2))
    assert runtime_dependencies._valid_probe_argv([unicode_executable, *fixed]) is True


@pytest.mark.parametrize("constraint", ["3.12", ">=3.11", "", 313])
def test_runner_rejects_unknown_constraint_before_launch(monkeypatch, constraint):
    monkeypatch.setattr(
        runtime_dependencies.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("invalid constraint was launched"),
    )
    with pytest.raises(ValueError, match="invalid target Python constraint"):
        runtime_dependencies._run_installed_probe(
            os.path.abspath(sys.executable), constraint, {}
        )


def _run_invalid_protocol(monkeypatch, stdout: bytes, *, stderr=b"", status=0):
    helper = (
        "import os\n"
        f"os.write(1, {stdout!r})\n"
        f"os.write(2, {stderr!r})\n"
        f"raise SystemExit({status})\n"
    )
    calls = install_helper_popen(monkeypatch, helper)
    with pytest.raises(RuntimeError) as caught:
        runtime_dependencies._run_installed_probe(
            os.path.abspath(os.path.normpath(sys.executable)), ">=3.12", {}
        )
    return caught.value, calls


@pytest.mark.parametrize(
    ("mutate", "stderr", "status"),
    [
        (lambda body: body[:-1] + b"\r\n", b"", 0),
        (lambda body: body + b"\n", b"", 0),
        (lambda body: b"\xef\xbb\xbf" + body, b"", 0),
        (
            lambda body: body.replace(
                b'"contract_sha256":', b'"extra":1,"contract_sha256":', 1
            ),
            b"",
            0,
        ),
        (
            lambda body: body.replace(
                b'"contract_sha256":',
                b'"contract_sha256":"duplicate","contract_sha256":',
                1,
            ),
            b"",
            0,
        ),
        (lambda body: json.dumps(json.loads(body), indent=2).encode() + b"\n", b"", 0),
        (lambda body: body, b"stderr-canary", 0),
        (lambda body: body, b"", 1),
        (lambda body: body, b"", 70),
    ],
)
def test_runner_rejects_noncanonical_or_inconsistent_protocol(
    monkeypatch, mutate, stderr, status
):
    body = canonical_probe_bytes(
        complete_probe_result(load_runtime_dependency_profile())
    )
    error, calls = _run_invalid_protocol(
        monkeypatch, mutate(body), stderr=stderr, status=status
    )

    assert str(error) == "target dependency probe returned an invalid response"
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert "canary" not in str(error)
    assert calls[0]["process"].poll() is not None
    assert not Path(calls[0]["kwargs"]["cwd"]).parent.exists()


def test_runner_converts_deep_json_recursion_to_fixed_protocol_error(monkeypatch):
    nested = (b"[" * 30000) + (b"]" * 30000) + b"\n"

    error, calls = _run_invalid_protocol(monkeypatch, nested)

    assert str(error) == "target dependency probe returned an invalid response"
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert calls[0]["process"].poll() is not None
    assert not Path(calls[0]["kwargs"]["cwd"]).parent.exists()


def test_protocol_rejects_duplicate_nested_dependency_key():
    body = canonical_probe_bytes(
        complete_probe_result(load_runtime_dependency_profile())
    )
    duplicate = body.replace(
        b'"dependencies":[{"distribution":',
        b'"dependencies":[{"distribution":"duplicate","distribution":',
        1,
    )
    assert runtime_dependencies._parse_probe_response(duplicate, b"", 0) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(schema_version=True),
        lambda payload: payload.update(public_import_roots=[1]),
        lambda payload: payload["dependencies"][0].update(public=1),
        lambda payload: payload["dependencies"][0].update(probe="wrong.root"),
        lambda payload: payload["dependencies"][0].update(extra="field"),
        lambda payload: payload.pop("python_version"),
        lambda payload: payload.update(ok=False),
        lambda payload: payload.update(
            failures=[
                {
                    "import_root": "numpy",
                    "distribution": "numpy",
                    "probe": "numpy",
                    "reason": "ModuleNotFoundError",
                }
            ]
        ),
    ],
)
def test_protocol_rejects_schema_type_shape_and_status_bypasses(mutate):
    payload = complete_probe_result(load_runtime_dependency_profile())
    mutate(payload)
    assert (
        runtime_dependencies._parse_probe_response(
            canonical_probe_bytes(payload), b"", 0
        )
        is None
    )


def test_protocol_validator_is_total_for_mixed_public_root_types():
    payload = complete_probe_result(load_runtime_dependency_profile())
    payload["public_import_roots"] = ["numpy", 1]
    assert (
        runtime_dependencies._parse_probe_response(
            canonical_probe_bytes(payload), b"", 0
        )
        is None
    )


def test_runner_accepts_canonical_exit_one_failure(monkeypatch):
    profile = load_runtime_dependency_profile()
    failure = {
        "import_root": "numpy",
        "distribution": "numpy",
        "probe": "numpy",
        "reason": "ModuleNotFoundError",
    }
    payload = complete_probe_result(profile, failures=[failure])
    calls = install_helper_popen(
        monkeypatch,
        "import os\n"
        f"os.write(1, {canonical_probe_bytes(payload)!r})\n"
        "raise SystemExit(1)\n",
    )

    assert (
        runtime_dependencies._run_installed_probe(
            os.path.abspath(os.path.normpath(sys.executable)), ">=3.12", {}
        )
        == payload
    )
    assert calls[0]["process"].returncode == 1


def test_runner_times_out_terminates_reaps_joins_and_removes_root(monkeypatch):
    calls = install_helper_popen(
        monkeypatch,
        "import time\ntime.sleep(60)\n",
    )
    monkeypatch.setattr(runtime_dependencies, "_PROBE_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(runtime_dependencies, "_PROBE_TERMINATE_GRACE_SECONDS", 0.1)
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="target dependency probe timed out"):
        runtime_dependencies._run_installed_probe(
            os.path.abspath(os.path.normpath(sys.executable)), ">=3.12", {}
        )

    assert time.monotonic() - started < 2
    assert calls[0]["process"].poll() is not None
    assert not Path(calls[0]["kwargs"]["cwd"]).parent.exists()
    assert not any(
        thread.name.startswith("runtime-profile-probe-")
        for thread in threading.enumerate()
    )


@pytest.mark.skipif(os.name == "nt", reason="native Windows has terminate semantics")
def test_runner_escalates_from_ignored_terminate_to_kill(monkeypatch):
    calls = install_helper_popen(
        monkeypatch,
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(60)\n",
    )
    monkeypatch.setattr(runtime_dependencies, "_PROBE_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(runtime_dependencies, "_PROBE_TERMINATE_GRACE_SECONDS", 0.2)

    with pytest.raises(RuntimeError, match="target dependency probe timed out"):
        runtime_dependencies._run_installed_probe(
            os.path.abspath(os.path.normpath(sys.executable)), ">=3.12", {}
        )

    assert calls[0]["process"].poll() is not None
    assert calls[0]["process"].returncode != 0
    assert not Path(calls[0]["kwargs"]["cwd"]).parent.exists()


def test_runner_cleans_up_when_second_reader_cannot_start(monkeypatch):
    calls = install_helper_popen(
        monkeypatch,
        "import time\ntime.sleep(60)\n",
    )
    real_start = threading.Thread.start

    def guarded_start(thread):
        if thread.name == "runtime-profile-probe-stderr":
            raise RuntimeError("reader-start-canary")
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", guarded_start)
    monkeypatch.setattr(runtime_dependencies, "_PROBE_TIMEOUT_SECONDS", 1.0)

    with pytest.raises(
        RuntimeError, match="target dependency probe returned an invalid response"
    ) as caught:
        runtime_dependencies._run_installed_probe(
            os.path.abspath(os.path.normpath(sys.executable)), ">=3.12", {}
        )

    assert "canary" not in str(caught.value)
    assert calls[0]["process"].poll() is not None
    assert not Path(calls[0]["kwargs"]["cwd"]).parent.exists()
    assert not any(
        thread.name.startswith("runtime-profile-probe-")
        for thread in threading.enumerate()
    )


def test_runner_cleans_up_when_reader_reports_failure(monkeypatch):
    calls = install_helper_popen(
        monkeypatch,
        "import time\ntime.sleep(60)\n",
    )

    def failed_reader(_pipe, _buffer, _overflow, failed, _stop, _eof, _deadline):
        failed.set()

    monkeypatch.setattr(runtime_dependencies, "_read_bounded_pipe", failed_reader)
    monkeypatch.setattr(runtime_dependencies, "_PROBE_TIMEOUT_SECONDS", 1.0)

    with pytest.raises(
        RuntimeError, match="target dependency probe returned an invalid response"
    ):
        runtime_dependencies._run_installed_probe(
            os.path.abspath(os.path.normpath(sys.executable)), ">=3.12", {}
        )

    assert calls[0]["process"].poll() is not None
    assert not Path(calls[0]["kwargs"]["cwd"]).parent.exists()


def test_runner_deadline_survives_descendant_holding_both_pipes(monkeypatch, tmp_path):
    profile = load_runtime_dependency_profile()
    payload = canonical_probe_bytes(complete_probe_result(profile))
    pid_path = tmp_path / "descendant.pid"
    calls = install_helper_popen(
        monkeypatch,
        "import os, pathlib, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(2)'])\n"
        f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid))\n"
        f"os.write(1, {payload!r})\n",
    )
    monkeypatch.setattr(runtime_dependencies, "_PROBE_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(runtime_dependencies, "_PROBE_TERMINATE_GRACE_SECONDS", 0.1)
    started = time.monotonic()

    try:
        with pytest.raises(
            RuntimeError,
            match="target dependency probe returned an invalid response",
        ):
            runtime_dependencies._run_installed_probe(
                os.path.abspath(os.path.normpath(sys.executable)), ">=3.12", {}
            )
        assert time.monotonic() - started < 0.8
        assert calls[0]["process"].poll() is not None
        assert not Path(calls[0]["kwargs"]["cwd"]).parent.exists()
        assert not any(
            thread.name.startswith("runtime-profile-probe-")
            for thread in threading.enumerate()
        )
    finally:
        if pid_path.exists():
            try:
                os.kill(int(pid_path.read_text()), signal.SIGTERM)
            except (OSError, ValueError):
                pass


def test_private_probe_marks_protocol_descriptors_noninheritable(monkeypatch):
    profile = load_runtime_dependency_profile()
    result = complete_probe_result(profile)
    calls = []
    monkeypatch.setattr(
        runtime_dependencies.os,
        "set_inheritable",
        lambda descriptor, inheritable: calls.append((descriptor, inheritable)),
    )
    monkeypatch.setattr(
        runtime_dependencies,
        "_installed_probe_result",
        lambda _profile, _constraint: result,
    )
    monkeypatch.setattr(runtime_dependencies, "_emit_json", lambda value: None)

    assert runtime_dependencies._private_probe_main(["--json"]) == 0
    assert calls == [(1, False), (2, False)]


def test_runner_caps_both_pipes_and_reaps_on_overflow(monkeypatch):
    calls = install_helper_popen(
        monkeypatch,
        "import os, threading, time\n"
        "a = threading.Thread(target=lambda: os.write(1, b'x' * 70000))\n"
        "b = threading.Thread(target=lambda: os.write(2, b'y' * 70000))\n"
        "a.start(); b.start(); a.join(); b.join(); time.sleep(60)\n",
    )
    monkeypatch.setattr(runtime_dependencies, "_PROBE_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(runtime_dependencies, "_PROBE_TERMINATE_GRACE_SECONDS", 0.1)

    with pytest.raises(
        RuntimeError, match="target dependency probe output limit exceeded"
    ):
        runtime_dependencies._run_installed_probe(
            os.path.abspath(os.path.normpath(sys.executable)), ">=3.12", {}
        )

    assert calls[0]["process"].poll() is not None
    assert not Path(calls[0]["kwargs"]["cwd"]).parent.exists()
    assert not any(
        thread.name.startswith("runtime-profile-probe-")
        for thread in threading.enumerate()
    )


def test_runner_launch_failure_has_fixed_error_and_cleans_private_root(
    monkeypatch, tmp_path
):
    canary = "probe-launch-path-canary"
    monkeypatch.setattr(runtime_dependencies.tempfile, "tempdir", str(tmp_path))

    with pytest.raises(RuntimeError) as caught:
        runtime_dependencies._run_installed_probe(
            str(tmp_path / canary), ">=3.12", {"AUTH_TOKEN": canary}
        )

    assert str(caught.value) == "target dependency probe could not be started"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert canary not in str(caught.value)
    assert not list(tmp_path.glob("hushine-profile-probe-*"))


@pytest.mark.parametrize(
    ("platform_name", "version", "expected"),
    [
        ("posix", (3, 12, 0), True),
        ("nt", (3, 12, 0), False),
        ("nt", (3, 12, 3), False),
        ("nt", (3, 12, 4), True),
        ("nt", (3, 13, 0), True),
    ],
)
def test_private_directory_support_rejects_vulnerable_windows_python(
    platform_name, version, expected
):
    assert (
        runtime_dependencies._secure_private_directories_supported(
            platform_name=platform_name,
            version_info=version,
        )
        is expected
    )


def test_private_root_fails_before_creation_without_secure_windows_acl(monkeypatch):
    monkeypatch.setattr(
        runtime_dependencies,
        "_secure_private_directories_supported",
        lambda **_kwargs: False,
        raising=False,
    )
    monkeypatch.setattr(
        runtime_dependencies.tempfile,
        "mkdtemp",
        lambda **_kwargs: pytest.fail("insecure directory was created"),
    )

    with pytest.raises(
        RuntimeError,
        match="secure private probe directories are unavailable",
    ):
        runtime_dependencies._create_private_probe_root()


def test_emit_json_writes_canonical_utf8_with_exact_lf(monkeypatch):
    class BinaryStdout:
        def __init__(self):
            self.buffer = io.BytesIO()

        def write(self, _value):
            pytest.fail("text stdout may translate LF to CRLF")

    output = BinaryStdout()
    monkeypatch.setattr(runtime_dependencies.sys, "stdout", output)

    runtime_dependencies._emit_json({"value": "é", "ok": True})

    assert output.buffer.getvalue() == b'{"ok":true,"value":"\\u00e9"}\n'


@pytest.mark.parametrize(
    "message",
    [
        r"failed at \\server\share\private dir\x.py",
        r"failed at \\?\C:\private dir\x.py",
        r"failed at C:\private dir\x.py",
    ],
)
def test_safe_exception_reason_redacts_windows_paths_with_spaces(message):
    reason = runtime_dependencies._safe_exception_reason(RuntimeError(message))
    assert "private dir" not in reason
    assert "server" not in reason


def test_safe_exception_reason_redacts_short_environment_canary(monkeypatch):
    monkeypatch.setenv("PROBE_SHORT_SECRET", "abc")
    assert "abc" not in runtime_dependencies._safe_exception_reason(RuntimeError("abc"))


def test_root_package_exports_stable_runtime_dependency_api():
    for name in (
        "RuntimeDependency",
        "RuntimeDependencyProfile",
        "DependencyProbeFailure",
        "load_runtime_dependency_profile",
        "probe_runtime_dependency_profile",
        "require_runtime_dependency_profile",
    ):
        assert getattr(hushine_strategy, name) is getattr(runtime_dependencies, name)
        assert name in hushine_strategy.__all__
