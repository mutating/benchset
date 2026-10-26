import ctypes
import io
import signal
from threading import Event, Thread
from typing import List, Optional

import pytest

from benchset import BenchmarkResult, BenchmarkRunner, api, cli, run, run_all
from benchset._native import Native
from tests.helpers import assert_result_references


def test_missing_library_is_a_failed_result(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> Native:
        raise OSError('missing native library')
    monkeypatch.setattr(api, 'Native', missing)
    result = run('single_core', warmup_seconds=0, measurement_seconds=0.01)
    assert_result_references(result)
    assert not result.success
    assert result.pi_equivalent is None
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
    assert_result_references(results[0])
    assert results[0].pi_equivalent is None


def test_suite_stops_after_failure() -> None:
    results = run_all(warmup_seconds=-1)
    assert len(results) == 1
    assert not results[0].success
    assert_result_references(results[0])


def test_version_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    native = Native()
    monkeypatch.setattr(ctypes, 'CDLL', lambda _path: native.lib)
    monkeypatch.setattr(native.lib, 'benchset_abi_version', lambda: 999)
    with pytest.raises(RuntimeError, match='version/ABI'):
        Native()
    result = run('single_core', warmup_seconds=0, measurement_seconds=0.01)
    assert_result_references(result)
    assert result.status == 'failed'
    assert result.reason is not None
    assert 'version/ABI' in result.reason


def test_allocation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    native = Native()
    monkeypatch.setattr(native.lib, 'benchset_create', lambda: 0)
    with pytest.raises(RuntimeError, match='allocate'):
        native.create()
    monkeypatch.setattr(api, 'Native', lambda: native)
    result = run('single_core', warmup_seconds=0, measurement_seconds=0.01)
    assert_result_references(result)
    assert result.status == 'failed'
    assert result.reason is not None
    assert 'allocate' in result.reason


@pytest.mark.parametrize('function', ['benchset_result_v2', 'benchset_sample'])
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
    monkeypatch.setenv('COLUMNS', '80')
    monkeypatch.setattr(cli.sys, 'stdout', terminal)
    assert cli.main(['--bench', 'single_core', '--warmup', '0', '--duration', '0.02']) == 0
    assert 'measurement' in terminal.getvalue()
    assert '100.0%' in terminal.getvalue()


def test_background_failure_reaches_stderr(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def broken(*_arguments: object, **_keywords: object) -> BenchmarkResult:
        raise RuntimeError('background failure')
    monkeypatch.setattr(BenchmarkRunner, 'run', broken)
    assert cli.main(['--bench', 'single_core', '--warmup', '0', '--duration', '0.02']) == 1
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
    assert cli.main(['--bench', 'single_core', '--warmup', '20', '--duration', '1']) == 130
    captured = capsys.readouterr()
    assert not captured.out
    assert 'cancelled' in captured.err


@pytest.mark.parametrize('boundary', ['before_start', 'after_start', 'before_join', 'after_join'])
def test_cli_interrupt_at_worker_boundaries(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], boundary: str,
) -> None:
    runner = BenchmarkRunner()
    workers: List[Thread] = []
    original_handler = signal.getsignal(signal.SIGINT)

    def interrupt() -> None:
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)

    class SignallingThread(Thread):
        def start(self) -> None:
            workers.append(self)
            if boundary == 'before_start':
                interrupt()
            super().start()
            if boundary == 'after_start':
                interrupt()

        def join(self, timeout: Optional[float] = None) -> None:
            if boundary == 'before_join':
                interrupt()
            super().join(timeout)
            if boundary == 'after_join':
                interrupt()

    monkeypatch.setattr(cli, 'Thread', SignallingThread)
    monkeypatch.setattr(cli, 'BenchmarkRunner', lambda: runner)
    try:
        try:
            status = cli.main(['--warmup', '0', '--duration', '0.05'])
        except KeyboardInterrupt:
            pytest.fail('SIGINT escaped the CLI during ' + boundary)
        assert status == 130
    finally:
        runner.cancel()
        for worker in workers:
            if worker.ident is not None:
                Thread.join(worker, 5)
    assert workers
    assert all(not worker.is_alive() for worker in workers)
    assert len(workers) == 1  # An interrupted suite must not start multi_core.
    assert signal.getsignal(signal.SIGINT) is original_handler
    captured = capsys.readouterr()
    assert not captured.out
    assert 'single_core' in captured.err


def test_cli_restores_signal_handler_after_start_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    original_handler = signal.getsignal(signal.SIGINT)

    class BrokenThread(Thread):
        def start(self) -> None:
            raise RuntimeError('cannot start CLI worker')

    monkeypatch.setattr(cli, 'Thread', BrokenThread)
    with pytest.raises(RuntimeError, match='cannot start CLI worker'):
        cli.main(['--bench', 'single_core'])
    assert signal.getsignal(signal.SIGINT) is original_handler


def test_cli_can_run_outside_main_thread(capsys: pytest.CaptureFixture[str]) -> None:
    results: List[int] = []
    worker = Thread(target=lambda: results.append(cli.main([
        '--bench', 'single_core', '--warmup', '0', '--duration', '0.02',
    ])))
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert results == [0]
    captured = capsys.readouterr()
    assert 'completed' in captured.out
    assert not captured.err
