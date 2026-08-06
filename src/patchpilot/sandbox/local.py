from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    return_code: int
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    duration_ms: int


class _CappedCollector:
    def __init__(self, stream: IO[bytes], max_bytes: int) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._buffer = bytearray()
        self.truncated = False

    def drain(self) -> None:
        while chunk := self._stream.read(8_192):
            remaining = self._max_bytes - len(self._buffer)
            if remaining > 0:
                self._buffer.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self.truncated = True

    def text(self, max_chars: int) -> str:
        decoded = self._buffer.decode("utf-8", errors="replace")
        if len(decoded) > max_chars:
            self.truncated = True
            return decoded[:max_chars]
        return decoded


class _JobBasicLimitInformation(ctypes.Structure):
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
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WindowsJob:
    _KILL_ON_JOB_CLOSE = 0x00002000
    _EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self, handle: int, kernel32: Any) -> None:
        self._handle = handle
        self._kernel32 = kernel32

    @classmethod
    def attach(cls, process: subprocess.Popen[bytes]) -> _WindowsJob | None:
        if os.name != "nt":
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        information = _JobExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = cls._KILL_ON_JOB_CLOSE
        configured = kernel32.SetInformationJobObject(
            handle,
            cls._EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        process_handle = getattr(process, "_handle", None)
        assigned = (
            configured
            and process_handle is not None
            and kernel32.AssignProcessToJobObject(handle, process_handle)
        )
        if not assigned:
            kernel32.CloseHandle(handle)
            return None
        return cls(int(handle), kernel32)

    def terminate(self) -> None:
        if self._handle:
            self._kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = 0


def sanitized_subprocess_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    allowed_names = {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
    environment = {
        name: value for name, value in os.environ.items() if name.upper() in allowed_names
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "NO_PROXY": "*",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
        }
    )
    if extra:
        environment.update(extra)
    return environment


def git_argv(arguments: Sequence[str]) -> list[str]:
    autocrlf = "true" if os.name == "nt" else "false"
    return ["git", "--no-pager", "-c", f"core.autocrlf={autocrlf}", *arguments]


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            process.kill()
    else:
        try:
            kill_process_group = getattr(os, "killpg", None)
            if kill_process_group is None:
                process.kill()
            else:
                kill_process_group(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except ProcessLookupError:
            return


def run_argv(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    output_max_chars: int,
    input_text: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> CommandResult:
    if not argv or any("\x00" in argument for argument in argv):
        raise ValueError("argv must contain non-empty, NUL-free arguments")
    if timeout_seconds <= 0 or output_max_chars <= 0:
        raise ValueError("command limits must be positive")

    started = time.monotonic()
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=sanitized_subprocess_environment(environment),
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=os.name != "nt",
        creationflags=creation_flags,
    )
    windows_job = _WindowsJob.attach(process)
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("failed to capture child process output")

    max_bytes = output_max_chars * 4
    stdout_collector = _CappedCollector(process.stdout, max_bytes)
    stderr_collector = _CappedCollector(process.stderr, max_bytes)
    threads = [
        threading.Thread(target=stdout_collector.drain, daemon=True),
        threading.Thread(target=stderr_collector.drain, daemon=True),
    ]
    for thread in threads:
        thread.start()

    if input_text is not None:
        if process.stdin is None:
            raise RuntimeError("failed to open child process input")
        try:
            process.stdin.write(input_text.encode("utf-8"))
            process.stdin.close()
        except BrokenPipeError:
            pass

    timed_out = False
    try:
        return_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        if windows_job is not None:
            windows_job.terminate()
        else:
            _terminate_process_tree(process)
        try:
            return_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            return_code = process.wait(timeout=5)

    if windows_job is not None:
        windows_job.close()
    for thread in threads:
        thread.join(timeout=5)
    duration_ms = round((time.monotonic() - started) * 1_000)
    stdout = stdout_collector.text(output_max_chars)
    stderr = stderr_collector.text(output_max_chars)
    return CommandResult(
        argv=tuple(argv),
        return_code=return_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        truncated=stdout_collector.truncated or stderr_collector.truncated,
        duration_ms=duration_ms,
    )
