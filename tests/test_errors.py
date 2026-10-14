import ctypes
import io
from threading import Event, Thread
from typing import List, Optional

import pytest

from benchset import BenchmarkResult, BenchmarkRunner, api, cli, run, run_all
from benchset._native import Native


def test_missing_library_is_a_failed_result(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> Native:
        raise OSError('missing native library')
    monkeypatch.setattr(api, 'Native', missing)
    result = run('single_core', warmup_seconds=0, measurement_seconds=0.01)
    assert not result.success
    assert result.reason == 'missing native library'


def test_cancel_during_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    loading, release = Event(), Event()
    native = Native()

    def delayed() -> Native:
        loading.set()
        assert release.wait(5)
        return native
    monkeypatch.setattr(api, 'Native', delayed)
    runner = BenchmarkRunner()
    results: List[BenchmarkResult] = []
    thread = Thread(target=lambda: results.append(runner.run('single_core', warmup_seconds=0, measurement_seconds=1)))
    thread.start()
    try:
        assert loading.wait(5)
        runner.cancel()
    finally:
        release.set()
        thread.join(5)
    assert results[0].status == 'cancelled'


def test_suite_stops_after_failure() -> None:
    results = run_all(warmup_seconds=-1)
    assert len(results) == 1
    assert not results[0].success


def test_version_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    native = Native()
    monkeypatch.setattr(ctypes, 'CDLL', lambda _path: native.lib)
    monkeypatch.setattr(native.lib, 'benchset_abi_version', lambda: 999)
    with pytest.raises(RuntimeError, match='version/ABI'):
        Native()


def test_allocation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    native = Native()
    monkeypatch.setattr(native.lib, 'benchset_create', lambda: 0)
    with pytest.raises(RuntimeError, match='allocate'):
        native.create()


@pytest.mark.parametrize('function', ['benchset_result', 'benchset_sample'])
def test_native_getter_failure(monkeypatch: pytest.MonkeyPatch, function: str) -> None:
    native = Native()
    handle = native.create()
    monkeypatch.setattr(native.lib, function, lambda *_arguments: 0)
    try:
        with pytest.raises(RuntimeError, match=r'result|sample'):
            native.execute(handle, 'single_core', 0, 0.02, None)
    finally:
        native.free(handle)


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_terminal_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    terminal = Terminal()
    monkeypatch.setattr(cli.sys, 'stdout', terminal)
    assert cli.main(['--banch', 'single_core', '--warmup', '0', '--duration', '0.02']) == 0
    assert 'measurement' in terminal.getvalue()
    assert '100.0%' in terminal.getvalue()


def test_background_failure_reaches_stderr(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def broken(*_arguments: object, **_keywords: object) -> BenchmarkResult:
        raise RuntimeError('background failure')
    monkeypatch.setattr(BenchmarkRunner, 'run', broken)
    assert cli.main(['--banch', 'single_core', '--warmup', '0', '--duration', '0.02']) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert 'background failure' in captured.err


class InterruptedEvent(Event):
    def __init__(self) -> None:
        super().__init__()
        self.first_wait = True

    def wait(self, timeout: Optional[float] = None) -> bool:
        if self.first_wait:
            self.first_wait = False
            raise KeyboardInterrupt
        return super().wait(timeout)


def test_cli_interrupt_without_console_signals(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, 'Event', InterruptedEvent)
    assert cli.main(['--banch', 'single_core', '--warmup', '20', '--duration', '1']) == 130
    captured = capsys.readouterr()
    assert not captured.out
    assert 'cancelled' in captured.err
