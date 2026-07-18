from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
import importlib.machinery
import inspect
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

import hushine_runtime_import_probe.protocol as protocol
import hushine_runtime_import_probe.transport as transport
from hushine_runtime_import_probe import (
    ExpectedProfile,
    ImportRecord,
    collect_import_records,
    probe_import_records,
)
from hushine_strategy.import_validation import validate_dependency_imports
from hushine_strategy.runtime_dependencies import load_runtime_dependency_profile

PROFILE = load_runtime_dependency_profile()
EXPECTED_PROFILE = ExpectedProfile(
    name=PROFILE.profile_name,
    version=PROFILE.profile_version,
    contract_sha256=PROFILE.contract_sha256,
)
PYTHON = os.path.abspath(os.path.normpath(sys.executable))
ImportProbeProtocolError = protocol.ImportProbeProtocolError
decode_import_request = protocol.decode_import_request
decode_import_response = protocol.decode_import_response
encode_import_request = protocol.encode_import_request


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _FakeWindowsKernel32:
    def __init__(
        self,
        *,
        create_result: int = 101,
        configure_result: int = 1,
        assign_result: int = 1,
        close_result: int = 1,
        snapshot_result: int = 202,
        first_thread_result: int = 1,
        thread_owner_pid: int = 404,
        thread_id: int = 505,
        open_thread_result: int = 606,
        resume_result: int = 1,
        terminate_result: int = 1,
    ):
        self.create_result = create_result
        self.configure_result = configure_result
        self.assign_result = assign_result
        self.close_result = close_result
        self.snapshot_result = snapshot_result
        self.first_thread_result = first_thread_result
        self.thread_owner_pid = thread_owner_pid
        self.thread_id = thread_id
        self.open_thread_result = open_thread_result
        self.resume_result = resume_result
        self.terminate_result = terminate_result
        self.calls = []

    def CreateJobObjectW(self, security, name):
        self.calls.append(("create", security, name))
        return self.create_result

    def SetInformationJobObject(self, handle, info_class, raw_info, size):
        self.calls.append(
            (
                "configure",
                handle,
                info_class,
                raw_info._obj.BasicLimitInformation.LimitFlags,
                size,
            )
        )
        return self.configure_result

    def AssignProcessToJobObject(self, handle, process_handle):
        self.calls.append(("assign", handle, process_handle))
        return self.assign_result

    def CreateToolhelp32Snapshot(self, flags, process_id):
        self.calls.append(("snapshot", flags, process_id))
        return self.snapshot_result

    def Thread32First(self, snapshot, raw_entry):
        self.calls.append(("thread-first", snapshot))
        if self.first_thread_result:
            raw_entry._obj.th32OwnerProcessID = self.thread_owner_pid
            raw_entry._obj.th32ThreadID = self.thread_id
        return self.first_thread_result

    def Thread32Next(self, snapshot, _raw_entry):
        self.calls.append(("thread-next", snapshot))
        return 0

    def OpenThread(self, access, inherit, thread_id):
        self.calls.append(("thread-open", access, inherit, thread_id))
        return self.open_thread_result

    def ResumeThread(self, handle):
        self.calls.append(("thread-resume", handle))
        return self.resume_result

    def TerminateJobObject(self, handle, exit_code):
        self.calls.append(("terminate-job", handle, exit_code))
        return self.terminate_result

    def CloseHandle(self, handle):
        self.calls.append(("close", handle))
        return self.close_result


def _import(module: str = "numpy", *, line: int = 1, column: int = 0) -> ImportRecord:
    return ImportRecord(
        kind="import",
        module=module,
        names=(),
        lineno=line,
        col_offset=column,
    )


def _from(
    module: str = "requests",
    names: tuple[tuple[str, str | None], ...] = (("get", None),),
    *,
    line: int = 1,
    column: int = 0,
) -> ImportRecord:
    return ImportRecord(
        kind="from",
        module=module,
        names=tuple(
            protocol.ImportName(name=name, asname=asname) for name, asname in names
        ),
        lineno=line,
        col_offset=column,
    )


def _record_json(record: ImportRecord) -> dict[str, object]:
    value: dict[str, object] = {
        "kind": record.kind,
        "module": record.module,
        "lineno": record.lineno,
        "col_offset": record.col_offset,
    }
    if record.kind == "from":
        value["names"] = [
            {"name": item.name, "asname": item.asname} for item in record.names
        ]
    return value


def _request(
    imports=(_import(),),
    *,
    expected_profile: ExpectedProfile = EXPECTED_PROFILE,
    extra_python_path: tuple[str, ...] = (),
) -> bytes:
    return encode_import_request(
        expected_profile=expected_profile,
        imports=imports,
        extra_python_path=extra_python_path,
    )


def _response_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "ok": True,
        "profile_name": PROFILE.profile_name,
        "profile_version": PROFILE.profile_version,
        "contract_sha256": PROFILE.contract_sha256,
        "requested_module": "",
        "static_found": True,
        "exception_kind": "none",
        "exception_class": "",
        "missing_name": "",
    }
    payload.update(updates)
    return payload


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _decode_response(
    payload: dict[str, object],
    *,
    exit_code: int,
    request: bytes | None = None,
):
    return decode_import_response(
        _canonical(payload),
        b"",
        exit_code,
        request_bytes=request or _request(),
    )


class TestImportCollection:
    def test_plain_aliases_become_independent_records_and_ignore_local_alias(self):
        records = collect_import_records(
            ast.parse("import numpy as np, requests.sessions as sessions")
        )

        assert records == (
            _import("numpy"),
            _import("requests.sessions"),
        )

    def test_from_record_preserves_one_statement_order_aliases_and_star(self):
        records = collect_import_records(
            ast.parse("from requests import get as fetch, post\nfrom pandas import *\n")
        )

        assert records == (
            _from("requests", (("get", "fetch"), ("post", None))),
            _from("pandas", (("*", None),), line=2),
        )

    def test_collector_preserves_lexical_location_and_nested_source_order(self):
        records = collect_import_records(
            ast.parse(
                "if True:\n"
                "    import requests\n"
                "import numpy\n"
                "def later():\n"
                "    from pandas import DataFrame\n"
            )
        )

        assert records == (
            _import("requests", line=2, column=4),
            _import("numpy", line=3),
            _from("pandas", (("DataFrame", None),), line=5, column=4),
        )

    def test_collector_deduplicates_exact_records_at_first_location_only(self):
        records = collect_import_records(
            ast.parse(
                "import numpy\n"
                "import numpy as np\n"
                "from requests import get\n"
                "from requests import get\n"
            )
        )

        assert records == (
            _import("numpy", line=1),
            _from("requests", (("get", None),), line=3),
        )

    def test_collector_does_not_merge_import_from_or_different_alias_lists(self):
        records = collect_import_records(
            ast.parse(
                "import requests\n"
                "from requests import get\n"
                "from requests import get as fetch\n"
                "from requests import get, post\n"
            )
        )

        assert records == (
            _import("requests", line=1),
            _from("requests", (("get", None),), line=2),
            _from("requests", (("get", "fetch"),), line=3),
            _from("requests", (("get", None), ("post", None)), line=4),
        )

    @pytest.mark.parametrize(
        "source",
        ["from . import x", "from .requests import get", "from .. import x"],
    )
    def test_relative_import_is_rejected_before_transport(self, source):
        with pytest.raises(ImportProbeProtocolError, match="invalid import request"):
            collect_import_records(ast.parse(source))

    def test_collector_rejects_more_than_128_unique_records(self):
        source = "\n".join(f"import root_{index}" for index in range(129))

        with pytest.raises(ImportProbeProtocolError, match="invalid import request"):
            collect_import_records(ast.parse(source))

    @pytest.mark.parametrize(
        ("lineno", "col_offset"),
        [
            (True, 0),
            (1.0, 0),
            ("1", 0),
            (1_048_577, 0),
            (1, True),
            (1, 0.0),
            (1, "0"),
            (1, 1_048_577),
        ],
    )
    def test_manual_ast_requires_exact_bounded_integer_locations(
        self, lineno, col_offset
    ):
        node = ast.Import(names=[ast.alias(name="numpy")])
        node.lineno = lineno
        node.col_offset = col_offset
        tree = ast.Module(body=[node], type_ignores=[])

        with pytest.raises(ImportProbeProtocolError, match="invalid import request"):
            collect_import_records(tree)

    @pytest.mark.parametrize("field", ["module", "name", "asname"])
    def test_manual_ast_requires_exact_string_fields(self, field):
        class DerivedStr(str):
            pass

        if field == "module":
            node = ast.ImportFrom(
                module=DerivedStr("requests"),
                names=[ast.alias(name="get")],
                level=0,
            )
        elif field == "name":
            node = ast.Import(names=[ast.alias(name=DerivedStr("numpy"))])
        else:
            node = ast.ImportFrom(
                module="requests",
                names=[ast.alias(name="get", asname=DerivedStr("fetch"))],
                level=0,
            )
        node.lineno = 1
        node.col_offset = 0
        tree = ast.Module(body=[node], type_ignores=[])

        with pytest.raises(ImportProbeProtocolError, match="invalid import request"):
            collect_import_records(tree)


class TestRequestProtocol:
    def test_neutral_value_objects_are_frozen_and_slotted(self):
        profile = ExpectedProfile("profile", "1.0.0", "a" * 64)
        name = protocol.ImportName("get", "fetch")
        record = _from("requests", (("get", "fetch"),))
        result = protocol.ImportProbeResult(True, "", "", "profile", "1.0.0", "a" * 64)

        assert not hasattr(profile, "__dict__")
        assert not hasattr(name, "__dict__")
        assert not hasattr(record, "__dict__")
        assert not hasattr(result, "__dict__")
        with pytest.raises(FrozenInstanceError):
            profile.name = "changed"
        with pytest.raises(FrozenInstanceError):
            name.name = "post"
        with pytest.raises(FrozenInstanceError):
            record.module = "pandas"
        with pytest.raises(FrozenInstanceError):
            result.ok = False

    @pytest.mark.parametrize(
        "updates",
        [
            {"ok": 1},
            {"ok": "true"},
            {"code": None},
            {"code": []},
            {"code": ""},
            {"code": "UNKNOWN"},
            {"requested_module": 1},
            {"requested_module": "requests..sessions"},
            {"requested_module": "import"},
            {"requested_module": "x" * 513},
            {"profile_name": None},
            {"profile_name": ""},
            {"profile_name": "x" * 129},
            {"profile_version": 1},
            {"profile_version": ""},
            {"profile_version": "x" * 129},
            {"contract_sha256": None},
            {"contract_sha256": "A" * 64},
            {"contract_sha256": "a" * 63},
        ],
    )
    def test_result_direct_construction_rejects_invalid_failure_values(self, updates):
        values = {
            "ok": False,
            "code": "STRATEGY_IMPORT_FAILED",
            "requested_module": "requests.sessions",
            "profile_name": "profile",
            "profile_version": "1.0.0",
            "contract_sha256": "a" * 64,
        }
        values.update(updates)

        with pytest.raises(ValueError, match=r"^invalid import probe result$"):
            protocol.ImportProbeResult(**values)

    @pytest.mark.parametrize(
        "updates",
        [
            {"code": "STRATEGY_IMPORT_FAILED"},
            {"code": "STRATEGY_DEPENDENCY_UNAVAILABLE"},
            {"requested_module": "requests"},
            {"requested_module": []},
        ],
    )
    def test_result_success_requires_exact_success_triple(self, updates):
        values = {
            "ok": True,
            "code": "",
            "requested_module": "",
            "profile_name": "profile",
            "profile_version": "1.0.0",
            "contract_sha256": "a" * 64,
        }
        values.update(updates)

        with pytest.raises(ValueError, match=r"^invalid import probe result$"):
            protocol.ImportProbeResult(**values)

    def test_unavailable_result_requires_a_correlated_requested_module(self):
        with pytest.raises(ValueError, match=r"^invalid import probe result$"):
            protocol.ImportProbeResult(
                False,
                "STRATEGY_DEPENDENCY_UNAVAILABLE",
                "",
                "profile",
                "1.0.0",
                "a" * 64,
            )

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("code", "STRATEGY_IMPORT_FAILED"),
            ("requested_module", "requests"),
            ("profile_name", "profile"),
            ("profile_version", "1.0.0"),
            ("contract_sha256", "a" * 64),
        ],
    )
    def test_result_direct_construction_rejects_string_subclasses(self, field, value):
        class DerivedStr(str):
            pass

        values = {
            "ok": False,
            "code": "STRATEGY_IMPORT_FAILED",
            "requested_module": "requests",
            "profile_name": "profile",
            "profile_version": "1.0.0",
            "contract_sha256": "a" * 64,
        }
        values[field] = DerivedStr(value)

        with pytest.raises(ValueError, match=r"^invalid import probe result$"):
            protocol.ImportProbeResult(**values)

    @pytest.mark.parametrize(
        ("code", "requested_module"),
        [
            ("STRATEGY_IMPORT_FAILED", ""),
            ("STRATEGY_IMPORT_FAILED", "requests.sessions"),
            ("STRATEGY_IMPORT_FAILED", "请求.模块"),
            ("STRATEGY_DEPENDENCY_UNAVAILABLE", "requests.sessions"),
            ("STRATEGY_DEPENDENCY_UNAVAILABLE", "请求.模块"),
        ],
    )
    def test_result_direct_construction_accepts_stable_failure_shapes(
        self, code, requested_module
    ):
        result = protocol.ImportProbeResult(
            False,
            code,
            requested_module,
            "profile",
            "1.0.0",
            "a" * 64,
        )

        assert result.code == code
        assert result.requested_module == requested_module

    def test_package_root_exports_only_the_five_stable_neutral_symbols(self):
        import hushine_runtime_import_probe as package

        assert package.__all__ == [
            "ExpectedProfile",
            "ImportRecord",
            "ImportProbeResult",
            "collect_import_records",
            "probe_import_records",
        ]
        assert not hasattr(package, "ImportName")
        assert not hasattr(package, "ImportProbeProtocolError")
        assert not hasattr(package, "encode_import_request")

    def test_codec_and_public_probe_reject_raw_mapping_records(self, monkeypatch):
        raw = {
            "kind": "import",
            "module": "numpy",
            "lineno": 1,
            "col_offset": 0,
        }
        monkeypatch.setattr(
            protocol,
            "run_probe",
            lambda *_args, **_kwargs: pytest.fail("raw mapping reached transport"),
        )

        with pytest.raises(ImportProbeProtocolError, match="invalid import request"):
            encode_import_request(expected_profile=EXPECTED_PROFILE, imports=(raw,))
        with pytest.raises(ImportProbeProtocolError, match="invalid import request"):
            probe_import_records(
                (raw,),
                python_invocation_path=PYTHON,
                expected_profile=EXPECTED_PROFILE,
            )

    @pytest.mark.parametrize(
        ("kind", "names"),
        [
            ("import", []),
            ("from", [protocol.ImportName("get", None)]),
            ("from", None),
        ],
    )
    def test_import_record_rejects_mutable_names_at_construction(self, kind, names):
        with pytest.raises(ValueError, match=r"^invalid import record$"):
            ImportRecord(kind, "requests", names, 1, 0)

    def test_public_codec_rejects_nonexact_import_name(self):
        class DerivedImportName(protocol.ImportName):
            pass

        with pytest.raises(ValueError, match=r"^invalid import record$"):
            ImportRecord(
                "from",
                "requests",
                (DerivedImportName("get", None),),
                1,
                0,
            )

    def test_neutral_value_constructors_require_exact_fields_and_shapes(self):
        class DerivedStr(str):
            pass

        class DerivedTuple(tuple):
            pass

        variants = (
            (
                lambda: ExpectedProfile(DerivedStr("profile"), "1.0.0", "a" * 64),
                "invalid expected profile",
            ),
            (
                lambda: ExpectedProfile("profile", DerivedStr("1.0.0"), "a" * 64),
                "invalid expected profile",
            ),
            (
                lambda: ExpectedProfile("profile", "1.0.0", DerivedStr("a" * 64)),
                "invalid expected profile",
            ),
            (
                lambda: protocol.ImportName(DerivedStr("get"), None),
                "invalid import name",
            ),
            (
                lambda: protocol.ImportName("get", DerivedStr("fetch")),
                "invalid import name",
            ),
            (
                lambda: ImportRecord(DerivedStr("import"), "numpy", (), 1, 0),
                "invalid import record",
            ),
            (
                lambda: ImportRecord("import", DerivedStr("numpy"), (), 1, 0),
                "invalid import record",
            ),
            (
                lambda: ImportRecord(
                    "from",
                    "requests",
                    DerivedTuple((protocol.ImportName("get", None),)),
                    1,
                    0,
                ),
                "invalid import record",
            ),
            (
                lambda: ImportRecord("import", "numpy", (), True, 0),
                "invalid import record",
            ),
            (
                lambda: ImportRecord("import", "numpy", (), 1, False),
                "invalid import record",
            ),
        )

        for factory, message in variants:
            with pytest.raises(ValueError, match=rf"^{message}$"):
                factory()

    @pytest.mark.parametrize(
        ("factory", "message"),
        [
            (
                lambda: ExpectedProfile("", "1.0.0", "a" * 64),
                "invalid expected profile",
            ),
            (
                lambda: ExpectedProfile("profile", "", "a" * 64),
                "invalid expected profile",
            ),
            (
                lambda: ExpectedProfile("profile", "1.0.0", "A" * 64),
                "invalid expected profile",
            ),
            (lambda: protocol.ImportName("", None), "invalid import name"),
            (lambda: protocol.ImportName("*", "alias"), "invalid import name"),
            (
                lambda: ImportRecord(
                    "import", "numpy", (protocol.ImportName("x", None),), 1, 0
                ),
                "invalid import record",
            ),
            (
                lambda: ImportRecord("from", "requests", (), 1, 0),
                "invalid import record",
            ),
            (
                lambda: ImportRecord("unknown", "numpy", (), 1, 0),
                "invalid import record",
            ),
        ],
    )
    def test_neutral_value_constructors_reject_invalid_semantics(
        self, factory, message
    ):
        with pytest.raises(ValueError, match=rf"^{message}$"):
            factory()

    def test_private_codec_accepts_profile_mapping_but_public_clients_reject_it(
        self, monkeypatch, tmp_path
    ):
        raw_profile = {
            "name": EXPECTED_PROFILE.name,
            "version": EXPECTED_PROFILE.version,
            "contract_sha256": EXPECTED_PROFILE.contract_sha256,
        }
        encoded = encode_import_request(expected_profile=raw_profile, imports=())
        assert decode_import_request(encoded)["expected_profile"] == raw_profile
        monkeypatch.setattr(
            protocol,
            "run_probe",
            lambda *_args, **_kwargs: pytest.fail("raw profile reached transport"),
        )

        for client in (
            lambda: probe_import_records(
                (),
                python_invocation_path=PYTHON,
                expected_profile=raw_profile,
            ),
            lambda: protocol._probe_import_records_for_test(
                (),
                python_invocation_path=PYTHON,
                expected_profile=raw_profile,
                extra_python_path=(str(tmp_path),),
            ),
        ):
            with pytest.raises(
                ImportProbeProtocolError, match=r"^invalid import request$"
            ):
                client()

    def test_public_clients_reject_expected_profile_subclasses(
        self, monkeypatch, tmp_path
    ):
        class DerivedProfile(ExpectedProfile):
            pass

        derived = DerivedProfile(
            EXPECTED_PROFILE.name,
            EXPECTED_PROFILE.version,
            EXPECTED_PROFILE.contract_sha256,
        )
        monkeypatch.setattr(
            protocol,
            "run_probe",
            lambda *_args, **_kwargs: pytest.fail("profile subclass reached transport"),
        )

        for client in (
            lambda: probe_import_records(
                (),
                python_invocation_path=PYTHON,
                expected_profile=derived,
            ),
            lambda: protocol._probe_import_records_for_test(
                (),
                python_invocation_path=PYTHON,
                expected_profile=derived,
                extra_python_path=(str(tmp_path),),
            ),
        ):
            with pytest.raises(
                ImportProbeProtocolError, match=r"^invalid import request$"
            ):
                client()

    @pytest.mark.parametrize("container_type", [list, tuple])
    def test_public_probe_rejects_import_container_subclasses_before_hooks_or_transport(
        self, monkeypatch, container_type
    ):
        hook_calls = []

        class DerivedImports(container_type):
            def __len__(self):
                hook_calls.append("len")
                return super().__len__()

            def __iter__(self):
                hook_calls.append("iter")
                return super().__iter__()

        imports = DerivedImports([_import()])
        monkeypatch.setattr(
            protocol,
            "run_probe",
            lambda *_args, **_kwargs: pytest.fail(
                "container subclass reached transport"
            ),
        )

        with pytest.raises(ImportProbeProtocolError, match=r"^invalid import request$"):
            probe_import_records(
                imports,
                python_invocation_path=PYTHON,
                expected_profile=EXPECTED_PROFILE,
            )

        assert hook_calls == []

    @pytest.mark.parametrize("client_name", ["public", "test_seam"])
    def test_probe_clients_reject_exact_list_imports_before_transport(
        self, monkeypatch, tmp_path, client_name
    ):
        monkeypatch.setattr(
            protocol,
            "run_probe",
            lambda *_args, **_kwargs: pytest.fail("exact list reached transport"),
        )

        def client():
            if client_name == "public":
                return probe_import_records(
                    [_import()],
                    python_invocation_path=PYTHON,
                    expected_profile=EXPECTED_PROFILE,
                )
            return protocol._probe_import_records_for_test(
                [_import()],
                python_invocation_path=PYTHON,
                expected_profile=EXPECTED_PROFILE,
                extra_python_path=(str(tmp_path),),
            )

        with pytest.raises(ImportProbeProtocolError, match=r"^invalid import request$"):
            client()

    @pytest.mark.parametrize("case", ["exact_list", "tuple_subclass", "str_subclass"])
    def test_private_probe_rejects_nonexact_extra_paths_before_hooks_or_transport(
        self, monkeypatch, tmp_path, case
    ):
        hook_calls = []

        class DerivedPaths(tuple):
            def __len__(self):
                hook_calls.append("len")
                return super().__len__()

            def __iter__(self):
                hook_calls.append("iter")
                return super().__iter__()

        class DerivedPath(str):
            def encode(self, *_args, **_kwargs):
                hook_calls.append("encode")
                return b"/safe-looking-path"

        path = str(tmp_path)
        variants = {
            "exact_list": [path],
            "tuple_subclass": DerivedPaths((path,)),
            "str_subclass": (DerivedPath(path),),
        }
        monkeypatch.setattr(
            protocol,
            "run_probe",
            lambda *_args, **_kwargs: pytest.fail(
                "invalid extra path reached transport"
            ),
        )

        with pytest.raises(ImportProbeProtocolError, match=r"^invalid import request$"):
            protocol._probe_import_records_for_test(
                (),
                python_invocation_path=PYTHON,
                expected_profile=EXPECTED_PROFILE,
                extra_python_path=variants[case],
            )

        assert hook_calls == []

    def test_canonical_request_has_exact_shapes_ascii_and_one_lf(self, tmp_path):
        imports = (
            _import("numpy", line=2, column=4),
            _from("requests", (("get", "fetch"), ("post", None)), line=3),
        )
        encoded = _request(imports, extra_python_path=(str(tmp_path),))

        assert encoded.endswith(b"\n")
        assert not encoded.endswith(b"\n\n")
        assert len(encoded) <= protocol.MAX_PROTOCOL_BYTES
        assert encoded == _canonical(
            {
                "schema_version": 1,
                "expected_profile": {
                    "name": PROFILE.profile_name,
                    "version": PROFILE.profile_version,
                    "contract_sha256": PROFILE.contract_sha256,
                },
                "imports": [
                    _record_json(_import("numpy", line=2, column=4)),
                    _record_json(
                        _from(
                            "requests",
                            (("get", "fetch"), ("post", None)),
                            line=3,
                        )
                    ),
                ],
                "extra_python_path": [str(tmp_path)],
            }
        )
        assert decode_import_request(encoded)["imports"][1]["names"][0] == {
            "name": "get",
            "asname": "fetch",
        }

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda value: value.update(extra=True),
            lambda value: value.pop("imports"),
            lambda value: value.update(schema_version=True),
            lambda value: value.update(schema_version=2),
            lambda value: value.update(imports="numpy"),
            lambda value: value.update(extra_python_path="/tmp"),
            lambda value: value["expected_profile"].update(extra=True),
            lambda value: value["expected_profile"].pop("version"),
        ],
    )
    def test_request_rejects_top_level_and_profile_shape_bypasses(self, mutate):
        value = json.loads(_request())
        mutate(value)

        with pytest.raises(ImportProbeProtocolError, match="invalid import request"):
            decode_import_request(_canonical(value))

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("name", ""),
            ("name", "é" * 65),
            ("version", "v" * 129),
            ("contract_sha256", "A" * 64),
            ("contract_sha256", "a" * 63),
            ("contract_sha256", "g" * 64),
        ],
    )
    def test_request_rejects_invalid_or_oversized_profile_facts(self, field, value):
        body = json.loads(_request())
        body["expected_profile"][field] = value

        with pytest.raises(ImportProbeProtocolError, match="invalid import request"):
            decode_import_request(_canonical(body))

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda record: record.update(extra=True),
            lambda record: record.pop("module"),
            lambda record: record.update(kind="unknown"),
            lambda record: record.update(module=""),
            lambda record: record.update(module=".numpy"),
            lambda record: record.update(module="numpy..core"),
            lambda record: record.update(module="numpy-core"),
            lambda record: record.update(module="class"),
            lambda record: record.update(module="numpy.class"),
            lambda record: record.update(module="é" * 257),
            lambda record: record.update(lineno=True),
            lambda record: record.update(lineno=0),
            lambda record: record.update(col_offset=True),
            lambda record: record.update(col_offset=-1),
            lambda record: record.update(lineno=1_048_577),
            lambda record: record.update(col_offset=1_048_577),
        ],
    )
    def test_request_rejects_invalid_import_record(self, mutate):
        body = json.loads(_request())
        mutate(body["imports"][0])

        with pytest.raises(ImportProbeProtocolError, match="invalid import request"):
            decode_import_request(_canonical(body))

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda record: record.update(extra=True),
            lambda record: record.pop("names"),
            lambda record: record.update(names=[]),
            lambda record: record.update(names=[{"name": "get"}]),
            lambda record: record.update(
                names=[{"name": "get", "asname": None, "extra": True}]
            ),
            lambda record: record.update(names=[{"name": "", "asname": None}]),
            lambda record: record.update(names=[{"name": "a.b", "asname": None}]),
            lambda record: record.update(names=[{"name": "class", "asname": None}]),
            lambda record: record.update(names=[{"name": "*", "asname": "all"}]),
            lambda record: record.update(names=[{"name": "get", "asname": ""}]),
            lambda record: record.update(names=[{"name": "get", "asname": "for"}]),
            lambda record: record.update(names=[{"name": "n" * 257, "asname": None}]),
            lambda record: record.update(names=[{"name": "get", "asname": "a" * 257}]),
            lambda record: record.update(names=[{"name": "get", "asname": None}] * 129),
        ],
    )
    def test_request_rejects_invalid_from_record(self, mutate):
        body = json.loads(_request((_from(),)))
        mutate(body["imports"][0])

        with pytest.raises(ImportProbeProtocolError, match="invalid import request"):
            decode_import_request(_canonical(body))

    def test_request_limits_import_count_and_total_utf8_bytes(self):
        assert (
            len(_request(tuple(_import(f"root_{index}") for index in range(128))))
            <= protocol.MAX_PROTOCOL_BYTES
        )
        with pytest.raises(ImportProbeProtocolError, match="invalid import request"):
            encode_import_request(
                expected_profile=EXPECTED_PROFILE,
                imports=tuple(_import(f"root_{index}") for index in range(129)),
            )
        with pytest.raises(ImportProbeProtocolError, match="invalid import request"):
            decode_import_request(b"{" + b" " * protocol.MAX_PROTOCOL_BYTES + b"}\n")

    def test_unicode_identifiers_and_exact_location_upper_bound_are_valid(self):
        encoded = _request(
            (
                _import("数据.指标", line=1_048_576, column=1_048_576),
                _from("数据", (("指标", "别名"),), line=2),
            )
        )

        decoded = decode_import_request(encoded)
        assert decoded["imports"][0]["module"] == "数据.指标"
        assert decoded["imports"][1]["names"] == [{"name": "指标", "asname": "别名"}]

    @pytest.mark.parametrize(
        "paths",
        [
            ("relative",),
            ("/tmp/nul\0path",),
            tuple(f"/tmp/{index}" for index in range(9)),
            ("/" + "a" * 1024,),
        ],
    )
    def test_request_rejects_invalid_test_only_python_paths(self, paths):
        with pytest.raises(ImportProbeProtocolError, match="invalid import request"):
            _request(extra_python_path=paths)

    def test_request_rejects_duplicate_keys_noncanonical_and_trailing_bytes(self):
        canonical = _request()
        duplicate = canonical.replace(b'"imports":', b'"imports":[],"imports":', 1)
        variants = (
            duplicate,
            json.dumps(json.loads(canonical), indent=2).encode() + b"\n",
            canonical[:-1] + b"\r\n",
            canonical + b"\n",
            b"\xef\xbb\xbf" + canonical,
        )

        for body in variants:
            with pytest.raises(
                ImportProbeProtocolError, match="invalid import request"
            ):
                decode_import_request(body)


class TestResponseProtocol:
    def test_success_response_requires_verified_profile_and_empty_failure_fields(self):
        result = _decode_response(_response_payload(), exit_code=0)

        assert result.ok is True
        assert result.code == ""
        assert result.requested_module == ""
        assert result.profile_name == PROFILE.profile_name
        assert result.profile_version == PROFILE.profile_version
        assert result.contract_sha256 == PROFILE.contract_sha256

    @pytest.mark.parametrize(
        ("static_found", "missing_name", "expected_code"),
        [
            (False, "google.cloud", "STRATEGY_DEPENDENCY_UNAVAILABLE"),
            (False, "google", "STRATEGY_DEPENDENCY_UNAVAILABLE"),
            (False, "private_transitive", "STRATEGY_IMPORT_FAILED"),
            (True, "google.cloud", "STRATEGY_IMPORT_FAILED"),
        ],
    )
    def test_module_not_found_classification_uses_static_fact_and_path_relation(
        self, static_found, missing_name, expected_code
    ):
        request = _request((_import("google.cloud"),))
        result = _decode_response(
            _response_payload(
                ok=False,
                requested_module="google.cloud",
                static_found=static_found,
                exception_kind="module_not_found",
                exception_class="ModuleNotFoundError",
                missing_name=missing_name,
            ),
            exit_code=10,
            request=request,
        )

        assert result.code == expected_code
        assert result.requested_module == "google.cloud"
        if missing_name == "private_transitive":
            assert missing_name not in str(result)

    @pytest.mark.parametrize("kind", ["import_error", "other"])
    def test_other_import_exceptions_are_import_failed(self, kind):
        result = _decode_response(
            _response_payload(
                ok=False,
                requested_module="numpy",
                static_found=True,
                exception_kind=kind,
                exception_class="RuntimeError",
            ),
            exit_code=10,
        )
        assert result.code == "STRATEGY_IMPORT_FAILED"

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda value: value.update(extra=True),
            lambda value: value.pop("profile_version"),
            lambda value: value.update(schema_version=True),
            lambda value: value.update(ok=1),
            lambda value: value.update(static_found=1),
            lambda value: value.update(profile_name="wrong"),
            lambda value: value.update(profile_version="wrong"),
            lambda value: value.update(contract_sha256="0" * 64),
            lambda value: value.update(requested_module="unknown.request"),
            lambda value: value.update(requested_module="é" * 257),
            lambda value: value.update(missing_name="é" * 257),
            lambda value: value.update(exception_class="E" * 129),
            lambda value: value.update(exception_kind="traceback"),
        ],
    )
    def test_response_rejects_shape_bounds_profile_and_correlation_bypasses(
        self, mutate
    ):
        value = _response_payload(
            ok=False,
            requested_module="numpy",
            static_found=True,
            exception_kind="other",
            exception_class="RuntimeError",
        )
        mutate(value)

        with pytest.raises(
            ImportProbeProtocolError, match="invalid import probe response"
        ):
            _decode_response(value, exit_code=10)

    @pytest.mark.parametrize(
        ("payload", "exit_code"),
        [
            (_response_payload(), 10),
            (_response_payload(ok=False), 0),
            (
                _response_payload(
                    ok=False,
                    requested_module="numpy",
                    exception_kind="none",
                    exception_class="RuntimeError",
                ),
                10,
            ),
            (
                _response_payload(
                    requested_module="numpy",
                    exception_kind="other",
                    exception_class="RuntimeError",
                ),
                0,
            ),
            (_response_payload(static_found=False), 0),
        ],
    )
    def test_response_rejects_inconsistent_exit_and_status(self, payload, exit_code):
        with pytest.raises(
            ImportProbeProtocolError, match="invalid import probe response"
        ):
            _decode_response(payload, exit_code=exit_code)

    def test_response_rejects_duplicate_noncanonical_trailing_stderr_and_internal_exit(
        self,
    ):
        canonical = _canonical(_response_payload())
        variants = (
            (canonical.replace(b'"ok":', b'"ok":false,"ok":', 1), b"", 0),
            (json.dumps(json.loads(canonical), indent=2).encode() + b"\n", b"", 0),
            (canonical + b"\n", b"", 0),
            (canonical, b"stderr-canary", 0),
            (canonical, b"", 64),
            (canonical, b"", 70),
        )

        for stdout, stderr, exit_code in variants:
            with pytest.raises(
                ImportProbeProtocolError, match="invalid import probe response"
            ) as caught:
                decode_import_response(
                    stdout, stderr, exit_code, request_bytes=_request()
                )
            assert "canary" not in str(caught.value)

    def test_response_parser_converts_deep_json_to_fixed_protocol_error(self):
        nested = (b"[" * 30_000) + (b"]" * 30_000) + b"\n"

        with pytest.raises(
            ImportProbeProtocolError, match="invalid import probe response"
        ) as caught:
            decode_import_response(nested, b"", 0, request_bytes=_request())

        assert caught.value.__cause__ is None

    def test_response_parser_never_exposes_invalid_request_codec_category(self):
        with pytest.raises(
            ImportProbeProtocolError, match="invalid import probe response"
        ) as caught:
            decode_import_response(
                _canonical(_response_payload()),
                b"",
                0,
                request_bytes=b"{}\n",
            )

        assert str(caught.value) == "invalid import probe response"


class TestTransportPolicies:
    def test_profile_and_import_policies_are_distinct_exact_immutable_sets(self):
        assert transport.PROFILE_PROBE_ENV_KEYS == frozenset(
            {
                "PATH",
                "SOURCE_DATE_EPOCH",
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
                "TZ",
                "SYSTEMROOT",
                "WINDIR",
            }
        )
        assert transport.IMPORT_PROBE_ENV_KEYS == frozenset(
            {"LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT", "WINDIR"}
        )
        assert transport.PROFILE_PROBE_ENV_KEYS is not transport.IMPORT_PROBE_ENV_KEYS

    def test_import_policy_drops_every_poisoned_or_sensitive_environment_value(self):
        source = {
            "LANG": "C.UTF-8",
            "LC_ALL": "C",
            "LC_CTYPE": "C.UTF-8",
            "TZ": "UTC",
            "PATH": "path-canary",
            "COMSPEC": "comspec-canary",
            "PATHEXT": "pathext-canary",
            "HOME": "home-canary",
            "USERPROFILE": "profile-canary",
            "TMP": "tmp-canary",
            "PYTHONPATH": "python-canary",
            "VIRTUAL_ENV": "venv-canary",
            "UV_CACHE_DIR": "uv-canary",
            "DATABASE_URL": "db-canary",
            "KAFKA_BROKERS": "kafka-canary",
            "CORE_SERVICE_ADDR": "core-canary",
            "ORDER_SERVICE_ADDR": "order-canary",
            "CONTROL_PANEL_ADDR": "control-canary",
            "VENUE_API_SECRET": "venue-canary",
            "AUTH_TOKEN": "auth-canary",
            "AWS_SECRET_ACCESS_KEY": "cloud-canary",
            "HTTPS_PROXY": "proxy-canary",
        }

        assert transport.sanitize_probe_environment(
            transport.IMPORT_PROBE_POLICY, source, windows=False
        ) == {
            "LANG": "C.UTF-8",
            "LC_ALL": "C",
            "LC_CTYPE": "C.UTF-8",
            "TZ": "UTC",
        }

    def test_windows_policy_is_case_insensitive_and_rejects_collisions(self):
        source = {
            "lAnG": "C.UTF-8",
            "systemRoot": r"C:\Windows",
            "windir": r"C:\Windows",
            "Path": "path-canary",
        }
        assert transport.sanitize_probe_environment(
            transport.IMPORT_PROBE_POLICY, source, windows=True
        ) == {
            "LANG": "C.UTF-8",
            "SYSTEMROOT": r"C:\Windows",
            "WINDIR": r"C:\Windows",
        }
        with pytest.raises(ValueError, match="invalid probe environment"):
            transport.sanitize_probe_environment(
                transport.IMPORT_PROBE_POLICY,
                {"LANG": "one", "Lang": "two"},
                windows=True,
            )

    def test_environment_policy_ignores_string_subclasses_without_calling_overrides(
        self,
    ):
        class ReclassifiedKey(str):
            def upper(self):
                return "LANG"

        class DerivedValue(str):
            pass

        assert transport.sanitize_probe_environment(
            transport.IMPORT_PROBE_POLICY,
            {
                ReclassifiedKey("AUTH_TOKEN"): "secret-canary",
                "LANG": DerivedValue("malicious-locale"),
                "LC_ALL": "C.UTF-8",
            },
            windows=True,
        ) == {"LC_ALL": "C.UTF-8"}

    def test_argv_rejects_string_subclasses_before_overridable_encoding(self):
        class UnderreportedArg(str):
            def encode(self, *_args, **_kwargs):
                return b"x"

        assert (
            transport.valid_probe_argv(
                [PYTHON, UnderreportedArg("x" * transport.PROBE_ARGV_LIMIT)]
            )
            is False
        )
        assert transport.valid_probe_argv([UnderreportedArg(PYTHON), "-I"]) is False

    @pytest.mark.parametrize("container_type", [list, tuple])
    def test_run_probe_rejects_command_container_subclasses_before_hooks_or_actions(
        self, monkeypatch, container_type
    ):
        hook_calls = []

        class SwitchingCommand(container_type):
            def __len__(self):
                hook_calls.append("len")
                return super().__len__()

            def __getitem__(self, index):
                hook_calls.append("getitem")
                return super().__getitem__(index)

            def __iter__(self):
                hook_calls.append("iter")
                return super().__iter__()

        command = SwitchingCommand([PYTHON, "-I", "-c", "pass"])
        monkeypatch.setattr(
            transport,
            "create_private_probe_root",
            lambda *_args, **_kwargs: pytest.fail("private root was created"),
        )
        monkeypatch.setattr(
            transport.subprocess,
            "Popen",
            lambda *_args, **_kwargs: pytest.fail("process was launched"),
        )

        with pytest.raises(ValueError, match=r"^invalid target Python invocation$"):
            transport.run_probe(
                command,
                environment_policy=transport.IMPORT_PROBE_POLICY,
                timeout_seconds=1,
            )

        assert hook_calls == []

    def test_unknown_or_union_environment_policy_is_rejected_before_launch(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            transport.subprocess,
            "Popen",
            lambda *_args, **_kwargs: pytest.fail("unknown policy launched"),
        )
        for policy in (
            "unknown",
            transport.PROFILE_PROBE_ENV_KEYS | transport.IMPORT_PROBE_ENV_KEYS,
        ):
            with pytest.raises(ValueError, match="invalid probe environment policy"):
                transport.run_probe(
                    [PYTHON, "-I", "-c", "pass"],
                    environment_policy=policy,
                    timeout_seconds=1,
                )

    def test_private_root_security_floor_is_shared(self):
        assert (
            transport.secure_private_directories_supported(
                platform_name="nt", version_info=(3, 12, 3)
            )
            is False
        )
        assert (
            transport.secure_private_directories_supported(
                platform_name="nt", version_info=(3, 12, 4)
            )
            is True
        )
        assert (
            transport.secure_private_directories_supported(
                platform_name="posix", version_info=(3, 12, 0)
            )
            is True
        )

    @pytest.mark.parametrize(
        "timeout",
        [
            True,
            False,
            None,
            "1",
            float("nan"),
            float("inf"),
            -float("inf"),
            0,
            -1,
            transport.PROBE_MAX_TIMEOUT_SECONDS + 0.1,
        ],
    )
    def test_timeout_is_rejected_before_directory_creation_or_launch(
        self, monkeypatch, timeout
    ):
        monkeypatch.setattr(
            transport,
            "create_private_probe_root",
            lambda *_args, **_kwargs: pytest.fail("private root was created"),
        )
        monkeypatch.setattr(
            transport.subprocess,
            "Popen",
            lambda *_args, **_kwargs: pytest.fail("process was launched"),
        )

        with pytest.raises(ValueError, match="invalid probe timeout"):
            transport.run_probe(
                [PYTHON, "-I", "-c", "pass"],
                environment_policy=transport.IMPORT_PROBE_POLICY,
                timeout_seconds=timeout,
            )

    @pytest.mark.parametrize(
        "timeout", [1, 1.5, transport.PROBE_MAX_TIMEOUT_SECONDS]
    )
    def test_finite_positive_timeout_through_import_hard_limit_is_accepted(
        self, monkeypatch, timeout
    ):
        real_run = transport.run_probe
        calls = TestTransportLifecycle._install_helper_popen(monkeypatch, "pass\n")

        result = real_run(
            [PYTHON, "-I", "-c", "pass"],
            environment_policy=transport.IMPORT_PROBE_POLICY,
            timeout_seconds=timeout,
        )

        assert result.returncode == 0
        assert calls[0]["process"].poll() is not None

    def test_profile_probe_has_a_separate_bounded_cold_start_budget(
        self, monkeypatch
    ):
        calls = TestTransportLifecycle._install_helper_popen(monkeypatch, "pass\n")

        assert transport.PROFILE_PROBE_TIMEOUT_SECONDS >= 90
        result = transport.run_probe(
            [PYTHON, "-I", "-c", "pass"],
            environment_policy=transport.PROFILE_PROBE_POLICY,
            timeout_seconds=transport.PROFILE_PROBE_TIMEOUT_SECONDS,
        )

        assert result.returncode == 0
        assert calls[0]["process"].poll() is not None
        with pytest.raises(ValueError, match="invalid probe timeout"):
            transport.run_probe(
                [PYTHON, "-I", "-c", "pass"],
                environment_policy=transport.IMPORT_PROBE_POLICY,
                timeout_seconds=transport.PROFILE_PROBE_TIMEOUT_SECONDS,
            )

    def test_profile_probe_rejects_timeout_above_its_hard_limit(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            transport,
            "create_private_probe_root",
            lambda *_args, **_kwargs: pytest.fail("private root was created"),
        )

        with pytest.raises(ValueError, match="invalid probe timeout"):
            transport.run_probe(
                [PYTHON, "-I", "-c", "pass"],
                environment_policy=transport.PROFILE_PROBE_POLICY,
                timeout_seconds=transport.PROFILE_PROBE_MAX_TIMEOUT_SECONDS + 0.1,
            )


class TestTransportLifecycle:
    @staticmethod
    def _install_helper_popen(monkeypatch, helper_source: str):
        real_popen = subprocess.Popen
        calls = []

        def helper_popen(argv, **kwargs):
            call = {
                "argv": list(argv),
                "argv_object": argv,
                "kwargs": kwargs,
                "process": None,
            }
            calls.append(call)
            process = real_popen([sys.executable, "-c", helper_source], **kwargs)
            call["process"] = process
            return process

        monkeypatch.setattr(transport.subprocess, "Popen", helper_popen)
        return calls

    @pytest.mark.parametrize("container_type", [list, tuple])
    def test_valid_command_is_frozen_before_popen(self, monkeypatch, container_type):
        command = container_type([PYTHON, "-I", "-c", "pass"])
        calls = self._install_helper_popen(monkeypatch, "pass\n")

        result = transport.run_probe(
            command,
            environment_policy=transport.IMPORT_PROBE_POLICY,
            timeout_seconds=1,
        )

        assert result.returncode == 0
        assert type(calls[0]["argv_object"]) is tuple
        assert calls[0]["argv_object"] == tuple(command)
        if container_type is list:
            assert calls[0]["argv_object"] is not command
        else:
            assert calls[0]["argv_object"] is command

    def test_request_writer_sends_exact_bytes_and_private_environment(
        self, monkeypatch
    ):
        payload = b"request-payload"
        calls = self._install_helper_popen(
            monkeypatch,
            "import os,sys\ndata=sys.stdin.buffer.read()\nos.write(1,data)\n",
        )

        result = transport.run_probe(
            [PYTHON, "-I", "-m", "fake", "_probe-imports"],
            environment_policy=transport.IMPORT_PROBE_POLICY,
            source_environment={"LANG": "C.UTF-8", "PATH": "poison"},
            stdin_bytes=payload,
            timeout_seconds=2,
        )

        assert result.stdout == payload
        assert result.stderr == b""
        call = calls[0]
        assert call["argv"] == [PYTHON, "-I", "-m", "fake", "_probe-imports"]
        assert call["kwargs"]["shell"] is False
        assert call["kwargs"]["stdin"] is subprocess.PIPE
        assert call["kwargs"]["stdout"] is subprocess.PIPE
        assert call["kwargs"]["stderr"] is subprocess.PIPE
        assert call["kwargs"]["close_fds"] is True
        assert call["kwargs"]["bufsize"] == 0
        private_root = Path(call["kwargs"]["cwd"]).parent
        environment = call["kwargs"]["env"]
        assert environment["LANG"] == "C.UTF-8"
        assert "PATH" not in environment
        assert "HOME" not in environment
        assert "USERPROFILE" not in environment
        assert environment["TEMP"] == str(private_root / "tmp")
        assert environment["TMP"] == str(private_root / "tmp")
        assert environment["TMPDIR"] == str(private_root / "tmp")
        assert not private_root.exists()

    def test_process_creation_options_are_portable_and_containing(self):
        assert transport._process_containment_popen_kwargs("posix") == {
            "start_new_session": True
        }
        assert transport._process_containment_popen_kwargs("nt") == {
            "creationflags": transport._WINDOWS_CREATE_NEW_PROCESS_GROUP
            | transport._WINDOWS_CREATE_SUSPENDED
        }

    def test_windows_job_object_is_configured_assigned_and_closed_portably(self):
        kernel32 = _FakeWindowsKernel32()

        handle = transport._create_windows_job_object(303, kernel32=kernel32)

        assert handle == 101
        assert [call[:2] for call in kernel32.calls] == [
            ("create", None),
            ("configure", 101),
            ("assign", 101),
        ]
        assert kernel32.calls[-1] == ("assign", 101, 303)
        assert kernel32.calls[1] == (
            "configure",
            101,
            transport._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            transport._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            transport.ctypes.sizeof(transport._JobObjectExtendedLimitInformation),
        )
        assert transport._close_windows_job_object(handle, kernel32=kernel32) is True
        assert kernel32.calls[-1] == ("close", 101)

    @pytest.mark.parametrize(
        ("updates", "expected_calls"),
        [
            ({"create_result": 0}, ["create"]),
            ({"configure_result": 0}, ["create", "configure", "close"]),
            (
                {"assign_result": 0},
                ["create", "configure", "assign", "close"],
            ),
        ],
    )
    def test_windows_job_setup_and_assignment_fail_closed_portably(
        self, updates, expected_calls
    ):
        kernel32 = _FakeWindowsKernel32(**updates)

        with pytest.raises(
            RuntimeError, match=r"^process tree containment unavailable$"
        ):
            transport._create_windows_job_object(303, kernel32=kernel32)

        assert [call[0] for call in kernel32.calls] == expected_calls

    def test_windows_job_close_failure_is_reported_portably(self):
        kernel32 = _FakeWindowsKernel32(close_result=0)
        assert transport._close_windows_job_object(101, kernel32=kernel32) is False
        assert kernel32.calls == [("close", 101)]

    def test_windows_containment_persistent_close_failure_terminates_job_before_retry(
        self,
    ):
        kernel32 = _FakeWindowsKernel32(close_result=0)
        owner = transport._ProcessContainment(
            platform_name="nt",
            windows_job_handle=101,
            windows_kernel32=kernel32,
        )

        assert transport._close_process_containment(owner) is False
        assert kernel32.calls == [
            ("close", 101),
            ("terminate-job", 101, 1),
            ("close", 101),
        ]
        assert owner.closed is False

    def test_windows_job_is_assigned_before_suspended_primary_thread_resumes(self):
        class FakeProcess:
            pid = 404
            _handle = 303

        kernel32 = _FakeWindowsKernel32()

        owner = transport._establish_process_containment(
            FakeProcess(),
            platform_name="nt",
            kernel32=kernel32,
        )

        assert owner.windows_job_handle == 101
        actions = [call[0] for call in kernel32.calls]
        assert actions.index("assign") < actions.index("thread-resume")
        assert actions == [
            "create",
            "configure",
            "assign",
            "snapshot",
            "thread-first",
            "thread-open",
            "thread-resume",
            "close",
            "close",
        ]
        assert transport._close_process_containment(owner) is True

    @pytest.mark.parametrize(
        "updates",
        [
            {"snapshot_result": transport._WINDOWS_INVALID_HANDLE_VALUE},
            {"first_thread_result": 0},
            {"open_thread_result": 0},
            {"resume_result": transport._WINDOWS_RESUME_FAILED},
            {"resume_result": 2},
        ],
    )
    def test_windows_suspended_launch_resume_failure_closes_job_and_fails_closed(
        self, updates
    ):
        class FakeProcess:
            pid = 404
            _handle = 303

        kernel32 = _FakeWindowsKernel32(**updates)

        with pytest.raises(
            RuntimeError, match=r"^process tree containment unavailable$"
        ):
            transport._establish_process_containment(
                FakeProcess(),
                platform_name="nt",
                kernel32=kernel32,
            )

        assert ("close", 101) in kernel32.calls

    def test_containment_setup_failure_reaps_child_and_uses_fixed_error(
        self, monkeypatch
    ):
        calls = self._install_helper_popen(
            monkeypatch,
            "import time\ntime.sleep(60)\n",
        )
        monkeypatch.setattr(
            transport,
            "_establish_process_containment",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("containment-setup-canary")
            ),
        )

        with pytest.raises(transport.ProbeTransportError) as caught:
            transport.run_probe(
                [PYTHON, "-I", "-c", "pass"],
                environment_policy=transport.IMPORT_PROBE_POLICY,
                timeout_seconds=1,
            )

        assert caught.value.kind == "invalid"
        assert "canary" not in str(caught.value)
        assert calls[0]["process"].poll() is not None
        assert not Path(calls[0]["kwargs"]["cwd"]).parent.exists()

    def test_containment_close_failure_runs_all_cleanup_and_fails_closed(
        self, monkeypatch
    ):
        calls = self._install_helper_popen(monkeypatch, "pass\n")
        owner = object()
        close_calls = []
        monkeypatch.setattr(
            transport,
            "_establish_process_containment",
            lambda *_args, **_kwargs: owner,
        )
        monkeypatch.setattr(
            transport,
            "_close_process_containment",
            lambda selected: close_calls.append(selected) or False,
        )

        with pytest.raises(transport.ProbeTransportError) as caught:
            transport.run_probe(
                [PYTHON, "-I", "-c", "pass"],
                environment_policy=transport.IMPORT_PROBE_POLICY,
                timeout_seconds=1,
            )

        assert caught.value.kind == "invalid"
        assert close_calls and all(selected is owner for selected in close_calls)
        assert calls[0]["process"].poll() is not None
        assert not Path(calls[0]["kwargs"]["cwd"]).parent.exists()

    def test_zero_stdin_uses_devnull_and_profile_private_home(self, monkeypatch):
        calls = self._install_helper_popen(monkeypatch, "pass\n")

        transport.run_probe(
            [PYTHON, "-I", "-c", "pass"],
            environment_policy=transport.PROFILE_PROBE_POLICY,
            stdin_bytes=None,
            timeout_seconds=2,
        )

        call = calls[0]
        private_root = Path(call["kwargs"]["cwd"]).parent
        assert call["kwargs"]["stdin"] is subprocess.DEVNULL
        assert call["kwargs"]["env"]["HOME"] == str(private_root / "home")
        assert not private_root.exists()

    def test_all_reader_and_writer_threads_are_non_daemon(self, monkeypatch):
        calls = self._install_helper_popen(
            monkeypatch,
            "import sys\nsys.stdin.buffer.read()\n",
        )
        real_thread = threading.Thread
        created = []

        def recording_thread(*args, **kwargs):
            created.append((kwargs["name"], kwargs.get("daemon")))
            return real_thread(*args, **kwargs)

        monkeypatch.setattr(transport.threading, "Thread", recording_thread)

        result = transport.run_probe(
            [PYTHON, "-I", "-c", "pass"],
            environment_policy=transport.IMPORT_PROBE_POLICY,
            stdin_bytes=b"{}\n",
            timeout_seconds=2,
        )

        assert result.returncode == 0
        assert created == [
            ("runtime-probe-stdout", False),
            ("runtime-probe-stderr", False),
            ("runtime-probe-stdin", False),
        ]
        assert calls[0]["process"].poll() is not None

    def test_start_then_raise_thread_is_registered_stopped_and_joined(
        self, monkeypatch
    ):
        calls = self._install_helper_popen(
            monkeypatch,
            "import time\ntime.sleep(60)\n",
        )
        real_start = threading.Thread.start
        created = []

        def delayed_cooperative_reader(
            _pipe, _buffer, _overflow, _failed, stop, _eof, _deadline
        ):
            stop.wait()
            time.sleep(0.2)

        def start_then_raise(thread):
            created.append(thread)
            real_start(thread)
            raise RuntimeError("start-then-raise-canary")

        monkeypatch.setattr(transport, "_read_bounded_pipe", delayed_cooperative_reader)
        monkeypatch.setattr(threading.Thread, "start", start_then_raise)

        try:
            with pytest.raises(transport.ProbeTransportError) as caught:
                transport.run_probe(
                    [PYTHON, "-I", "-c", "pass"],
                    environment_policy=transport.IMPORT_PROBE_POLICY,
                    timeout_seconds=1,
                    thread_prefix="start-then-raise-probe-",
                )

            assert caught.value.kind == "invalid"
            assert created
            assert not any(thread.is_alive() for thread in created)
            assert calls[0]["process"].poll() is not None
            assert not Path(calls[0]["kwargs"]["cwd"]).parent.exists()
        finally:
            for thread in created:
                thread.join(1)

    def test_never_started_thread_is_registered_and_cleanup_join_is_safe(
        self, monkeypatch
    ):
        calls = self._install_helper_popen(
            monkeypatch,
            "import time\ntime.sleep(60)\n",
        )
        real_join_threads = transport._join_threads
        registered = []

        def start_raises(_thread):
            raise RuntimeError("never-started-canary")

        def recording_join(threads, deadline):
            registered.extend(threads)
            return real_join_threads(threads, deadline)

        monkeypatch.setattr(threading.Thread, "start", start_raises)
        monkeypatch.setattr(transport, "_join_threads", recording_join)

        with pytest.raises(transport.ProbeTransportError) as caught:
            transport.run_probe(
                [PYTHON, "-I", "-c", "pass"],
                environment_policy=transport.IMPORT_PROBE_POLICY,
                timeout_seconds=1,
                thread_prefix="never-started-probe-",
            )

        assert caught.value.kind == "invalid"
        assert any(thread.name == "never-started-probe-stdout" for thread in registered)
        assert calls[0]["process"].poll() is not None
        assert not Path(calls[0]["kwargs"]["cwd"]).parent.exists()

    @pytest.mark.parametrize(
        "failing_phase",
        [
            "_terminate_and_reap",
            "_join_threads",
            "_close_process_pipes",
            "_remove_private_probe_root",
        ],
    )
    def test_cleanup_phase_failure_cannot_escape_or_skip_later_phases(
        self, monkeypatch, failing_phase
    ):
        calls = self._install_helper_popen(
            monkeypatch,
            "import time\ntime.sleep(60)\n",
        )
        phases = [
            "_terminate_and_reap",
            "_join_threads",
            "_close_process_pipes",
            "_remove_private_probe_root",
        ]
        observed = []

        for phase in phases:
            real_phase = getattr(transport, phase)

            def guarded_phase(*args, _name=phase, _real=real_phase, **kwargs):
                observed.append(_name)
                result = _real(*args, **kwargs)
                if _name == failing_phase:
                    raise RuntimeError(f"{_name}-cleanup-canary")
                return result

            monkeypatch.setattr(transport, phase, guarded_phase)

        with pytest.raises(transport.ProbeTransportError) as caught:
            transport.run_probe(
                [PYTHON, "-I", "-c", "pass"],
                environment_policy=transport.IMPORT_PROBE_POLICY,
                timeout_seconds=0.25,
                thread_prefix="cleanup-phase-probe-",
            )

        assert "canary" not in str(caught.value)
        assert all(phase in observed for phase in phases)
        assert calls[0]["process"].poll() is not None
        assert not Path(calls[0]["kwargs"]["cwd"]).parent.exists()
        assert not any(
            thread.name.startswith("cleanup-phase-probe-")
            for thread in threading.enumerate()
        )

    def test_full_64k_stdin_is_deadline_bound(self, monkeypatch):
        calls = self._install_helper_popen(
            monkeypatch,
            "import time\ntime.sleep(60)\n",
        )
        started = time.monotonic()

        with pytest.raises(transport.ProbeTransportError) as caught:
            transport.run_probe(
                [PYTHON, "-I", "-c", "pass"],
                environment_policy=transport.IMPORT_PROBE_POLICY,
                stdin_bytes=b"x" * protocol.MAX_PROTOCOL_BYTES,
                timeout_seconds=0.25,
            )

        assert caught.value.kind == "timeout"
        assert time.monotonic() - started < 1.5
        assert calls[0]["process"].poll() is not None
        assert not Path(calls[0]["kwargs"]["cwd"]).parent.exists()
        assert not any(
            thread.name.startswith("runtime-import-probe-")
            for thread in threading.enumerate()
        )

    def test_both_output_overflow_terminates_reaps_and_cleans(self, monkeypatch):
        calls = self._install_helper_popen(
            monkeypatch,
            "import os,threading,time\n"
            "a=threading.Thread(target=lambda:os.write(1,b'x'*70000))\n"
            "b=threading.Thread(target=lambda:os.write(2,b'y'*70000))\n"
            "a.start();b.start();a.join();b.join();time.sleep(60)\n",
        )

        with pytest.raises(transport.ProbeTransportError) as caught:
            transport.run_probe(
                [PYTHON, "-I", "-c", "pass"],
                environment_policy=transport.IMPORT_PROBE_POLICY,
                stdin_bytes=b"{}\n",
                timeout_seconds=2,
            )

        assert caught.value.kind == "overflow"
        assert calls[0]["process"].poll() is not None
        assert not Path(calls[0]["kwargs"]["cwd"]).parent.exists()

    @pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
    def test_direct_exit_still_terminates_grandchild_holding_both_pipes(
        self, monkeypatch, tmp_path
    ):
        pid_path = tmp_path / "descendant.pid"
        calls = self._install_helper_popen(
            monkeypatch,
            "import os,pathlib,subprocess,sys\n"
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
            f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid))\n"
            "os.write(1,b'payload\\n')\n",
        )
        descendant_pid = None

        try:
            result = transport.run_probe(
                [PYTHON, "-I", "-c", "pass"],
                environment_policy=transport.IMPORT_PROBE_POLICY,
                stdin_bytes=b"{}\n",
                timeout_seconds=1,
            )
            assert result.returncode == 0
            assert result.stdout == b"payload\n"
            assert calls[0]["kwargs"]["start_new_session"] is True
            assert _wait_until(pid_path.exists)
            descendant_pid = int(pid_path.read_text())
            assert _wait_until(lambda: not _pid_exists(descendant_pid))
            assert calls[0]["process"].poll() is not None
            assert not Path(calls[0]["kwargs"]["cwd"]).parent.exists()
        finally:
            if descendant_pid is None and pid_path.exists():
                descendant_pid = int(pid_path.read_text())
            if descendant_pid is not None and _pid_exists(descendant_pid):
                try:
                    os.kill(descendant_pid, signal.SIGKILL)
                except (OSError, ValueError):
                    pass


class TestRealImportChild:
    def test_public_probe_has_no_fixture_python_path_parameter(self):
        public_signature = inspect.signature(probe_import_records)
        private_signature = inspect.signature(protocol._probe_import_records_for_test)
        assert "extra_python_path" not in public_signature.parameters
        assert transport.IMPORT_PROBE_TIMEOUT_SECONDS >= 90
        assert (
            public_signature.parameters["timeout_seconds"].default
            == transport.IMPORT_PROBE_TIMEOUT_SECONDS
        )
        assert (
            private_signature.parameters["timeout_seconds"].default
            == transport.IMPORT_PROBE_TIMEOUT_SECONDS
        )
        assert not hasattr(
            sys.modules["hushine_runtime_import_probe"],
            "_probe_import_records_for_test",
        )

    def test_exact_child_initializes_plain_and_from_import_semantics(self):
        imports = collect_import_records(
            ast.parse(
                "import numpy\n"
                "from requests import get as fetch, post\n"
                "from requests.packages import urllib3\n"
                "from pandas import *\n"
            )
        )

        result = probe_import_records(
            imports,
            python_invocation_path=PYTHON,
            expected_profile=EXPECTED_PROFILE,
        )

        assert result.ok is True
        assert result.code == ""

    def test_empty_import_request_is_a_real_child_noop_success(self):
        result = probe_import_records(
            (),
            python_invocation_path=PYTHON,
            expected_profile=EXPECTED_PROFILE,
        )

        assert result.ok is True
        assert result.code == ""
        assert result.requested_module == ""

    @pytest.mark.skipif(
        os.name != "posix" or not hasattr(os, "fork"),
        reason="requires POSIX fork inheritance",
    )
    def test_fork_inherited_protocol_descriptor_is_contained_after_direct_exit(
        self, tmp_path
    ):
        package = tmp_path / "requests"
        package.mkdir()
        pid_path = tmp_path / "fork-descendant.pid"
        package.joinpath("__init__.py").write_text(
            "import os, pathlib, time\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            f"    pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()))\n"
            "    time.sleep(60)\n"
            "    os._exit(0)\n"
        )
        descendant_pid = None

        try:
            result = protocol._probe_import_records_for_test(
                (_import("requests"),),
                python_invocation_path=PYTHON,
                expected_profile=EXPECTED_PROFILE,
                timeout_seconds=1,
                extra_python_path=(str(tmp_path),),
            )

            assert result.ok is True
            assert _wait_until(pid_path.exists)
            descendant_pid = int(pid_path.read_text())
            assert _wait_until(lambda: not _pid_exists(descendant_pid))
        finally:
            if descendant_pid is None and pid_path.exists():
                descendant_pid = int(pid_path.read_text())
            if descendant_pid is not None and _pid_exists(descendant_pid):
                try:
                    os.kill(descendant_pid, signal.SIGKILL)
                except OSError:
                    pass

    def test_child_invalid_request_exits_64_without_output(self):
        completed = subprocess.run(
            [
                PYTHON,
                "-I",
                "-m",
                "hushine_runtime_import_probe",
                "_probe-imports",
            ],
            input=b"{}\n",
            capture_output=True,
            check=False,
            timeout=5,
        )

        assert completed.returncode == 64
        assert completed.stdout == b""
        assert completed.stderr == b""

    @pytest.mark.parametrize("module", ["os.path", "requests.packages.urllib3"])
    def test_false_preliminary_lookup_still_executes_successfully(self, module):
        result = probe_import_records(
            (_import(module),),
            python_invocation_path=PYTHON,
            expected_profile=EXPECTED_PROFILE,
        )
        assert result.ok is True

    def test_missing_requested_path_is_unavailable(self):
        result = probe_import_records(
            (_import("google.hushine_missing"),),
            python_invocation_path=PYTHON,
            expected_profile=EXPECTED_PROFILE,
        )

        assert result.ok is False
        assert result.code == "STRATEGY_DEPENDENCY_UNAVAILABLE"
        assert result.requested_module == "google.hushine_missing"

    def test_from_requested_module_drives_exact_child_classification(self):
        unavailable = probe_import_records(
            (_from("google.hushine_missing", (("storage", None),)),),
            python_invocation_path=PYTHON,
            expected_profile=EXPECTED_PROFILE,
        )
        attribute_failure = probe_import_records(
            (_from("google", (("hushine_missing", None),)),),
            python_invocation_path=PYTHON,
            expected_profile=EXPECTED_PROFILE,
        )

        assert (unavailable.code, unavailable.requested_module) == (
            "STRATEGY_DEPENDENCY_UNAVAILABLE",
            "google.hushine_missing",
        )
        assert (attribute_failure.code, attribute_failure.requested_module) == (
            "STRATEGY_IMPORT_FAILED",
            "google",
        )

    def test_static_lookup_exception_is_found_unknown_not_unavailable(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            importlib.machinery.PathFinder,
            "find_spec",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("canary")),
        )

        assert protocol._static_module_found("requests.child") is True

    @pytest.mark.parametrize(
        ("initializer", "expected_code"),
        [
            ("import private_transitive_canary\n", "STRATEGY_IMPORT_FAILED"),
            (
                "raise RuntimeError('initializer-secret-canary')\n",
                "STRATEGY_IMPORT_FAILED",
            ),
            (
                "raise ModuleNotFoundError('self missing', name='requests')\n",
                "STRATEGY_IMPORT_FAILED",
            ),
        ],
    )
    def test_found_requested_package_initialization_failures_are_distinct_and_safe(
        self, tmp_path, initializer, expected_code
    ):
        package = tmp_path / "requests"
        package.mkdir()
        (package / "__init__.py").write_text(initializer)

        result = protocol._probe_import_records_for_test(
            (_import("requests"),),
            python_invocation_path=PYTHON,
            expected_profile=EXPECTED_PROFILE,
            extra_python_path=(str(tmp_path),),
        )

        assert result.code == expected_code
        assert result.requested_module == "requests"
        text = str(result)
        assert "private_transitive_canary" not in text
        assert "initializer-secret-canary" not in text
        assert str(tmp_path) not in text

    def test_import_python_and_native_output_cannot_become_protocol_output(
        self, tmp_path
    ):
        package = tmp_path / "requests"
        package.mkdir()
        (package / "__init__.py").write_text(
            "import os\n"
            "print('python-output-canary')\n"
            "os.write(1,b'native-output-canary')\n"
            "os.write(2,b'native-error-canary')\n"
        )

        result = protocol._probe_import_records_for_test(
            (_import("requests"),),
            python_invocation_path=PYTHON,
            expected_profile=EXPECTED_PROFILE,
            extra_python_path=(str(tmp_path),),
        )

        assert result.ok is True
        assert "canary" not in str(result)

    def test_fixture_python_path_cannot_shadow_installed_profile_loader(self, tmp_path):
        marker = tmp_path / "shadow-profile-loaded"
        fake_strategy = tmp_path / "hushine_strategy"
        fake_strategy.mkdir()
        (fake_strategy / "__init__.py").write_text("")
        (fake_strategy / "runtime_dependencies.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('shadowed')\n"
            "def load_runtime_dependency_profile():\n"
            "    raise RuntimeError('shadow-profile-canary')\n"
        )
        fixture = tmp_path / "requests"
        fixture.mkdir()
        (fixture / "__init__.py").write_text("VALUE = 1\n")

        result = protocol._probe_import_records_for_test(
            (_import("requests"),),
            python_invocation_path=PYTHON,
            expected_profile=EXPECTED_PROFILE,
            extra_python_path=(str(tmp_path),),
        )

        assert result.ok is True
        assert not marker.exists()

    def test_profile_mismatch_is_fixed_environment_failure(self):
        wrong = ExpectedProfile(
            name=EXPECTED_PROFILE.name,
            version=EXPECTED_PROFILE.version,
            contract_sha256="0" * 64,
        )
        result = probe_import_records(
            (_import("numpy"),),
            python_invocation_path=PYTHON,
            expected_profile=wrong,
        )

        assert result.ok is False
        assert result.code == "STRATEGY_IMPORT_FAILED"
        assert result.requested_module == ""
        assert "0" * 64 not in str(result)

    @pytest.mark.parametrize("kind", ["launch", "timeout", "overflow", "invalid"])
    def test_transport_failures_map_to_one_safe_empty_module_result(
        self, monkeypatch, kind
    ):
        monkeypatch.setattr(
            protocol,
            "run_probe",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                transport.ProbeTransportError(kind)
            ),
        )

        result = probe_import_records(
            (_import("numpy"),),
            python_invocation_path=PYTHON,
            expected_profile=EXPECTED_PROFILE,
        )

        assert (result.ok, result.code, result.requested_module) == (
            False,
            "STRATEGY_IMPORT_FAILED",
            "",
        )
        assert kind not in str(result)

    def test_invalid_child_response_maps_to_safe_empty_module_result(self, monkeypatch):
        monkeypatch.setattr(
            protocol,
            "run_probe",
            lambda *_args, **_kwargs: transport.ProbeTransportResult(
                returncode=0,
                stdout=b"response-secret-canary\n",
                stderr=b"",
            ),
        )

        result = probe_import_records(
            (_import("numpy"),),
            python_invocation_path=PYTHON,
            expected_profile=EXPECTED_PROFILE,
        )

        assert (result.code, result.requested_module) == (
            "STRATEGY_IMPORT_FAILED",
            "",
        )
        assert "canary" not in str(result)

    @pytest.mark.parametrize(
        "timeout",
        [
            True,
            "1",
            float("nan"),
            float("inf"),
            0,
            -1,
            transport.PROBE_MAX_TIMEOUT_SECONDS + 0.1,
        ],
    )
    def test_public_client_rejects_invalid_timeout_before_private_root(
        self, monkeypatch, timeout
    ):
        monkeypatch.setattr(
            transport,
            "create_private_probe_root",
            lambda *_args, **_kwargs: pytest.fail("private root was created"),
        )

        with pytest.raises(ValueError, match="invalid probe timeout"):
            probe_import_records(
                (_import("numpy"),),
                python_invocation_path=PYTHON,
                expected_profile=EXPECTED_PROFILE,
                timeout_seconds=timeout,
            )

    def test_internal_probe_package_is_not_a_public_strategy_dependency(self):
        issues = validate_dependency_imports(
            ast.parse("import hushine_runtime_import_probe"),
            profile=PROFILE,
            stdlib_roots=frozenset(sys.stdlib_module_names),
            platform_modules=frozenset({"hushine_strategy"}),
        )
        assert [(issue.code, issue.module) for issue in issues] == [
            ("UNSUPPORTED_STRATEGY_DEPENDENCY", "hushine_runtime_import_probe")
        ]

    def test_final_virtualenv_symlink_is_preserved_in_exact_argv(
        self, monkeypatch, tmp_path
    ):
        invocation = tmp_path / "venv" / "bin" / "python"
        invocation.parent.mkdir(parents=True)
        invocation.symlink_to(sys.executable)
        normalized = os.path.abspath(os.path.normpath(str(invocation)))
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return transport.ProbeTransportResult(
                returncode=0,
                stdout=_canonical(_response_payload()),
                stderr=b"",
            )

        monkeypatch.setattr(protocol, "run_probe", fake_run)

        result = probe_import_records(
            (),
            python_invocation_path=normalized,
            expected_profile=EXPECTED_PROFILE,
        )

        assert result.ok is True
        assert calls[0][0] == [
            normalized,
            "-I",
            "-m",
            "hushine_runtime_import_probe",
            "_probe-imports",
        ]
        assert calls[0][0][0] != os.path.realpath(normalized)
        assert calls[0][1]["environment_policy"] == transport.IMPORT_PROBE_POLICY
        assert calls[0][1]["stdin_bytes"] == _request(())


def test_transport_is_the_only_subprocess_and_private_root_implementation():
    package_root = Path(protocol.__file__).parent
    transport_source = (package_root / "transport.py").read_text()
    protocol_source = (package_root / "protocol.py").read_text()
    runtime_source = Path(
        sys.modules["hushine_strategy.runtime_dependencies"].__file__
    ).read_text()

    assert transport_source.count("subprocess.Popen(") == 1
    assert "subprocess.Popen(" not in protocol_source
    assert "subprocess.Popen(" not in runtime_source
    assert "tempfile.mkdtemp(" not in protocol_source
    assert "tempfile.mkdtemp(" not in runtime_source
    assert "def _read_bounded_pipe" not in protocol_source
    assert "def _read_bounded_pipe" not in runtime_source
    assert "def _terminate_and_reap" not in protocol_source
    assert "def _terminate_and_reap" not in runtime_source
