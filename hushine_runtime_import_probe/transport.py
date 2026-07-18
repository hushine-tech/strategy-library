from __future__ import annotations

import ctypes
from dataclasses import dataclass
import errno
import math
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import BinaryIO, Mapping

PROFILE_PROBE_POLICY = "profile"
IMPORT_PROBE_POLICY = "import"
PROFILE_PROBE_ENV_KEYS = frozenset(
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
IMPORT_PROBE_ENV_KEYS = frozenset(
    {"LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT", "WINDIR"}
)
PROBE_ARGV_LIMIT = 8 * 1024
PROBE_PIPE_LIMIT = 64 * 1024
PROFILE_PROBE_TIMEOUT_SECONDS = 120.0
PROFILE_PROBE_MAX_TIMEOUT_SECONDS = 120.0
IMPORT_PROBE_TIMEOUT_SECONDS = 90.0
PROBE_MAX_TIMEOUT_SECONDS = 90.0
PROBE_TERMINATE_GRACE_SECONDS = 1.0
PROBE_PIPE_DRAIN_SECONDS = 0.1
_SOURCE_DATE_EPOCH_MAX_DIGITS = 20
_WINDOWS_ENV_KEYS = frozenset({"SYSTEMROOT", "WINDIR"})
_WINDOWS_SECURE_DIRECTORY_MIN_VERSION = (3, 12, 4)
_WINDOWS_CREATE_NEW_PROCESS_GROUP = getattr(
    subprocess,
    "CREATE_NEW_PROCESS_GROUP",
    0x00000200,
)
_WINDOWS_CREATE_SUSPENDED = getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
_WINDOWS_TH32CS_SNAPTHREAD = 0x00000004
_WINDOWS_THREAD_SUSPEND_RESUME = 0x0002
_WINDOWS_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_WINDOWS_RESUME_FAILED = 0xFFFFFFFF
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ThreadID", ctypes.c_uint32),
        ("th32OwnerProcessID", ctypes.c_uint32),
        ("tpBasePri", ctypes.c_long),
        ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
    ]


@dataclass(frozen=True, slots=True)
class ProbeTransportResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class ProbeTransportError(RuntimeError):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__("probe transport failed")


@dataclass(slots=True)
class _ProcessContainment:
    platform_name: str
    process_group_id: int | None = None
    windows_job_handle: int | None = None
    windows_kernel32: object | None = None
    closed: bool = False


def _environment_keys(policy: object) -> frozenset[str]:
    if policy == PROFILE_PROBE_POLICY and type(policy) is str:
        return PROFILE_PROBE_ENV_KEYS
    if policy == IMPORT_PROBE_POLICY and type(policy) is str:
        return IMPORT_PROBE_ENV_KEYS
    raise ValueError("invalid probe environment policy")


def sanitize_probe_environment(
    policy: object,
    source: Mapping[str, str] | None = None,
    *,
    windows: bool | None = None,
) -> dict[str, str]:
    selected = os.environ if source is None else source
    case_insensitive = os.name == "nt" if windows is None else windows
    allowed = _environment_keys(policy)
    if not case_insensitive:
        allowed = allowed - _WINDOWS_ENV_KEYS

    environment: dict[str, str] = {}
    seen: set[str] = set()
    for raw_key, value in selected.items():
        if type(raw_key) is not str or type(value) is not str:
            continue
        canonical = raw_key.upper() if case_insensitive else raw_key
        if canonical not in allowed:
            continue
        if canonical in seen:
            raise ValueError("invalid probe environment")
        seen.add(canonical)
        if "\0" in value:
            continue
        if canonical == "SOURCE_DATE_EPOCH" and not (
            value
            and len(value) <= _SOURCE_DATE_EPOCH_MAX_DIGITS
            and value.isascii()
            and value.isdigit()
        ):
            continue
        environment[canonical] = value
    return environment


def secure_private_directories_supported(
    *,
    platform_name: str | None = None,
    version_info: tuple[int, int, int] | None = None,
) -> bool:
    selected_platform = os.name if platform_name is None else platform_name
    selected_version = (
        tuple(sys.version_info[:3]) if version_info is None else version_info
    )
    return selected_platform != "nt" or (
        selected_version >= _WINDOWS_SECURE_DIRECTORY_MIN_VERSION
    )


def create_private_probe_root(policy: object) -> Path:
    _environment_keys(policy)
    if not secure_private_directories_supported():
        raise RuntimeError("secure private probe directories are unavailable")
    prefix = (
        "hushine-profile-probe-"
        if policy == PROFILE_PROBE_POLICY
        else "hushine-import-probe-"
    )
    root = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        if os.name != "nt":
            root.chmod(0o700)
        for name in ("cwd", "home", "tmp"):
            directory = root / name
            directory.mkdir(mode=0o700)
            if os.name != "nt":
                directory.chmod(0o700)
    except Exception:
        _remove_private_probe_root(root)
        raise
    return root


def _remove_private_probe_root(root: Path) -> bool:
    def make_removable(function, path, _error):
        try:
            os.chmod(path, 0o700)
            function(path)
        except Exception:
            pass

    for _attempt in range(2):
        try:
            shutil.rmtree(root, onerror=make_removable)
        except Exception:
            pass
        try:
            if not root.exists():
                return True
        except Exception:
            pass
    return False


def _private_probe_environment(
    policy: object,
    copied: Mapping[str, str],
    private_root: Path,
) -> dict[str, str]:
    _environment_keys(policy)
    environment = dict(copied)
    temporary = str(private_root / "tmp")
    environment.update({"TEMP": temporary, "TMP": temporary, "TMPDIR": temporary})
    if policy == PROFILE_PROBE_POLICY:
        home = str(private_root / "home")
        environment["HOME"] = home
        if os.name == "nt":
            environment["USERPROFILE"] = home
    return environment


def _validated_probe_argv_snapshot(command: object) -> tuple[str, ...] | None:
    if type(command) is not list and type(command) is not tuple:
        return None
    snapshot = tuple(command)
    if not snapshot:
        return None
    if any(type(item) is not str or not item or "\0" in item for item in snapshot):
        return None
    executable = snapshot[0]
    if not os.path.isabs(executable) or os.path.normpath(executable) != executable:
        return None
    try:
        size = sum(len(item.encode("utf-8", "strict")) + 1 for item in snapshot)
    except UnicodeError:
        return None
    return snapshot if size <= PROBE_ARGV_LIMIT else None


def valid_probe_argv(command: object) -> bool:
    return _validated_probe_argv_snapshot(command) is not None


def _valid_timeout(value: object, maximum: float) -> bool:
    return (
        type(value) in {int, float}
        and math.isfinite(value)
        and 0 < value <= maximum
    )


def _read_bounded_pipe(
    pipe: BinaryIO,
    buffer: bytearray,
    overflow: threading.Event,
    failed: threading.Event,
    stop: threading.Event,
    eof: threading.Event,
    deadline: float,
) -> None:
    drain_deadline: float | None = None
    try:
        descriptor = pipe.fileno()
        os.set_blocking(descriptor, False)
        while True:
            try:
                chunk = os.read(descriptor, 8192)
            except BlockingIOError:
                if not stop.is_set():
                    stop.wait(0.01)
                    continue
                now = time.monotonic()
                if drain_deadline is None:
                    drain_deadline = min(
                        deadline,
                        now + PROBE_PIPE_DRAIN_SECONDS,
                    )
                if now >= drain_deadline:
                    return
                time.sleep(min(0.005, drain_deadline - now))
                continue
            if not chunk:
                eof.set()
                return
            remaining = (PROBE_PIPE_LIMIT + 1) - len(buffer)
            if remaining > 0:
                buffer.extend(chunk[:remaining])
            if len(buffer) > PROBE_PIPE_LIMIT:
                overflow.set()
    except Exception:
        failed.set()


def _write_bounded_pipe(
    pipe: BinaryIO,
    value: bytes,
    failed: threading.Event,
    stop: threading.Event,
    complete: threading.Event,
    deadline: float,
) -> None:
    offset = 0
    try:
        descriptor = pipe.fileno()
        os.set_blocking(descriptor, False)
        while offset < len(value):
            if stop.is_set() or time.monotonic() >= deadline:
                return
            try:
                written = os.write(descriptor, value[offset : offset + 8192])
            except BlockingIOError:
                stop.wait(0.005)
                continue
            if written <= 0:
                failed.set()
                return
            offset += written
        complete.set()
    except (BrokenPipeError, OSError):
        failed.set()
    except Exception:
        failed.set()
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _process_is_running(process: subprocess.Popen[bytes]) -> bool:
    try:
        return process.poll() is None
    except Exception:
        return True


def _process_containment_popen_kwargs(
    platform_name: str | None = None,
) -> dict[str, object]:
    selected_platform = os.name if platform_name is None else platform_name
    if selected_platform == "posix":
        return {"start_new_session": True}
    if selected_platform == "nt":
        return {
            "creationflags": (
                _WINDOWS_CREATE_NEW_PROCESS_GROUP | _WINDOWS_CREATE_SUSPENDED
            )
        }
    raise RuntimeError("process tree containment unavailable")


def _load_windows_kernel32():
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise RuntimeError("process tree containment unavailable")
    try:
        kernel32 = loader("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.CreateToolhelp32Snapshot.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel32.Thread32First.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_ThreadEntry32),
        ]
        kernel32.Thread32First.restype = ctypes.c_int
        kernel32.Thread32Next.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_ThreadEntry32),
        ]
        kernel32.Thread32Next.restype = ctypes.c_int
        kernel32.OpenThread.argtypes = [
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint32,
        ]
        kernel32.OpenThread.restype = ctypes.c_void_p
        kernel32.ResumeThread.argtypes = [ctypes.c_void_p]
        kernel32.ResumeThread.restype = ctypes.c_uint32
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.TerminateJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
    except Exception:
        raise RuntimeError("process tree containment unavailable") from None
    return kernel32


def _close_windows_job_object(handle: int, *, kernel32=None) -> bool:
    try:
        selected_kernel32 = _load_windows_kernel32() if kernel32 is None else kernel32
        return bool(selected_kernel32.CloseHandle(handle))
    except Exception:
        return False


def _terminate_windows_job_object(handle: int, *, kernel32=None) -> bool:
    try:
        selected_kernel32 = _load_windows_kernel32() if kernel32 is None else kernel32
        return bool(selected_kernel32.TerminateJobObject(handle, 1))
    except Exception:
        return False


def _dispose_windows_job_object(handle: int, *, kernel32=None) -> bool:
    selected_kernel32 = _load_windows_kernel32() if kernel32 is None else kernel32
    if _close_windows_job_object(handle, kernel32=selected_kernel32):
        return True
    _terminate_windows_job_object(handle, kernel32=selected_kernel32)
    return _close_windows_job_object(handle, kernel32=selected_kernel32)


def _resume_windows_primary_thread(
    process_id: int,
    *,
    kernel32,
) -> None:
    snapshot = 0
    thread_handle = 0
    cleanup_ok = True
    resumed = False
    try:
        snapshot = kernel32.CreateToolhelp32Snapshot(_WINDOWS_TH32CS_SNAPTHREAD, 0)
        if not snapshot or int(snapshot) == _WINDOWS_INVALID_HANDLE_VALUE:
            raise RuntimeError("process tree containment unavailable")

        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        has_entry = bool(kernel32.Thread32First(snapshot, ctypes.byref(entry)))
        while has_entry:
            if entry.th32OwnerProcessID == process_id:
                thread_handle = kernel32.OpenThread(
                    _WINDOWS_THREAD_SUSPEND_RESUME,
                    0,
                    entry.th32ThreadID,
                )
                break
            has_entry = bool(kernel32.Thread32Next(snapshot, ctypes.byref(entry)))
        if not thread_handle:
            raise RuntimeError("process tree containment unavailable")
        resumed = kernel32.ResumeThread(thread_handle) == 1
        if not resumed:
            raise RuntimeError("process tree containment unavailable")
    except Exception:
        raise RuntimeError("process tree containment unavailable") from None
    finally:
        if thread_handle:
            try:
                cleanup_ok = bool(kernel32.CloseHandle(thread_handle)) and cleanup_ok
            except Exception:
                cleanup_ok = False
        if snapshot and int(snapshot) != _WINDOWS_INVALID_HANDLE_VALUE:
            try:
                cleanup_ok = bool(kernel32.CloseHandle(snapshot)) and cleanup_ok
            except Exception:
                cleanup_ok = False
        if resumed and not cleanup_ok:
            raise RuntimeError("process tree containment unavailable") from None


def _create_windows_job_object(process_handle: int, *, kernel32=None) -> int:
    selected_kernel32 = _load_windows_kernel32() if kernel32 is None else kernel32
    handle = 0
    try:
        handle = selected_kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise RuntimeError("process tree containment unavailable")
        limits = _JobObjectExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not selected_kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise RuntimeError("process tree containment unavailable")
        if not selected_kernel32.AssignProcessToJobObject(handle, process_handle):
            raise RuntimeError("process tree containment unavailable")
        return int(handle)
    except Exception:
        if handle:
            _dispose_windows_job_object(int(handle), kernel32=selected_kernel32)
        raise RuntimeError("process tree containment unavailable") from None


def _establish_process_containment(
    process: subprocess.Popen[bytes],
    *,
    platform_name: str | None = None,
    kernel32=None,
) -> _ProcessContainment:
    selected_platform = os.name if platform_name is None else platform_name
    if selected_platform == "posix":
        return _ProcessContainment(
            platform_name="posix",
            process_group_id=process.pid,
        )
    if selected_platform == "nt":
        try:
            process_handle = int(process._handle)  # type: ignore[attr-defined]
        except Exception:
            raise RuntimeError("process tree containment unavailable") from None
        selected_kernel32 = _load_windows_kernel32() if kernel32 is None else kernel32
        job_handle = _create_windows_job_object(
            process_handle,
            kernel32=selected_kernel32,
        )
        try:
            _resume_windows_primary_thread(
                process.pid,
                kernel32=selected_kernel32,
            )
        except Exception:
            _dispose_windows_job_object(job_handle, kernel32=selected_kernel32)
            raise RuntimeError("process tree containment unavailable") from None
        return _ProcessContainment(
            platform_name="nt",
            windows_job_handle=job_handle,
            windows_kernel32=selected_kernel32,
        )
    raise RuntimeError("process tree containment unavailable")


def _close_process_containment(containment: object) -> bool:
    if not isinstance(containment, _ProcessContainment):
        return False
    if containment.closed:
        return True
    if containment.platform_name == "posix":
        containment.closed = True
        return True
    if containment.platform_name != "nt" or containment.windows_job_handle is None:
        return False
    if not _dispose_windows_job_object(
        containment.windows_job_handle,
        kernel32=containment.windows_kernel32,
    ):
        return False
    containment.closed = True
    return True


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        return error.errno != errno.ESRCH
    return True


def _signal_process_group(process_group_id: int, selected_signal: int) -> None:
    try:
        os.killpg(process_group_id, selected_signal)
    except ProcessLookupError:
        pass
    except OSError as error:
        if error.errno != errno.ESRCH:
            raise


def _terminate_direct_process(
    process: subprocess.Popen[bytes], deadline: float
) -> bool:
    if _process_is_running(process):
        try:
            process.terminate()
        except Exception:
            pass
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=min(PROBE_TERMINATE_GRACE_SECONDS, remaining / 2))
            return True
        except Exception:
            pass
    try:
        if process.poll() is None:
            process.kill()
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except Exception:
        pass
    return not _process_is_running(process)


def _terminate_posix_process_group(
    process: subprocess.Popen[bytes],
    process_group_id: int,
    deadline: float,
) -> bool:
    try:
        if _process_group_exists(process_group_id):
            _signal_process_group(process_group_id, signal.SIGTERM)
    except Exception:
        pass

    if _process_is_running(process):
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=min(PROBE_TERMINATE_GRACE_SECONDS, remaining / 2))
        except Exception:
            pass

    try:
        if _process_group_exists(process_group_id):
            _signal_process_group(process_group_id, signal.SIGKILL)
    except Exception:
        pass

    if _process_is_running(process):
        try:
            process.kill()
        except Exception:
            pass
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except Exception:
        pass

    while time.monotonic() < deadline:
        try:
            if not _process_group_exists(process_group_id):
                break
        except Exception:
            break
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
    try:
        group_absent = not _process_group_exists(process_group_id)
    except Exception:
        group_absent = False
    return not _process_is_running(process) and group_absent


def _terminate_and_reap(
    process: subprocess.Popen[bytes],
    deadline: float,
    containment: object = None,
) -> bool:
    if isinstance(containment, _ProcessContainment):
        if (
            containment.platform_name == "posix"
            and containment.process_group_id is not None
        ):
            return _terminate_posix_process_group(
                process,
                containment.process_group_id,
                deadline,
            )
        if containment.platform_name == "nt":
            closed = _close_process_containment(containment)
            return _terminate_direct_process(process, deadline) and closed
    return _terminate_direct_process(process, deadline)


def _close_process_pipes(process: subprocess.Popen[bytes]) -> None:
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except Exception:
                pass


def _join_threads(threads: list[threading.Thread], deadline: float) -> bool:
    complete = True
    for thread in threads:
        try:
            remaining = deadline - time.monotonic()
        except Exception:
            complete = False
            continue
        if remaining <= 0:
            complete = False
            continue
        try:
            thread.join(remaining)
            if thread.is_alive():
                complete = False
        except Exception:
            complete = False
    return complete


def run_probe(
    command: list[str] | tuple[str, ...],
    *,
    environment_policy: object,
    timeout_seconds: float,
    stdin_bytes: bytes | None = None,
    source_environment: Mapping[str, str] | None = None,
    thread_prefix: str = "runtime-probe-",
) -> ProbeTransportResult:
    _environment_keys(environment_policy)
    maximum_timeout = (
        PROFILE_PROBE_MAX_TIMEOUT_SECONDS
        if environment_policy == PROFILE_PROBE_POLICY
        else PROBE_MAX_TIMEOUT_SECONDS
    )
    if not _valid_timeout(timeout_seconds, maximum_timeout):
        raise ValueError("invalid probe timeout")
    command_snapshot = _validated_probe_argv_snapshot(command)
    if command_snapshot is None:
        raise ValueError("invalid target Python invocation")
    if stdin_bytes is not None and (
        not isinstance(stdin_bytes, bytes) or len(stdin_bytes) > PROBE_PIPE_LIMIT
    ):
        raise ValueError("invalid probe input")

    copied_environment = sanitize_probe_environment(
        environment_policy,
        source_environment,
    )
    timeout = float(timeout_seconds)
    deadline = time.monotonic() + timeout
    cleanup_reserve = min(PROBE_TERMINATE_GRACE_SECONDS, timeout / 2)
    run_deadline = deadline - cleanup_reserve
    private_root: Path | None = None
    process: subprocess.Popen[bytes] | None = None
    containment: _ProcessContainment | None = None
    threads: list[threading.Thread] = []
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    overflow = threading.Event()
    reader_failed = threading.Event()
    writer_failed = threading.Event()
    stop = threading.Event()
    reader_eof: list[threading.Event] = []
    writer_complete = threading.Event()
    failure = ""
    returncode: int | None = None
    try:
        private_root = create_private_probe_root(environment_policy)
        environment = _private_probe_environment(
            environment_policy,
            copied_environment,
            private_root,
        )
        process = subprocess.Popen(
            command_snapshot,
            cwd=str(private_root / "cwd"),
            env=environment,
            shell=False,
            stdin=subprocess.DEVNULL if stdin_bytes is None else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            bufsize=0,
            **_process_containment_popen_kwargs(),
        )
        containment = _establish_process_containment(process)
        if process.stdout is None or process.stderr is None:
            failure = "invalid"
        else:
            for name, pipe, buffer in (
                ("stdout", process.stdout, stdout_buffer),
                ("stderr", process.stderr, stderr_buffer),
            ):
                eof = threading.Event()
                thread = threading.Thread(
                    target=_read_bounded_pipe,
                    args=(
                        pipe,
                        buffer,
                        overflow,
                        reader_failed,
                        stop,
                        eof,
                        deadline,
                    ),
                    name=f"{thread_prefix}{name}",
                    daemon=False,
                )
                threads.append(thread)
                thread.start()
                reader_eof.append(eof)
        if stdin_bytes is not None:
            if process.stdin is None:
                failure = "invalid"
            else:
                writer = threading.Thread(
                    target=_write_bounded_pipe,
                    args=(
                        process.stdin,
                        stdin_bytes,
                        writer_failed,
                        stop,
                        writer_complete,
                        run_deadline,
                    ),
                    name=f"{thread_prefix}stdin",
                    daemon=False,
                )
                threads.append(writer)
                writer.start()

        while not failure:
            if overflow.is_set():
                failure = "overflow"
                break
            if reader_failed.is_set() or writer_failed.is_set():
                failure = "invalid"
                break
            try:
                returncode = process.poll()
            except Exception:
                failure = "invalid"
                break
            if returncode is not None:
                break
            remaining = run_deadline - time.monotonic()
            if remaining <= 0:
                failure = "timeout"
                break
            overflow.wait(min(0.01, remaining))

        if not failure:
            try:
                returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except Exception:
                failure = "invalid"
    except Exception:
        if process is None:
            failure = "launch"
        else:
            failure = failure or "invalid"
    finally:
        cleanup_failed = False
        if process is not None:
            try:
                if not _terminate_and_reap(process, deadline, containment):
                    cleanup_failed = True
            except Exception:
                cleanup_failed = True
        if containment is not None:
            try:
                if not _close_process_containment(containment):
                    cleanup_failed = True
            except Exception:
                cleanup_failed = True
        try:
            stop.set()
        except Exception:
            cleanup_failed = True
        try:
            if not _join_threads(threads, deadline):
                cleanup_failed = True
        except Exception:
            cleanup_failed = True
        if process is not None:
            try:
                _close_process_pipes(process)
            except Exception:
                cleanup_failed = True
        try:
            if not _join_threads(threads, deadline):
                cleanup_failed = True
        except Exception:
            cleanup_failed = True
        if private_root is not None:
            try:
                if not _remove_private_probe_root(private_root):
                    cleanup_failed = True
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            failure = failure or "invalid"

    if overflow.is_set():
        failure = "overflow"
    elif reader_failed.is_set() or writer_failed.is_set():
        failure = failure or "invalid"
    if not failure and not all(event.is_set() for event in reader_eof):
        failure = "invalid"
    if stdin_bytes is not None and not failure and not writer_complete.is_set():
        failure = "invalid"

    if failure:
        raise ProbeTransportError(failure) from None
    if returncode is None:
        raise ProbeTransportError("invalid") from None
    return ProbeTransportResult(
        returncode=returncode,
        stdout=bytes(stdout_buffer),
        stderr=bytes(stderr_buffer),
    )
