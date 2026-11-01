"""Own launcher children, including their descendants, for one foreground run."""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path


class _WindowsJob:
    """Windows closes this handle on launcher death, killing only our children."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                ('flags', wintypes.DWORD), ('min_working_set', ctypes.c_size_t),
                ('max_working_set', ctypes.c_size_t), ('active_processes', wintypes.DWORD),
                ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                ('scheduling', wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('basic', BasicLimits), ('io_counters', ctypes.c_ulonglong * 6),
                        ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                        ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]

        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                    ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def add(self, proc):
        import ctypes
        if not self.api.AssignProcessToJobObject(self.handle, int(proc._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class ManagedProcesses:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.children: list[tuple[str, subprocess.Popen]] = []
        self.job = None

    def __enter__(self):
        if os.name == 'nt':
            self.job = _WindowsJob()
        return self

    def start(self, cmd: list[str], name: str, *, cwd: Path | None = None):
        env = dict(os.environ)
        env.setdefault('PYTHONPATH', str(self.base_dir))
        env.setdefault('PYTHONIOENCODING', 'utf-8')
        print(f'[{name}] starting...', flush=True)
        proc = subprocess.Popen(
            cmd, cwd=str(cwd or self.base_dir), env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0,
            start_new_session=os.name != 'nt',
        )
        try:
            if self.job:
                self.job.add(proc)
        except BaseException:
            # Assignment failure must not leave an unowned tunnel running.
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            proc.wait(timeout=4)
            raise
        self.children.append((name, proc))
        return proc

    def __exit__(self, *exc):
        # Signal all children before waiting, so shutdown time is bounded together.
        for name, proc in reversed(self.children):
            if proc.poll() is None:
                print(f'[{name}] stopping...', flush=True)
                try:
                    if os.name == 'nt':
                        proc.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        os.killpg(proc.pid, signal.SIGTERM)
                except OSError:
                    pass
        try:
            for _, proc in reversed(self.children):
                try:
                    proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    if os.name == 'nt':
                        proc.kill()
                    else:
                        os.killpg(proc.pid, signal.SIGKILL)
        finally:
            if self.job:
                # Also removes surviving stdio/cloudflared descendants, even if
                # tunnel-client itself has already exited.
                self.job.close()
            elif os.name != 'nt':
                for _, proc in self.children:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            for _, proc in self.children:
                proc.wait()
