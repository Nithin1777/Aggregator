"""Launcher ownership: servers and tunnel start together and cannot orphan children."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from scripts import aggregate
from scripts.managed_processes import ManagedProcesses


@pytest.fixture
def launcher(monkeypatch, tmp_path):
    events = []
    client = tmp_path / 'tunnel-client.exe'
    client.touch()

    class Process:
        def __init__(self, name):
            self.name = name

        def poll(self):
            return 0 if self.name != 'tunnel' else None

    class Processes:
        def __init__(self, base_dir):
            self.children = []

        def __enter__(self):
            return self

        def start(self, cmd, name, **kwargs):
            events.append((name, cmd, kwargs))
            proc = Process(name)
            self.children.append((name, proc))
            return proc

        def __exit__(self, *exc):
            events.append(('cleanup', [name for name, _ in self.children], {}))

    monkeypatch.setattr(aggregate, 'ManagedProcesses', Processes)
    monkeypatch.setattr(aggregate, 'find_tunnel_client', lambda explicit: client)
    monkeypatch.setattr(aggregate, 'port_in_use', lambda *_: False)
    monkeypatch.setattr(aggregate, 'open_browser_later', lambda *_: None)
    return events, client


@pytest.mark.parametrize('args,names', [(['--mcp'], ['mcp', 'tunnel']),
                                       ([], ['web', 'mcp', 'tunnel']),
                                       (['--web'], ['web']),
                                       (['--mcp', '--no-tunnel'], ['mcp'])])
def test_cli_starts_the_requested_services_and_owns_cleanup(launcher, monkeypatch, args, names):
    events, client = launcher
    monkeypatch.setattr(sys, 'argv', ['aggregate', *args])
    assert aggregate.main() == 0
    assert [event[0] for event in events] == [*names, 'cleanup']
    assert events[-1][1] == names
    if 'tunnel' in names:
        tunnel = next(event for event in events if event[0] == 'tunnel')
        assert tunnel[1] == [str(client), 'run', '--profile', 'aggregate-local']
        assert tunnel[2]['cwd'] == client.parent


def test_stdio_and_list_tools_do_not_start_tunnels_or_print_launcher_logs(launcher, monkeypatch, capsys):
    events, _ = launcher
    commands = []
    monkeypatch.setattr(aggregate, 'run_foreground', lambda cmd: commands.append(cmd) or 0)
    for args in [['--mcp', '--stdio'], ['--list-tools']]:
        monkeypatch.setattr(sys, 'argv', ['aggregate', *args])
        assert aggregate.main() == 0
    assert not events and len(commands) == 2
    assert capsys.readouterr().out == ''


def test_failed_tunnel_start_cleans_up_the_already_started_mcp(launcher, monkeypatch):
    events, _ = launcher
    original = aggregate.ManagedProcesses.start

    def start(self, cmd, name, **kwargs):
        if name == 'tunnel':
            raise OSError('tunnel could not start')
        return original(self, cmd, name, **kwargs)

    monkeypatch.setattr(aggregate.ManagedProcesses, 'start', start)
    assert aggregate.run_mcp('127.0.0.1', 8001, False) == 1
    assert events[-1] == ('cleanup', ['mcp'], {})


def test_ctrl_c_stops_the_server_and_tunnel(launcher, monkeypatch):
    events, _ = launcher

    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(aggregate.time, 'sleep', interrupt)
    original = aggregate.ManagedProcesses.start

    def start(self, cmd, name, **kwargs):
        proc = original(self, cmd, name, **kwargs)
        proc.poll = lambda: None
        return proc

    monkeypatch.setattr(aggregate.ManagedProcesses, 'start', start)
    assert aggregate.run_mcp('127.0.0.1', 8001, False) == 0
    assert events[-1] == ('cleanup', ['mcp', 'tunnel'], {})


def test_tunnel_failure_stops_both_owned_servers(launcher, monkeypatch):
    events, _ = launcher
    original = aggregate.ManagedProcesses.start

    def start(self, cmd, name, **kwargs):
        proc = original(self, cmd, name, **kwargs)
        proc.poll = lambda: 7 if name == 'tunnel' else None
        return proc

    monkeypatch.setattr(aggregate.ManagedProcesses, 'start', start)
    assert aggregate.run_both('127.0.0.1', 8000, 8001, False) == 7
    assert events[-1] == ('cleanup', ['web', 'mcp', 'tunnel'], {})


def test_reused_server_stopping_stops_only_the_owned_tunnel(launcher, monkeypatch):
    events, _ = launcher
    checks = iter([True, False])
    monkeypatch.setattr(aggregate, 'port_in_use', lambda *_: next(checks))
    assert aggregate.run_mcp('127.0.0.1', 8001, False) == 0
    assert [event[0] for event in events] == ['tunnel', 'cleanup']
    assert events[-1][1] == ['tunnel']


def test_missing_client_fails_before_starting_servers(launcher, monkeypatch):
    events, _ = launcher

    def missing(_):
        raise FileNotFoundError('missing tunnel-client')

    monkeypatch.setattr(aggregate, 'find_tunnel_client', missing)
    assert aggregate.run_mcp('127.0.0.1', 8001, False) == 1
    assert not events
    assert aggregate.run_mcp('127.0.0.1', 8001, False, tunnel=False) == 0


def test_finds_the_release_in_the_project_root_and_explicit_override(tmp_path, monkeypatch):
    monkeypatch.setattr(aggregate, 'BASE_DIR', tmp_path)
    name = 'tunnel-client.exe' if os.name == 'nt' else 'tunnel-client'
    release = tmp_path / 'tunnel-client-v0.0.16-windows-amd64' / name
    release.parent.mkdir()
    release.touch()
    assert aggregate.find_tunnel_client() == release
    other = tmp_path / name
    other.touch()
    assert aggregate.find_tunnel_client(str(release)) == release
    with pytest.raises(FileNotFoundError):
        aggregate.find_tunnel_client(str(tmp_path / 'absent'))


def _wait_until(check, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError('timed out waiting for process state')


def _alive(pid):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(api.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        stat = Path(f'/proc/{pid}/stat')
        return not stat.exists() or stat.read_text().split(')')[-1].split()[0] != 'Z'
    except ProcessLookupError:
        return False


SPAWN_CHILD = (
    'import subprocess,sys,time; from pathlib import Path; '
    'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(120)"]); '
    'Path(sys.argv[1]).write_text(str(child.pid)); '
    'time.sleep(120)'
)


def test_shutdown_cleans_up_tunnel_descendants_and_preserves_unowned_process(tmp_path):
    outsider = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(120)'])
    marker = tmp_path / 'descendant.pid'
    try:
        with ManagedProcesses(tmp_path) as processes:
            tunnel = processes.start([sys.executable, '-c', SPAWN_CHILD, str(marker)], 'tunnel')
            _wait_until(lambda: marker.exists() and marker.stat().st_size > 0)
            descendant = int(marker.read_text())
            assert _alive(descendant)
        _wait_until(lambda: not _alive(descendant))
        assert tunnel.poll() is not None
        assert outsider.poll() is None
    finally:
        outsider.kill()
        outsider.wait()


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job Object crash/console-close guarantee')
def test_abrupt_launcher_death_also_removes_tunnel_descendants(tmp_path):
    marker = tmp_path / 'descendant.pid'
    parent_code = (
        'import sys,time; from pathlib import Path; '
        'from scripts.managed_processes import ManagedProcesses; '
        'processes=ManagedProcesses(Path.cwd()); processes.__enter__(); '
        'processes.start([sys.executable,"-c",sys.argv[2],sys.argv[1]],"tunnel"); '
        'time.sleep(120)'
    )
    parent = subprocess.Popen([sys.executable, '-c', parent_code, str(marker), SPAWN_CHILD],
                              cwd=str(Path(__file__).resolve().parents[1]),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_until(lambda: marker.exists() and marker.stat().st_size > 0)
        descendant = int(marker.read_text())
        assert _alive(descendant)
        parent.kill()  # Simulates closing the launcher without Python finally blocks.
        parent.wait()
        _wait_until(lambda: not _alive(descendant))
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait()
