from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import tomllib

import pytest

import scripts.check_runtime_dependency_contract as checker
from hushine_strategy.runtime_dependencies import (
    RuntimeDependency,
    RuntimeDependencyProfile,
)
from scripts.check_runtime_dependency_contract import (
    ContractConfigurationError,
    check_baseline,
    check_installed_projection,
    check_profile_change,
    check_project_projection,
    sync_project_projection,
)

BEGIN = "# BEGIN GENERATED RUNTIME DEPENDENCY PROJECTION"
END = "# END GENERATED RUNTIME DEPENDENCY PROJECTION"
INITIAL_MANIFEST = (
    Path(__file__).parents[1] / "hushine_strategy" / "runtime_dependencies.toml"
).read_bytes()
SCRIPT = Path(__file__).parents[1] / "scripts" / "check_runtime_dependency_contract.py"
ORIGINAL_RUN_UV_LOCK_CHECK = checker._run_uv_lock_check


def test_internal_import_probe_package_is_explicitly_wheel_packaged():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())

    includes = project["tool"]["setuptools"]["packages"]["find"]["include"]
    assert "hushine_runtime_import_probe*" in includes


@pytest.fixture(autouse=True)
def successful_uv_lock_check(monkeypatch):
    monkeypatch.setattr(checker, "_run_uv_lock_check", lambda *_: ())


def profile_for(*distributions: str) -> RuntimeDependencyProfile:
    dependencies = tuple(
        RuntimeDependency(
            import_root=distribution.lower().replace("-", "_"),
            distribution=distribution,
            probe=distribution.lower().replace("-", "_"),
            public=True,
        )
        for distribution in distributions
    )
    return RuntimeDependencyProfile(
        schema_version=1,
        profile_name="platform-python-3.13",
        profile_version="1.0.0",
        hosted_python="3.13",
        debugger_python=">=3.12",
        dependencies=dependencies,
        contract_sha256="caller-contract-digest",
    )


def manifest_bytes(
    *, version: str = "1.0.0", roots: tuple[str, ...] = ("numpy",)
) -> bytes:
    rows = [
        "schema_version = 1",
        'profile_name = "platform-python-3.13"',
        f'profile_version = "{version}"',
        'hosted_python = "3.13"',
        'debugger_python = ">=3.12"',
        "",
    ]
    for root in roots:
        rows.extend(
            [
                "[[dependencies]]",
                f'import_root = "{root}"',
                f'distribution = "{root}"',
                f'probe = "{root}"',
                "public = true",
                "",
            ]
        )
    return "\n".join(rows).encode()


def write_project(
    tmp_path: Path,
    *,
    outside: tuple[str, ...] = (),
    generated: tuple[str, ...] = (),
    locked: tuple[str, ...] = (),
) -> tuple[Path, Path]:
    project = tmp_path / "pyproject.toml"
    dependency_lines = [*(f'  "{item}",' for item in outside)]
    dependency_lines.extend(
        [
            f"  {BEGIN}",
            *(f'  "{item}",' for item in generated),
            f"  {END}",
        ]
    )
    project.write_text(
        "\n".join(
            [
                "[project]",
                'name = "fixture"',
                'version = "0.1.0"',
                "dependencies = [",
                *dependency_lines,
                "]",
                "",
            ]
        )
    )
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "version = 1\n"
        + "".join(
            f'\n[[package]]\nname = "{item}"\nversion = "1.0.0"\n' for item in locked
        )
    )
    return project, lock


def codes(records) -> list[str]:
    return [record.code for record in records]


def git_repo_with_commit(tmp_path: Path, files: dict[str, bytes | str]) -> Path:
    repo = tmp_path / "repository"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", repo], check=True)
    for name, contents in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(contents, bytes):
            path.write_bytes(contents)
        else:
            path.write_text(contents)
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Contract Test",
        "GIT_AUTHOR_EMAIL": "contract@example.invalid",
        "GIT_COMMITTER_NAME": "Contract Test",
        "GIT_COMMITTER_EMAIL": "contract@example.invalid",
    }
    subprocess.run(
        ["git", "-C", repo, "commit", "-qm", "fixture"],
        check=True,
        env=env,
    )
    return repo


def successful_probe_result(
    profile: RuntimeDependencyProfile, **updates: object
) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": profile.schema_version,
        "profile_name": profile.profile_name,
        "profile_version": profile.profile_version,
        "hosted_python": profile.hosted_python,
        "debugger_python": profile.debugger_python,
        "contract_sha256": profile.contract_sha256,
        "python_version": "3.13.2",
        "failures": [],
    }
    result.update(updates)
    return result


def test_transitive_dependency_does_not_satisfy_direct_projection(tmp_path):
    project, lock = write_project(
        tmp_path,
        outside=("pandas>=2",),
        locked=("pandas", "numpy"),
    )

    violations = check_project_projection(
        profile_for("numpy"), "debugger", project, lock
    )

    assert codes(violations) == ["MISSING_DIRECT_DISTRIBUTION"]


def test_manifest_change_requires_profile_version_change():
    before = manifest_bytes(version="1.0.0", roots=("numpy",))
    after = manifest_bytes(version="1.0.0", roots=("numpy", "pandas"))

    assert codes(check_profile_change(before, after)) == ["PROFILE_VERSION_NOT_BUMPED"]


def test_manifest_change_requires_strictly_greater_semver():
    before = manifest_bytes(version="1.2.0", roots=("numpy",))
    after = manifest_bytes(version="1.1.9", roots=("numpy", "pandas"))

    assert codes(check_profile_change(before, after)) == ["PROFILE_VERSION_NOT_GREATER"]


def test_unchanged_manifest_does_not_require_a_version_bump():
    current = manifest_bytes(version="1.2.0", roots=("numpy",))

    assert check_profile_change(current, current) == ()


def test_first_introduction_at_existing_ref_accepts_only_task_1_bytes(tmp_path):
    repo = git_repo_with_commit(tmp_path, {"README.md": "before\n"})

    result = check_baseline(repo, "HEAD", INITIAL_MANIFEST)

    assert result.state == "introduced"
    assert result.violations == ()
    assert codes(result.notices) == ["BASELINE_MANIFEST_ABSENT"]
    assert len(result.commit) == 40


def test_first_introduction_rejects_any_non_exact_current_bytes(tmp_path):
    repo = git_repo_with_commit(tmp_path, {"README.md": "before\n"})

    result = check_baseline(repo, "HEAD", INITIAL_MANIFEST + b"\n")

    assert result.state == "introduced"
    assert codes(result.violations) == ["INVALID_INITIAL_CONTRACT"]
    assert codes(result.notices) == ["BASELINE_MANIFEST_ABSENT"]


def test_existing_baseline_manifest_checks_monotonic_version(tmp_path):
    baseline = manifest_bytes(version="1.0.0", roots=("numpy",))
    repo = git_repo_with_commit(
        tmp_path,
        {"hushine_strategy/runtime_dependencies.toml": baseline},
    )
    current = manifest_bytes(version="1.1.0", roots=("numpy", "pandas"))

    result = check_baseline(repo, "HEAD", current)

    assert result.state == "present"
    assert result.violations == ()
    assert result.notices == ()


def test_baseline_manifest_comparison_preserves_committed_bytes(tmp_path):
    baseline = manifest_bytes(version="1.0.0", roots=("numpy",)).replace(b"\n", b"\r\n")
    repo = git_repo_with_commit(
        tmp_path,
        {
            ".gitattributes": "*.toml -text\n",
            "hushine_strategy/runtime_dependencies.toml": baseline,
        },
    )
    current = baseline.replace(b"\r\n", b"\n")

    result = check_baseline(repo, "HEAD", current)

    assert result.state == "present"
    assert codes(result.violations) == ["PROFILE_VERSION_NOT_BUMPED"]
    assert result.notices == ()


def test_unresolved_baseline_ref_is_a_configuration_error(tmp_path):
    repo = git_repo_with_commit(tmp_path, {"README.md": "before\n"})

    with pytest.raises(ContractConfigurationError, match="cannot resolve"):
        check_baseline(repo, "does-not-exist", INITIAL_MANIFEST)


def test_baseline_manifest_read_failure_is_cli_configuration_error(
    tmp_path, monkeypatch, capsys
):
    commit = "a" * 40

    def git_result(repository, *arguments):
        command = ["git", "-C", str(repository), *arguments]
        if arguments == ("rev-parse", "--show-toplevel"):
            return subprocess.CompletedProcess(command, 0, f"{tmp_path}\n", "")
        if arguments == ("rev-parse", "--verify", "HEAD^{commit}"):
            return subprocess.CompletedProcess(command, 0, f"{commit}\n", "")
        if arguments == (
            "ls-tree",
            "--full-tree",
            "--name-only",
            commit,
            "--",
            checker.MANIFEST_PATH,
        ):
            return subprocess.CompletedProcess(
                command, 0, f"{checker.MANIFEST_PATH}\n", ""
            )
        raise AssertionError(f"unexpected git invocation: {arguments!r}")

    def git_bytes_result(repository, *arguments):
        command = ["git", "-C", str(repository), *arguments]
        assert arguments == ("show", f"{commit}:{checker.MANIFEST_PATH}")
        return subprocess.CompletedProcess(command, 128, b"", b"object read failed")

    monkeypatch.setattr(checker, "_git", git_result)
    monkeypatch.setattr(checker, "_git_bytes", git_bytes_result)

    status = checker.main(["--baseline-only", "--baseline-ref", "HEAD", "--json"])

    assert status == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "CONFIGURATION_ERROR"
    assert "cannot read baseline manifest" in payload["error"]["message"]


@pytest.mark.parametrize(
    ("tree_status", "tree_stdout"),
    [(128, ""), (0, "unexpected/path.toml\n")],
)
def test_baseline_tree_inspection_must_succeed_and_be_unambiguous(
    tmp_path, monkeypatch, tree_status, tree_stdout
):
    commit = "b" * 40

    def git_result(repository, *arguments):
        command = ["git", "-C", str(repository), *arguments]
        if arguments == ("rev-parse", "--show-toplevel"):
            return subprocess.CompletedProcess(command, 0, f"{tmp_path}\n", "")
        if arguments == ("rev-parse", "--verify", "HEAD^{commit}"):
            return subprocess.CompletedProcess(command, 0, f"{commit}\n", "")
        if arguments == (
            "ls-tree",
            "--full-tree",
            "--name-only",
            commit,
            "--",
            checker.MANIFEST_PATH,
        ):
            return subprocess.CompletedProcess(
                command, tree_status, tree_stdout, "tree inspection failed"
            )
        raise AssertionError(f"unexpected git invocation: {arguments!r}")

    monkeypatch.setattr(checker, "_git", git_result)

    with pytest.raises(
        ContractConfigurationError, match="cannot inspect baseline manifest"
    ):
        check_baseline(tmp_path, "HEAD", INITIAL_MANIFEST)


def test_projection_write_is_manifest_derived_idempotent_and_scoped(tmp_path):
    project, _ = write_project(
        tmp_path,
        outside=("internal-tool>=1",),
    )
    original_prefix = project.read_text().split(BEGIN)[0]

    assert (
        sync_project_projection(profile_for("pandas", "numpy"), project, write=True)
        == ()
    )
    once = project.read_bytes()
    assert (
        sync_project_projection(profile_for("pandas", "numpy"), project, write=True)
        == ()
    )

    assert project.read_bytes() == once
    assert project.read_text().split(BEGIN)[0] == original_prefix
    assert '"internal-tool>=1"' in project.read_text()
    assert project.read_text().index('"numpy"') < project.read_text().index('"pandas"')


def test_check_mode_reports_drift_without_writing(tmp_path):
    project, _ = write_project(
        tmp_path,
        generated=("pandas", "numpy"),
    )
    before = project.read_bytes()

    violations = sync_project_projection(profile_for("numpy", "pandas"), project)

    assert codes(violations) == ["PROJECTION_NOT_GENERATED"]
    assert project.read_bytes() == before


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (("[project]", 'dependencies = ["numpy"]'), "PROJECTION_MARKERS_MISSING"),
        (
            (
                "[project]",
                "dependencies = [",
                BEGIN,
                BEGIN,
                END,
                "]",
            ),
            "PROJECTION_MARKERS_DUPLICATE",
        ),
        (
            (
                "[project]",
                "dependencies = [",
                END,
                '"numpy",',
                BEGIN,
                "]",
            ),
            "PROJECTION_MARKERS_CORRUPT",
        ),
    ],
)
def test_marker_corruption_is_rejected(tmp_path, lines, expected):
    project = tmp_path / "pyproject.toml"
    project.write_text("\n".join(lines) + "\n")

    assert codes(sync_project_projection(profile_for("numpy"), project)) == [expected]


def test_markers_after_project_dependencies_are_rejected_without_writing(tmp_path):
    project = tmp_path / "pyproject.toml"
    project.write_text(
        "\n".join(
            [
                "[project]",
                'name = "fixture"',
                'version = "0.1.0"',
                "dependencies = [",
                "]",
                BEGIN,
                END,
                "[tool.fixture]",
                "items = [",
                "]",
                "",
            ]
        )
    )
    before = project.read_bytes()

    violations = sync_project_projection(profile_for("numpy"), project, write=True)

    assert codes(violations) == ["PROJECTION_MARKERS_CORRUPT"]
    assert project.read_bytes() == before


def test_public_distribution_outside_generated_block_is_rejected(tmp_path):
    project, lock = write_project(
        tmp_path,
        outside=("numpy>=1.26",),
        locked=("numpy",),
    )

    violations = check_project_projection(
        profile_for("numpy"), "service", project, lock
    )

    assert codes(violations) == ["PUBLIC_DISTRIBUTION_OUTSIDE_PROJECTION"]


def test_duplicate_generated_distribution_is_rejected(tmp_path):
    project, lock = write_project(
        tmp_path,
        generated=("numpy", "numpy"),
        locked=("numpy",),
    )

    violations = check_project_projection(
        profile_for("numpy"), "service", project, lock
    )

    assert codes(violations) == ["DUPLICATE_GENERATED_DISTRIBUTION"]


def test_extra_internal_dependency_does_not_become_public(tmp_path):
    project, lock = write_project(
        tmp_path,
        outside=("internal-tool>=1",),
        generated=("numpy",),
        locked=("internal-tool", "numpy"),
    )

    assert (
        check_project_projection(profile_for("numpy"), "service", project, lock) == ()
    )


def test_lock_must_contain_direct_distribution(tmp_path):
    project, lock = write_project(
        tmp_path,
        generated=("numpy",),
        locked=(),
    )

    violations = check_project_projection(
        profile_for("numpy"), "service", project, lock
    )

    assert codes(violations) == ["DISTRIBUTION_NOT_LOCKED"]


def test_malformed_lock_is_rejected(tmp_path):
    project, lock = write_project(
        tmp_path,
        generated=("numpy",),
        locked=("numpy",),
    )
    lock.write_text("not valid = [")

    violations = check_project_projection(
        profile_for("numpy"), "service", project, lock
    )

    assert codes(violations) == ["MALFORMED_LOCK"]


def test_stale_lock_is_detected_by_invoking_uv_lock_check(tmp_path, monkeypatch):
    project, lock = write_project(
        tmp_path,
        generated=("numpy",),
        locked=("numpy",),
    )
    calls = []

    def completed(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 1, "", "stale")

    monkeypatch.setattr(checker, "_run_uv_lock_check", ORIGINAL_RUN_UV_LOCK_CHECK)
    monkeypatch.setattr(checker.subprocess, "run", completed)

    violations = check_project_projection(
        profile_for("numpy"), "service", project, lock
    )

    assert codes(violations) == ["STALE_LOCK"]
    assert calls[0][0] == [
        "uv",
        "lock",
        "--check",
        "--project",
        str(project.parent),
    ]


def test_installed_check_uses_one_target_process_and_sanitized_environment(
    monkeypatch,
):
    profile = profile_for("numpy")
    calls = []
    poisoned = (
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "UV_PROJECT_ENVIRONMENT",
        "DATABASE_URL",
        "POSTGRES_PASSWORD",
        "KAFKA_BROKERS",
        "CORE_SERVICE_ADDR",
        "ORDER_SERVICE_ADDR",
        "AUTH_TOKEN",
        "API_SECRET",
        "LC_TOKEN",
        "HUSHINE_RUNTIME_BUILD_SECRET",
        "HUSHINE_RUNTIME_DEPENDENCY_PROFILE_SHA256",
    )
    for key in poisoned:
        monkeypatch.setenv(key, f"poison-{key}")
    monkeypatch.setenv("PATH", "/safe/path")
    monkeypatch.setenv("HOME", "/safe/home")
    monkeypatch.setenv("LANG", "C.UTF-8")
    build_facts = {
        "HUSHINE_RUNTIME_STRATEGY_SERVICE_COMMIT": "service-commit",
        "HUSHINE_RUNTIME_STRATEGY_LIBRARY_COMMIT": "library-commit",
        "HUSHINE_RUNTIME_IMAGE_BUILD_ID": "image-build",
    }
    for key, value in build_facts.items():
        monkeypatch.setenv(key, value)

    def run_probe(executable, constraint, environment):
        calls.append((executable, constraint, environment))
        return successful_probe_result(profile)

    monkeypatch.setattr(checker, "_run_installed_probe", run_probe)

    assert check_installed_projection(profile, "/target/python", "3.13") == ()
    assert len(calls) == 1
    assert calls[0][0:2] == ("/target/python", "3.13")
    assert {key: calls[0][2][key] for key in ("LANG", "PATH")} == {
        "LANG": "C.UTF-8",
        "PATH": "/safe/path",
    }
    assert all(
        key
        in {
            "LANG",
            "LANGUAGE",
            "LC_ADDRESS",
            "LC_ALL",
            "LC_COLLATE",
            "LC_CTYPE",
            "LC_IDENTIFICATION",
            "LC_MEASUREMENT",
            "LC_MESSAGES",
            "LC_MONETARY",
            "LC_NAME",
            "LC_NUMERIC",
            "LC_PAPER",
            "LC_TELEPHONE",
            "LC_TIME",
            "PATH",
            "SOURCE_DATE_EPOCH",
            "TZ",
        }
        for key in calls[0][2]
    )
    assert not (set(poisoned) | set(build_facts)).intersection(calls[0][2])


def test_installed_check_rejects_missing_target_metadata(monkeypatch):
    profile = profile_for("numpy")
    result = successful_probe_result(profile)
    result.pop("python_version")
    monkeypatch.setattr(checker, "_run_installed_probe", lambda *_: result)

    assert codes(check_installed_projection(profile, "python", "3.13")) == [
        "TARGET_METADATA_MISSING"
    ]


def test_installed_check_rejects_wrong_python_version(monkeypatch):
    profile = profile_for("numpy")
    monkeypatch.setattr(
        checker,
        "_run_installed_probe",
        lambda *_: successful_probe_result(profile, python_version="3.11.9"),
    )

    assert codes(check_installed_projection(profile, "python", "3.13")) == [
        "PYTHON_VERSION_MISMATCH"
    ]


def test_installed_check_rejects_caller_target_profile_disagreement(monkeypatch):
    profile = profile_for("numpy")
    monkeypatch.setattr(
        checker,
        "_run_installed_probe",
        lambda *_: successful_probe_result(
            profile, contract_sha256="target-contract-digest"
        ),
    )

    assert codes(check_installed_projection(profile, "python", "3.13")) == [
        "CALLER_TARGET_PROFILE_MISMATCH"
    ]


def test_installed_check_reports_missing_distribution_metadata(monkeypatch):
    profile = profile_for("numpy")
    failure = {
        "import_root": "numpy",
        "distribution": "numpy",
        "probe": "numpy",
        "reason": "PackageNotFoundError: numpy",
    }
    monkeypatch.setattr(
        checker,
        "_run_installed_probe",
        lambda *_: successful_probe_result(profile, failures=[failure]),
    )

    assert codes(check_installed_projection(profile, "python", "3.13")) == [
        "INSTALLED_METADATA_MISSING"
    ]


def test_installed_check_reports_import_probe_failure(monkeypatch):
    profile = profile_for("numpy")
    failure = {
        "import_root": "numpy",
        "distribution": "numpy",
        "probe": "numpy",
        "reason": "ModuleNotFoundError: numpy",
    }
    monkeypatch.setattr(
        checker,
        "_run_installed_probe",
        lambda *_: successful_probe_result(profile, failures=[failure]),
    )

    assert codes(check_installed_projection(profile, "python", "3.13")) == [
        "INSTALLED_PROBE_FAILED"
    ]


def test_installed_check_turns_target_process_failure_into_violation(monkeypatch):
    profile = profile_for("numpy")

    def fail(*_):
        raise RuntimeError("target failed")

    monkeypatch.setattr(checker, "_run_installed_probe", fail)

    violations = check_installed_projection(profile, "python", "3.13")
    assert codes(violations) == ["TARGET_PROBE_FAILED"]
    assert violations[0].project == "installed-runtime"
    assert violations[0].message == "target dependency probe failed"


def test_installed_check_uses_fixed_logical_target_for_every_violation(monkeypatch):
    profile = profile_for("numpy")
    result = successful_probe_result(profile, python_version="3.11.9")
    result["contract_sha256"] = "wrong"
    monkeypatch.setattr(checker, "_run_installed_probe", lambda *_: result)

    violations = check_installed_projection(
        profile, "/private/interpreter-canary", "3.13"
    )

    assert {item.project for item in violations} == {"installed-runtime"}
    assert "interpreter-canary" not in json.dumps(
        [checker._record_json(item) for item in violations]
    )


def test_configured_interpreter_normalizes_without_resolving_final_symlink(
    monkeypatch, tmp_path
):
    target = tmp_path / "base-python"
    target.write_text("placeholder")
    invocation = tmp_path / "venv" / "bin" / "python"
    invocation.parent.mkdir(parents=True)
    invocation.symlink_to(target)
    monkeypatch.chdir(tmp_path)
    options = SimpleNamespace(
        installed_python=["debugger=venv/bin/../bin/python"],
        installed_python_version=["debugger=>=3.12"],
    )

    configured = checker._configured_interpreters(options, profile_for("numpy"))

    expected = os.path.abspath(os.path.normpath("venv/bin/../bin/python"))
    assert configured == [("debugger", expected, ">=3.12")]
    assert configured[0][1] != os.path.realpath(expected)


def test_initial_contract_digest_is_immutable_task_1_digest():
    assert hashlib.sha256(INITIAL_MANIFEST).hexdigest() == (
        "8457b3c35618558fc8bfc74d4135b7eb52e00c33a8c9a49d202830f3fd5b62c5"
    )


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=SCRIPT.parents[1],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_baseline_only_cli_reports_first_introduction_deterministically():
    arguments = (
        "--baseline-only",
        "--baseline-ref",
        "bd354b4c46dbe3685af6da0aa6c8b809d8c1fe07",
        "--json",
    )

    first = run_cli(*arguments)
    second = run_cli(*arguments)

    assert first.returncode == 0
    assert first.stdout == second.stdout
    payload = json.loads(first.stdout)
    assert payload["ok"] is True
    assert payload["baseline"] == {
        "commit": "bd354b4c46dbe3685af6da0aa6c8b809d8c1fe07",
        "ref": "bd354b4c46dbe3685af6da0aa6c8b809d8c1fe07",
        "state": "introduced",
    }
    assert [item["code"] for item in payload["notices"]] == ["BASELINE_MANIFEST_ABSENT"]
    assert payload["violations"] == []


def test_baseline_only_cli_accepts_a_commit_with_the_manifest():
    result = run_cli(
        "--baseline-only",
        "--baseline-ref",
        "6657c003ba8678c6aaafd01b08893fc2bbe65468",
        "--json",
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["baseline"]["state"] == "present"
    assert payload["notices"] == []
    assert payload["violations"] == []


def test_unresolved_baseline_ref_is_cli_error_exit_2():
    result = run_cli(
        "--baseline-only",
        "--baseline-ref",
        "does-not-exist",
        "--json",
    )

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "CONFIGURATION_ERROR"
    assert "cannot resolve" in payload["error"]["message"]


def test_write_projection_cli_updates_only_the_marked_block(tmp_path):
    project, lock = write_project(
        tmp_path,
        outside=("internal-tool>=1",),
    )

    result = run_cli(
        "--service-project",
        str(project),
        "--service-lock",
        str(lock),
        "--write-projections",
        "--json",
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["checked_projects"] == ["service"]
    assert payload["violations"] == []
    updated = project.read_text()
    assert '"internal-tool>=1"' in updated
    assert [
        distribution
        for distribution in (
            "PyYAML",
            "grpcio",
            "numpy",
            "pandas",
            "protobuf",
            "pydantic",
            "python-dateutil",
            "requests",
        )
        if f'"{distribution}",' in updated
    ] == [
        "PyYAML",
        "grpcio",
        "numpy",
        "pandas",
        "protobuf",
        "pydantic",
        "python-dateutil",
        "requests",
    ]


def test_baseline_only_rejects_product_paths():
    result = run_cli(
        "--baseline-only",
        "--baseline-ref",
        "HEAD",
        "--service-project",
        "pyproject.toml",
        "--service-lock",
        "uv.lock",
        "--json",
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "CONFIGURATION_ERROR"


def test_write_and_baseline_only_are_mutually_exclusive():
    result = run_cli(
        "--write-projections",
        "--baseline-only",
        "--baseline-ref",
        "HEAD",
        "--json",
    )

    assert result.returncode == 2


def test_installed_python_requires_a_matching_version_entry(capsys):
    status = checker.main(
        [
            "--installed-python",
            f"debugger={sys.executable}",
            "--json",
        ]
    )

    assert status == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "CONFIGURATION_ERROR"


def test_cli_pairs_installed_interpreter_name_path_and_constraint(monkeypatch, capsys):
    calls = []

    def installed(profile, executable, constraint):
        calls.append((profile.profile_name, executable, constraint))
        return ()

    monkeypatch.setattr(checker, "check_installed_projection", installed)

    status = checker.main(
        [
            "--installed-python",
            "debugger=/runtime/python",
            "--installed-python-version",
            "debugger=>=3.12",
            "--json",
        ]
    )

    assert status == 0
    assert calls == [("platform-python-3.13", "/runtime/python", ">=3.12")]
    payload = json.loads(capsys.readouterr().out)
    assert payload["checked_interpreters"] == [
        {
            "expected_python": ">=3.12",
            "name": "debugger",
            "path": "/runtime/python",
        }
    ]


def test_cli_contract_violation_exits_1_and_sorts_json(monkeypatch, capsys):
    violations = (
        checker.ContractViolation("Z_LAST", "z"),
        checker.ContractViolation("A_FIRST", "a"),
    )
    monkeypatch.setattr(checker, "check_installed_projection", lambda *_: violations)

    status = checker.main(
        [
            "--installed-python",
            "service=/runtime/python",
            "--installed-python-version",
            "service=3.13",
            "--json",
        ]
    )

    assert status == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert [item["code"] for item in payload["violations"]] == [
        "A_FIRST",
        "Z_LAST",
    ]
