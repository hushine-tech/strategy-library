from __future__ import annotations

import ast
from dataclasses import dataclass
import json
import keyword
import os
import re
import sys
from typing import Iterable, Literal, Mapping, Sequence

from hushine_runtime_import_probe.transport import (
    IMPORT_PROBE_POLICY,
    ProbeTransportError,
    run_probe,
    valid_probe_argv,
)

SCHEMA_VERSION = 1
MAX_PROTOCOL_BYTES = 64 * 1024
MAX_IMPORT_RECORDS = 128
MAX_FROM_NAMES = 128
MAX_PROFILE_TEXT_BYTES = 128
MAX_MODULE_BYTES = 512
MAX_IMPORTED_NAME_BYTES = 256
MAX_EXTRA_PYTHON_PATHS = 8
MAX_EXTRA_PYTHON_PATH_BYTES = 1024
MAX_SOURCE_LOCATION = 1_048_576
_REQUEST_FIELDS = frozenset(
    {"schema_version", "expected_profile", "imports", "extra_python_path"}
)
_PROFILE_FIELDS = frozenset({"name", "version", "contract_sha256"})
_IMPORT_FIELDS = frozenset({"kind", "module", "lineno", "col_offset"})
_FROM_FIELDS = frozenset({"kind", "module", "names", "lineno", "col_offset"})
_NAME_FIELDS = frozenset({"name", "asname"})
_RESPONSE_FIELDS = frozenset(
    {
        "schema_version",
        "ok",
        "profile_name",
        "profile_version",
        "contract_sha256",
        "requested_module",
        "static_found",
        "exception_kind",
        "exception_class",
        "missing_name",
    }
)
_EXCEPTION_KINDS = frozenset({"none", "module_not_found", "import_error", "other"})
_LOWER_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class ExpectedProfile:
    name: str
    version: str
    contract_sha256: str

    def __post_init__(self) -> None:
        if not (
            _bounded_text(self.name, maximum=MAX_PROFILE_TEXT_BYTES)
            and _bounded_text(self.version, maximum=MAX_PROFILE_TEXT_BYTES)
            and type(self.contract_sha256) is str
            and _LOWER_SHA256.fullmatch(self.contract_sha256) is not None
        ):
            raise ValueError("invalid expected profile")


@dataclass(frozen=True, slots=True)
class ImportName:
    name: str
    asname: str | None

    def __post_init__(self) -> None:
        if not (
            _valid_imported_name(self.name, allow_star=True)
            and (
                self.asname is None
                or _valid_imported_name(self.asname, allow_star=False)
            )
            and (self.name != "*" or self.asname is None)
        ):
            raise ValueError("invalid import name")


@dataclass(frozen=True, slots=True)
class ImportRecord:
    kind: Literal["import", "from"]
    module: str
    names: tuple[ImportName, ...]
    lineno: int
    col_offset: int

    def __post_init__(self) -> None:
        exact_fields = (
            type(self.kind) is str
            and type(self.module) is str
            and type(self.names) is tuple
            and type(self.lineno) is int
            and type(self.col_offset) is int
        )
        if not exact_fields:
            raise ValueError("invalid import record")
        valid_shape = (self.kind == "import" and not self.names) or (
            self.kind == "from"
            and 0 < len(self.names) <= MAX_FROM_NAMES
            and all(type(item) is ImportName for item in self.names)
        )
        if not (
            _valid_module(self.module)
            and 1 <= self.lineno <= MAX_SOURCE_LOCATION
            and 0 <= self.col_offset <= MAX_SOURCE_LOCATION
            and valid_shape
        ):
            raise ValueError("invalid import record")


@dataclass(frozen=True, slots=True)
class ImportProbeResult:
    ok: bool
    code: Literal[
        "",
        "STRATEGY_DEPENDENCY_UNAVAILABLE",
        "STRATEGY_IMPORT_FAILED",
    ]
    requested_module: str
    profile_name: str
    profile_version: str
    contract_sha256: str

    def __post_init__(self) -> None:
        if not (
            type(self.ok) is bool
            and type(self.code) is str
            and type(self.requested_module) is str
            and type(self.profile_name) is str
            and type(self.profile_version) is str
            and type(self.contract_sha256) is str
        ):
            raise ValueError("invalid import probe result")
        valid_shape = (
            self.ok is True and self.code == "" and self.requested_module == ""
        ) or (
            self.ok is False
            and (
                self.code == "STRATEGY_IMPORT_FAILED"
                or (
                    self.code == "STRATEGY_DEPENDENCY_UNAVAILABLE"
                    and bool(self.requested_module)
                )
            )
        )
        if not (
            valid_shape
            and _bounded_text(
                self.requested_module,
                maximum=MAX_MODULE_BYTES,
                allow_empty=True,
            )
            and (not self.requested_module or _valid_module(self.requested_module))
            and _bounded_text(self.profile_name, maximum=MAX_PROFILE_TEXT_BYTES)
            and _bounded_text(self.profile_version, maximum=MAX_PROFILE_TEXT_BYTES)
            and _LOWER_SHA256.fullmatch(self.contract_sha256) is not None
        ):
            raise ValueError("invalid import probe result")

    def __str__(self) -> str:
        if self.ok:
            return "strategy import probe succeeded"
        if self.requested_module:
            return f"{self.code}: {self.requested_module}"
        return self.code


class ImportProbeProtocolError(RuntimeError):
    pass


def _request_error() -> ImportProbeProtocolError:
    return ImportProbeProtocolError("invalid import request")


def _response_error() -> ImportProbeProtocolError:
    return ImportProbeProtocolError("invalid import probe response")


def _utf8_size(value: str) -> int | None:
    try:
        return len(value.encode("utf-8", "strict"))
    except UnicodeError:
        return None


def _bounded_text(
    value: object,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> bool:
    if type(value) is not str or (not allow_empty and not value):
        return False
    size = _utf8_size(value)
    return size is not None and size <= maximum


def _valid_module(value: object) -> bool:
    return _bounded_text(value, maximum=MAX_MODULE_BYTES) and all(
        part.isidentifier() and not keyword.iskeyword(part) for part in value.split(".")
    )


def _valid_imported_name(value: object, *, allow_star: bool) -> bool:
    if not _bounded_text(value, maximum=MAX_IMPORTED_NAME_BYTES):
        return False
    return (allow_star and value == "*") or (
        value.isidentifier() and not keyword.iskeyword(value)
    )


def _exact_mapping(value: object, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != fields:
        raise _request_error()
    return value


def _normalize_profile(value: object) -> dict[str, str]:
    if isinstance(value, ExpectedProfile):
        table: Mapping[str, object] = {
            "name": value.name,
            "version": value.version,
            "contract_sha256": value.contract_sha256,
        }
    else:
        table = _exact_mapping(value, _PROFILE_FIELDS)
    name = table["name"]
    version = table["version"]
    digest = table["contract_sha256"]
    if not _bounded_text(name, maximum=MAX_PROFILE_TEXT_BYTES):
        raise _request_error()
    if not _bounded_text(version, maximum=MAX_PROFILE_TEXT_BYTES):
        raise _request_error()
    if type(digest) is not str or _LOWER_SHA256.fullmatch(digest) is None:
        raise _request_error()
    return {"name": name, "version": version, "contract_sha256": digest}


def _normalize_location(record: Mapping[str, object]) -> tuple[int, int]:
    line = record["lineno"]
    column = record["col_offset"]
    if type(line) is not int or not 1 <= line <= MAX_SOURCE_LOCATION:
        raise _request_error()
    if type(column) is not int or not 0 <= column <= MAX_SOURCE_LOCATION:
        raise _request_error()
    return line, column


def _record_mapping(value: object, *, allow_mapping: bool) -> Mapping[str, object]:
    if type(value) is ImportRecord:
        if type(value.kind) is not str or type(value.names) is not tuple:
            raise _request_error()
        if value.kind == "import":
            if value.names:
                raise _request_error()
            return {
                "kind": value.kind,
                "module": value.module,
                "lineno": value.lineno,
                "col_offset": value.col_offset,
            }
        if value.kind == "from":
            if not value.names or any(
                type(item) is not ImportName for item in value.names
            ):
                raise _request_error()
            return {
                "kind": value.kind,
                "module": value.module,
                "names": tuple(
                    {"name": item.name, "asname": item.asname} for item in value.names
                ),
                "lineno": value.lineno,
                "col_offset": value.col_offset,
            }
        raise _request_error()
    if allow_mapping and isinstance(value, Mapping):
        return value
    raise _request_error()


def _normalize_import_record(
    value: object, *, allow_mapping: bool
) -> dict[str, object]:
    value = _record_mapping(value, allow_mapping=allow_mapping)
    kind = value.get("kind")
    if kind == "import":
        record = _exact_mapping(value, _IMPORT_FIELDS)
        module = record["module"]
        if not _valid_module(module):
            raise _request_error()
        line, column = _normalize_location(record)
        return {
            "kind": "import",
            "module": module,
            "lineno": line,
            "col_offset": column,
        }
    if kind != "from":
        raise _request_error()
    record = _exact_mapping(value, _FROM_FIELDS)
    module = record["module"]
    if not _valid_module(module):
        raise _request_error()
    line, column = _normalize_location(record)
    raw_names = record["names"]
    if (
        not isinstance(raw_names, (list, tuple))
        or not raw_names
        or len(raw_names) > MAX_FROM_NAMES
    ):
        raise _request_error()
    names: list[dict[str, str | None]] = []
    for raw_name in raw_names:
        name_record = _exact_mapping(raw_name, _NAME_FIELDS)
        name = name_record["name"]
        asname = name_record["asname"]
        if not _valid_imported_name(name, allow_star=True):
            raise _request_error()
        if asname is not None and not _valid_imported_name(asname, allow_star=False):
            raise _request_error()
        if name == "*" and asname is not None:
            raise _request_error()
        names.append({"name": name, "asname": asname})
    return {
        "kind": "from",
        "module": module,
        "names": names,
        "lineno": line,
        "col_offset": column,
    }


def _normalize_imports(
    values: object, *, allow_mapping: bool
) -> list[dict[str, object]]:
    if not isinstance(values, (list, tuple)) or len(values) > MAX_IMPORT_RECORDS:
        raise _request_error()
    return [
        _normalize_import_record(value, allow_mapping=allow_mapping) for value in values
    ]


def _normalize_extra_python_path(values: object) -> list[str]:
    if not isinstance(values, (list, tuple)) or len(values) > MAX_EXTRA_PYTHON_PATHS:
        raise _request_error()
    normalized: list[str] = []
    for value in values:
        if (
            not _bounded_text(value, maximum=MAX_EXTRA_PYTHON_PATH_BYTES)
            or "\0" in value
            or not os.path.isabs(value)
        ):
            raise _request_error()
        normalized.append(value)
    return normalized


def collect_import_records(tree: ast.AST) -> tuple[ImportRecord, ...]:
    if not isinstance(tree, ast.AST):
        raise _request_error()
    collected: list[tuple[int, int, int, ImportRecord, tuple[object, ...]]] = []
    sequence = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            line, column = _normalize_location(
                {
                    "lineno": getattr(node, "lineno", None),
                    "col_offset": getattr(node, "col_offset", None),
                }
            )
            for alias in node.names:
                if type(alias.name) is not str:
                    raise _request_error()
                record = ImportRecord(
                    kind="import",
                    module=alias.name,
                    names=(),
                    lineno=line,
                    col_offset=column,
                )
                _normalize_import_record(record, allow_mapping=False)
                collected.append(
                    (
                        line,
                        column,
                        sequence,
                        record,
                        ("import", alias.name),
                    )
                )
                sequence += 1
        elif isinstance(node, ast.ImportFrom):
            if (
                type(node.level) is not int
                or node.level != 0
                or type(node.module) is not str
                or not node.module
                or any(
                    type(alias.name) is not str
                    or (alias.asname is not None and type(alias.asname) is not str)
                    for alias in node.names
                )
            ):
                raise _request_error()
            line, column = _normalize_location(
                {
                    "lineno": getattr(node, "lineno", None),
                    "col_offset": getattr(node, "col_offset", None),
                }
            )
            names = tuple(
                ImportName(name=alias.name, asname=alias.asname) for alias in node.names
            )
            key_names = tuple((alias.name, alias.asname) for alias in node.names)
            record = ImportRecord(
                kind="from",
                module=node.module,
                names=names,
                lineno=line,
                col_offset=column,
            )
            _normalize_import_record(record, allow_mapping=False)
            collected.append(
                (
                    line,
                    column,
                    sequence,
                    record,
                    ("from", node.module, key_names),
                )
            )
            sequence += 1
    records: list[ImportRecord] = []
    seen: set[tuple[object, ...]] = set()
    for _line, _column, _sequence, record, key in sorted(collected):
        if key in seen:
            continue
        seen.add(key)
        records.append(record)
    _normalize_imports(records, allow_mapping=False)
    return tuple(records)


def _canonical_bytes(value: object) -> bytes:
    try:
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
    except Exception:
        raise _request_error() from None
    return encoded


def encode_import_request(
    *,
    expected_profile: ExpectedProfile | Mapping[str, object],
    imports: Sequence[ImportRecord],
    extra_python_path: Sequence[str] = (),
) -> bytes:
    value = {
        "schema_version": SCHEMA_VERSION,
        "expected_profile": _normalize_profile(expected_profile),
        "imports": _normalize_imports(imports, allow_mapping=False),
        "extra_python_path": _normalize_extra_python_path(extra_python_path),
    }
    encoded = _canonical_bytes(value)
    if len(encoded) > MAX_PROTOCOL_BYTES:
        raise _request_error()
    return encoded


def _reject_duplicate_keys(pairs: Iterable[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("invalid JSON constant")


def _decode_canonical(value: bytes, *, response: bool) -> object:
    error = _response_error if response else _request_error
    if not isinstance(value, bytes) or not value or len(value) > MAX_PROTOCOL_BYTES:
        raise error()
    try:
        decoded = json.loads(
            value.decode("utf-8", "strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
        canonical = _canonical_bytes(decoded)
    except Exception:
        raise error() from None
    if canonical != value:
        raise error()
    return decoded


def decode_import_request(value: bytes) -> dict[str, object]:
    decoded = _decode_canonical(value, response=False)
    table = _exact_mapping(decoded, _REQUEST_FIELDS)
    if type(table["schema_version"]) is not int or table["schema_version"] != 1:
        raise _request_error()
    return {
        "schema_version": 1,
        "expected_profile": _normalize_profile(table["expected_profile"]),
        "imports": _normalize_imports(table["imports"], allow_mapping=True),
        "extra_python_path": _normalize_extra_python_path(table["extra_python_path"]),
    }


def _safe_failed_result(expected: Mapping[str, object]) -> ImportProbeResult:
    return ImportProbeResult(
        ok=False,
        code="STRATEGY_IMPORT_FAILED",
        requested_module="",
        profile_name=str(expected["name"]),
        profile_version=str(expected["version"]),
        contract_sha256=str(expected["contract_sha256"]),
    )


def decode_import_response(
    stdout: bytes,
    stderr: bytes,
    returncode: int,
    *,
    request_bytes: bytes,
) -> ImportProbeResult:
    try:
        try:
            request = decode_import_request(request_bytes)
        except ImportProbeProtocolError:
            raise _response_error() from None
        if stderr or returncode not in {0, 10}:
            raise _response_error()
        decoded = _decode_canonical(stdout, response=True)
        if not isinstance(decoded, Mapping) or frozenset(decoded) != _RESPONSE_FIELDS:
            raise _response_error()
        if type(decoded["schema_version"]) is not int or decoded["schema_version"] != 1:
            raise _response_error()
        if type(decoded["ok"]) is not bool or type(decoded["static_found"]) is not bool:
            raise _response_error()
        expected = request["expected_profile"]
        if (
            decoded["profile_name"] != expected["name"]
            or decoded["profile_version"] != expected["version"]
            or decoded["contract_sha256"] != expected["contract_sha256"]
        ):
            raise _response_error()
        if not _bounded_text(
            decoded["profile_name"], maximum=MAX_PROFILE_TEXT_BYTES
        ) or not _bounded_text(
            decoded["profile_version"], maximum=MAX_PROFILE_TEXT_BYTES
        ):
            raise _response_error()
        if (
            type(decoded["contract_sha256"]) is not str
            or _LOWER_SHA256.fullmatch(decoded["contract_sha256"]) is None
        ):
            raise _response_error()
        requested = decoded["requested_module"]
        missing = decoded["missing_name"]
        exception_class = decoded["exception_class"]
        kind = decoded["exception_kind"]
        if not _bounded_text(
            requested, maximum=MAX_MODULE_BYTES, allow_empty=True
        ) or not _bounded_text(missing, maximum=MAX_MODULE_BYTES, allow_empty=True):
            raise _response_error()
        if (
            not _bounded_text(exception_class, maximum=128, allow_empty=True)
            or kind not in _EXCEPTION_KINDS
        ):
            raise _response_error()
        requested_modules = {record["module"] for record in request["imports"]}
        if returncode == 0:
            if not (
                decoded["ok"] is True
                and decoded["static_found"] is True
                and kind == "none"
                and requested == ""
                and missing == ""
                and exception_class == ""
            ):
                raise _response_error()
            code = ""
        else:
            if not (
                decoded["ok"] is False
                and requested in requested_modules
                and requested
                and kind != "none"
                and exception_class
            ):
                raise _response_error()
            code = "STRATEGY_IMPORT_FAILED"
            if (
                kind == "module_not_found"
                and decoded["static_found"] is False
                and (
                    missing == requested
                    or (missing and requested.startswith(f"{missing}."))
                )
            ):
                code = "STRATEGY_DEPENDENCY_UNAVAILABLE"
        return ImportProbeResult(
            ok=decoded["ok"],
            code=code,
            requested_module=requested,
            profile_name=decoded["profile_name"],
            profile_version=decoded["profile_version"],
            contract_sha256=decoded["contract_sha256"],
        )
    except ImportProbeProtocolError:
        raise
    except Exception:
        raise _response_error() from None


def probe_import_records(
    imports: Sequence[ImportRecord],
    *,
    python_invocation_path: str,
    expected_profile: ExpectedProfile,
    timeout_seconds: float = 30.0,
) -> ImportProbeResult:
    return _probe_import_records(
        imports,
        python_invocation_path=python_invocation_path,
        expected_profile=expected_profile,
        timeout_seconds=timeout_seconds,
        extra_python_path=(),
    )


def _probe_import_records_for_test(
    imports: Sequence[ImportRecord],
    *,
    python_invocation_path: str,
    expected_profile: ExpectedProfile,
    timeout_seconds: float = 30.0,
    extra_python_path: Sequence[str] = (),
) -> ImportProbeResult:
    return _probe_import_records(
        imports,
        python_invocation_path=python_invocation_path,
        expected_profile=expected_profile,
        timeout_seconds=timeout_seconds,
        extra_python_path=extra_python_path,
    )


def _probe_import_records(
    imports: Sequence[ImportRecord],
    *,
    python_invocation_path: str,
    expected_profile: ExpectedProfile,
    timeout_seconds: float,
    extra_python_path: Sequence[str],
) -> ImportProbeResult:
    if type(expected_profile) is not ExpectedProfile:
        raise _request_error()
    request = encode_import_request(
        expected_profile=expected_profile,
        imports=imports,
        extra_python_path=extra_python_path,
    )
    command = [
        python_invocation_path,
        "-I",
        "-m",
        "hushine_runtime_import_probe",
        "_probe-imports",
    ]
    expected = _normalize_profile(expected_profile)
    if not valid_probe_argv(command):
        raise ValueError("invalid target Python invocation")
    try:
        completed = run_probe(
            command,
            environment_policy=IMPORT_PROBE_POLICY,
            stdin_bytes=request,
            timeout_seconds=timeout_seconds,
            thread_prefix="runtime-import-probe-",
        )
        return decode_import_response(
            completed.stdout,
            completed.stderr,
            completed.returncode,
            request_bytes=request,
        )
    except (ProbeTransportError, ImportProbeProtocolError):
        return _safe_failed_result(expected)


def _static_module_found(module: str) -> bool:
    from importlib.machinery import BuiltinImporter, FrozenImporter, PathFinder

    search_path = None
    fullname = ""
    try:
        for index, part in enumerate(module.split(".")):
            fullname = part if not fullname else f"{fullname}.{part}"
            spec = BuiltinImporter.find_spec(fullname)
            if spec is None:
                spec = FrozenImporter.find_spec(fullname)
            if spec is None:
                spec = PathFinder.find_spec(fullname, search_path)
            if spec is None:
                return False
            if index < len(module.split(".")) - 1:
                search_path = spec.submodule_search_locations
                if search_path is None:
                    return False
    except Exception:
        return True
    return True


def _response_profile(profile) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "profile_name": profile.profile_name,
        "profile_version": profile.profile_version,
        "contract_sha256": profile.contract_sha256,
    }


def _success_response(profile) -> dict[str, object]:
    return {
        **_response_profile(profile),
        "ok": True,
        "requested_module": "",
        "static_found": True,
        "exception_kind": "none",
        "exception_class": "",
        "missing_name": "",
    }


def _bounded_diagnostic(value: object, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    encoded = value.encode("utf-8", "replace")[:maximum]
    while encoded:
        try:
            return encoded.decode("utf-8", "strict")
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return ""


def _failure_response(
    profile,
    *,
    requested_module: str,
    static_found: bool,
    error: BaseException,
) -> dict[str, object]:
    if isinstance(error, ModuleNotFoundError):
        kind = "module_not_found"
        missing_name = _bounded_diagnostic(error.name, MAX_MODULE_BYTES)
    elif isinstance(error, ImportError):
        kind = "import_error"
        missing_name = ""
    else:
        kind = "other"
        missing_name = ""
    return {
        **_response_profile(profile),
        "ok": False,
        "requested_module": requested_module,
        "static_found": static_found,
        "exception_kind": kind,
        "exception_class": _bounded_diagnostic(type(error).__name__, 128),
        "missing_name": missing_name,
    }


def _execute_import_record(record: Mapping[str, object]) -> None:
    if record["kind"] == "import":
        statement: ast.stmt = ast.Import(
            names=[ast.alias(name=record["module"], asname=None)]
        )
    else:
        statement = ast.ImportFrom(
            module=record["module"],
            names=[
                ast.alias(name=name["name"], asname=name["asname"])
                for name in record["names"]
            ],
            level=0,
        )
    tree = ast.fix_missing_locations(ast.Module(body=[statement], type_ignores=[]))
    code = compile(tree, "<runtime-import-probe>", "exec", dont_inherit=True)
    exec(code, {"__builtins__": __builtins__})


def _write_protocol_bytes(descriptor: int, value: bytes) -> bool:
    offset = 0
    try:
        while offset < len(value):
            written = os.write(descriptor, value[offset:])
            if written <= 0:
                return False
            offset += written
        return True
    except Exception:
        return False


def _child_main(arguments: list[str]) -> int:
    if arguments != ["_probe-imports"]:
        return 64
    protocol_descriptor: int | None = None
    null_descriptor: int | None = None
    try:
        protocol_descriptor = os.dup(1)
        os.set_inheritable(protocol_descriptor, False)
        null_descriptor = os.open(os.devnull, os.O_RDWR)
        os.dup2(null_descriptor, 1)
        os.dup2(null_descriptor, 2)
        os.set_inheritable(1, False)
        os.set_inheritable(2, False)
        request_bytes = sys.stdin.buffer.read(MAX_PROTOCOL_BYTES + 1)
        try:
            request = decode_import_request(request_bytes)
        except ImportProbeProtocolError:
            return 64

        try:
            from hushine_strategy.runtime_dependencies import (
                load_runtime_dependency_profile,
            )

            profile = load_runtime_dependency_profile()
        except BaseException:
            return 70
        expected = request["expected_profile"]
        if (
            profile.profile_name != expected["name"]
            or profile.profile_version != expected["version"]
            or profile.contract_sha256 != expected["contract_sha256"]
        ):
            return 70
        sys.path[0:0] = request["extra_python_path"]

        response: dict[str, object] = _success_response(profile)
        returncode = 0
        for record in request["imports"]:
            module = record["module"]
            static_found = _static_module_found(module)
            try:
                _execute_import_record(record)
            except BaseException as error:
                response = _failure_response(
                    profile,
                    requested_module=module,
                    static_found=static_found,
                    error=error,
                )
                returncode = 10
                break
        encoded = _canonical_bytes(response)
        if len(encoded) > MAX_PROTOCOL_BYTES:
            return 70
        if not _write_protocol_bytes(protocol_descriptor, encoded):
            return 70
        return returncode
    except BaseException:
        return 70
    finally:
        for descriptor in (null_descriptor, protocol_descriptor):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except Exception:
                    pass
