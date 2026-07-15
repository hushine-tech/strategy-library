from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
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
PROFILE_PROBE_TIMEOUT_SECONDS = 30.0
PROBE_MAX_TIMEOUT_SECONDS = 30.0
PROBE_TERMINATE_GRACE_SECONDS = 1.0
PROBE_PIPE_DRAIN_SECONDS = 0.1
_SOURCE_DATE_EPOCH_MAX_DIGITS = 20
_WINDOWS_ENV_KEYS = frozenset({"SYSTEMROOT", "WINDIR"})
_WINDOWS_SECURE_DIRECTORY_MIN_VERSION = (3, 12, 4)


@dataclass(frozen=True, slots=True)
class ProbeTransportResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class ProbeTransportError(RuntimeError):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__("probe transport failed")


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
        if not isinstance(raw_key, str) or not isinstance(value, str):
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


def valid_probe_argv(command: object) -> bool:
    if not isinstance(command, (list, tuple)) or not command:
        return False
    if any(not isinstance(item, str) or not item or "\0" in item for item in command):
        return False
    executable = command[0]
    if not os.path.isabs(executable) or os.path.normpath(executable) != executable:
        return False
    try:
        size = sum(len(item.encode("utf-8", "strict")) + 1 for item in command)
    except UnicodeError:
        return False
    return size <= PROBE_ARGV_LIMIT


def _valid_timeout(value: object) -> bool:
    return (
        type(value) in {int, float}
        and math.isfinite(value)
        and 0 < value <= PROBE_MAX_TIMEOUT_SECONDS
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


def _terminate_and_reap(process: subprocess.Popen[bytes], deadline: float) -> bool:
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


def _close_process_pipes(process: subprocess.Popen[bytes]) -> None:
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except Exception:
                pass


def _join_threads(threads: list[threading.Thread], deadline: float) -> None:
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        thread.join(remaining)


def run_probe(
    command: list[str],
    *,
    environment_policy: object,
    timeout_seconds: float,
    stdin_bytes: bytes | None = None,
    source_environment: Mapping[str, str] | None = None,
    thread_prefix: str = "runtime-probe-",
) -> ProbeTransportResult:
    if not _valid_timeout(timeout_seconds):
        raise ValueError("invalid probe timeout")
    _environment_keys(environment_policy)
    if not valid_probe_argv(command):
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
            command,
            cwd=str(private_root / "cwd"),
            env=environment,
            shell=False,
            stdin=subprocess.DEVNULL if stdin_bytes is None else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            bufsize=0,
        )
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
                thread.start()
                threads.append(thread)
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
                writer.start()
                threads.append(writer)

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

        if failure:
            _terminate_and_reap(process, deadline)
        else:
            try:
                returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except Exception:
                failure = "invalid"
                _terminate_and_reap(process, deadline)

        stop.set()
        _join_threads(threads, deadline)
        if any(thread.is_alive() for thread in threads):
            failure = failure or "invalid"
        if not failure and not all(event.is_set() for event in reader_eof):
            failure = "invalid"
        if stdin_bytes is not None and not failure and not writer_complete.is_set():
            failure = "invalid"
        if overflow.is_set():
            failure = "overflow"
        elif reader_failed.is_set() or writer_failed.is_set():
            failure = failure or "invalid"
    except Exception:
        if process is None:
            failure = "launch"
        else:
            failure = failure or "invalid"
            _terminate_and_reap(process, deadline)
    finally:
        if process is not None and _process_is_running(process):
            _terminate_and_reap(process, deadline)
        stop.set()
        _join_threads(threads, deadline)
        if process is not None:
            _close_process_pipes(process)
        if private_root is not None and not _remove_private_probe_root(private_root):
            failure = failure or "invalid"

    if failure:
        raise ProbeTransportError(failure) from None
    if returncode is None:
        raise ProbeTransportError("invalid") from None
    return ProbeTransportResult(
        returncode=returncode,
        stdout=bytes(stdout_buffer),
        stderr=bytes(stderr_buffer),
    )
