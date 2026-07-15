from dataclasses import FrozenInstanceError
import importlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

import hushine_strategy
import hushine_strategy.runtime_dependencies as runtime_dependencies
from hushine_strategy.runtime_dependencies import (
    DependencyProbeFailure,
    RuntimeDependency,
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
    return text + f"""
[[dependencies]]
import_root = "{import_root}"
distribution = "{distribution}"
probe = "{probe}"
public = {str(public).lower()}
"""


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
    manifest = _VALID_FIXTURE.replace(
        f'{field} = "{expected}"', f'{field} = "{value}"'
    )
    with pytest.raises(ValueError):
        load_runtime_dependency_profile(_write_fixture(tmp_path, manifest))


def test_probe_uses_one_target_process_without_importing_in_caller(monkeypatch):
    profile = load_runtime_dependency_profile()
    calls = []
    monkeypatch.setattr(
        "hushine_strategy.runtime_dependencies._run_installed_probe",
        lambda executable, constraint, env: calls.append(
            (executable, constraint, env)
        )
        or installed_probe_result(
            profile,
            failures=[
                ("grpc", "grpcio", "grpc", "ModuleNotFoundError: grpc")
            ],
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
    target_result = installed_probe_result(
        profile,
        failures=[
            (
                dependency.import_root,
                dependency.distribution,
                dependency.probe,
                "PythonVersionMismatch: expected 3.13, got 3.12",
            ),
            (
                dependency.import_root,
                dependency.distribution,
                dependency.probe,
                f"PackageNotFoundError: {dependency.distribution}",
            ),
        ],
    )
    target = tmp_path / "target-python"
    target.write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "import sys\n"
        "expected = ['-I', '-m', 'hushine_strategy.runtime_dependencies', "
        "'_probe-installed', '--python-constraint', '3.13', '--json']\n"
        "if sys.argv[1:] != expected:\n"
        "    raise SystemExit(9)\n"
        f"print(json.dumps({json.dumps(target_result)!r} and "
        f"json.loads({json.dumps(target_result)!r})))\n"
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


def test_installed_probe_reports_target_python_version_mismatch(
    monkeypatch, tmp_path
):
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
        lambda selected_profile, constraint: calls.append(
            (selected_profile, constraint)
        )
        or payload,
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
