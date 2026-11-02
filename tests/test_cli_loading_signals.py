"""Native initialization remains inside the CLI's deferred cancellation boundary."""

import signal
from threading import Event, Thread, current_thread, main_thread
from typing import Any, List, Optional, Tuple, Type

import pytest

from benchset import BenchmarkRunner, cli
from benchset._native import Native


@pytest.mark.parametrize('benchmark', ['single_core', 'all'])
@pytest.mark.parametrize('stage', ['before', 'after'])
@pytest.mark.parametrize('policy', ['default', 'custom', 'ignored'])
def test_cli_sigint_during_native_loading_cancels_reports_and_releases_before_reuse(  # noqa: PLR0915 — One scope owns actual signal restoration and all worker/handle witnesses.
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    benchmark: str, stage: str, policy: str,
) -> None:
    """Defer startup SIGINT, render its native cancellation, and retain caller policy.

    The original CLI first constructed Native in its owned worker. Even a
    caller's custom/ignored SIGINT policy was temporarily deferred there, so
    Ctrl+C cancelled initialization and printed the cancelled result with 130.
    Eager main-thread argument validation must not bypass that boundary. Emit
    real signals immediately before/after real Native initialization; an Event
    makes a worker-side loader wait for observed cancellation without sleeps.
    Two calls on the same runner check restoration, joined workers and exact
    native handle release independently of Thread's own completion bookkeeping.
    """
    runner = BenchmarkRunner()
    cancelled = Event()
    caller_signals: List[int] = []
    workers: List[Tuple[Thread, Event]] = []
    live: List[Tuple[Native, int, Tuple[Any, ...]]] = []
    allocated: List[Tuple[Native, int, Tuple[Any, ...]]] = []
    released: List[Tuple[Native, int, Tuple[Any, ...]]] = []
    armed = False
    emitted = 0
    constructions = 0
    original_init, original_create, original_free = Native.__init__, Native.create, Native.free
    original_cancel = runner.cancel

    def custom(signum: int, _frame: object) -> None:
        caller_signals.append(signum)

    handler = (signal.default_int_handler if policy == 'default' else
               signal.SIG_IGN if policy == 'ignored' else custom)

    def emit() -> None:
        nonlocal armed, emitted
        if armed:
            armed = False
            emitted += 1
            signal.raise_signal(signal.SIGINT)
            if current_thread() is not main_thread():
                assert cancelled.wait(5), 'CLI did not cancel its initializing worker'

    def initialize(native: Native) -> None:
        nonlocal constructions
        constructions += 1
        if stage == 'before':
            emit()
        original_init(native)
        if stage == 'after':
            emit()

    def create(native: Native, *arguments: Any) -> int:
        handle = original_create(native, *arguments)
        identity = native, handle, arguments
        assert not live, 'the CLI overlapped native owners'
        live.append(identity)
        allocated.append(identity)
        return handle

    def free(native: Native, handle: int, *arguments: Any) -> None:
        identity = native, handle, arguments
        assert live.pop() == identity, 'a native handle was freed twice or with the wrong owner/mode'
        released.append(identity)
        original_free(native, handle, *arguments)

    def cancel() -> None:
        cancelled.set()
        original_cancel()

    class Worker(Thread):
        def __init__(self, *arguments: Any, **options: Any) -> None:
            super().__init__(*arguments, **options)
            self.finished = Event()
            self.finish_allowed = Event()
            workers.append((self, self.finished))

        def run(self) -> None:
            try:
                super().run()
            finally:
                assert self.finish_allowed.wait(5), 'CLI did not join its initialized worker'
                self.finished.set()

        def join(self, timeout: Optional[float] = None) -> None:
            self.finish_allowed.set()
            super().join(timeout)

    monkeypatch.setattr(Native, '__init__', initialize)
    monkeypatch.setattr(Native, 'create', create)
    monkeypatch.setattr(Native, 'free', free)
    monkeypatch.setattr(runner, 'cancel', cancel)
    monkeypatch.setattr(cli, 'BenchmarkRunner', lambda: runner)
    monkeypatch.setattr(cli, 'Thread', Worker)
    previous = signal.signal(signal.SIGINT, handler)
    try:
        for attempt in range(2):
            cancelled.clear()
            armed = True
            assert cli.main(['--bench=' + benchmark, '--warmup=0', '--duration=.01']) == 130
            output, errors = capsys.readouterr()
            assert not output
            assert 'Benchmark                  : single_core' in errors
            assert 'Status                     : cancelled' in errors
            assert 'Reason                     : cancelled before starting' in errors
            assert 'Traceback' not in errors
            assert errors.count('Full Pi equivalent :') == (1 if benchmark == 'all' else 0)
            assert signal.getsignal(signal.SIGINT) is handler
            assert not caller_signals, 'startup SIGINT escaped the CLI cancellation policy'
            assert emitted == attempt + 1
            assert constructions == attempt + 1, 'the observer must reuse the runner-owned Native'
            assert cancelled.is_set()
            assert runner._active is None
            assert runner._native is None
            assert runner._handle is None
            assert not live
            assert released == allocated
            assert len(allocated) == attempt + 1
            assert workers
            assert all(done.is_set() and not worker.is_alive() for worker, done in workers)
    finally:
        armed = False
        cancelled.set()
        for worker, _done in workers:
            worker.join(timeout=5)
        signal.signal(signal.SIGINT, previous)


@pytest.mark.parametrize('benchmark', ['single_core', 'all'])
@pytest.mark.parametrize('window', ['warmup', 'duration'])
def test_cli_initial_load_error_precedes_invalid_arguments_even_if_a_retry_could_succeed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], benchmark: str, window: str,
) -> None:
    """A later successful open must not reclassify an initial load failure as usage.

    Loading errors have the explicitly accepted CLI precedence and exit 1.
    Before the startup fix settings swallowed its first OSError; the worker's
    later ordinary invalid-result path still returned 1. A post-result native
    validation retry must not turn that load-failure path into argparse exit 2.
    """
    original_init = Native.__init__
    attempts = 0

    def initialize(native: Native) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError('first native load failed')
        original_init(native)

    monkeypatch.setattr(Native, '__init__', initialize)
    arguments = ['--bench=' + benchmark, '--warmup=0', '--duration=.01']
    arguments[1 if window == 'warmup' else 2] = '--' + window + '=-1'
    assert cli.main(arguments) == 1
    output, errors = capsys.readouterr()
    assert not output
    assert 'Status                     : failed' in errors
    assert 'usage:' not in errors
    assert 'Traceback' not in errors
    assert attempts >= 1


@pytest.mark.parametrize('benchmark', ['single_core', 'all'])
@pytest.mark.parametrize('invalid', [False, True])
@pytest.mark.parametrize('error_type', [ValueError, AttributeError, RuntimeError, OSError, OverflowError])
def test_cli_optional_duration_inspection_errors_still_defer_to_the_native_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    benchmark: str, invalid: bool, error_type: Type[Exception],
) -> None:
    """Optional CLI duration inspection cannot replace the API worker's result.

    settings previously swallowed ordinary duration_error failures, leaving
    execution to report its actual validation or cancellation result. The new
    observer must retain that fallback without loading another Native object.
    Real native cancellation makes valid-input outcomes deterministic, while
    invalid input still returns a normal failed result rather than usage exit 2.
    """
    runner = BenchmarkRunner()
    inspections = 0
    original = error_type('optional duration inspection failed')

    def inspect(_native: Native, _warmup: float, _measurement: float) -> str:
        nonlocal inspections
        inspections += 1
        raise original

    class Progress(cli._Progress):
        def __call__(self, value: Any) -> None:
            super().__call__(value)
            runner.cancel()

    monkeypatch.setattr(Native, 'duration_error', inspect, raising=False)
    monkeypatch.setattr(cli, 'BenchmarkRunner', lambda: runner)
    monkeypatch.setattr(cli, '_Progress', Progress)
    assert cli.main(['--bench=' + benchmark, '--warmup=0', '--duration=' + ('-1' if invalid else '.1')]) == (
        1 if invalid else 130
    )
    output, errors = capsys.readouterr()
    assert not output
    assert 'Status                     : ' + ('failed' if invalid else 'cancelled') in errors
    assert 'optional duration inspection failed' not in errors
    assert 'Traceback' not in errors
    assert 'usage:' not in errors
    assert inspections == 1
    assert runner._active is None
    assert runner._native is None
    assert runner._handle is None


@pytest.mark.parametrize(('arguments', 'reason'), [
    (['--warmup=-1'], '--warmup must be finite and nonnegative'),
    (['--warmup=nan'], '--warmup must be finite and nonnegative'),
    (['--warmup=inf'], '--warmup must be finite and nonnegative'),
    (['--duration=0'], '--duration must be finite and positive'),
    (['--duration=-1'], '--duration must be finite and positive'),
    (['--duration=nan'], '--duration must be finite and positive'),
    (['--duration=inf'], '--duration must be finite and positive'),
])
def test_standalone_settings_still_rejects_invalid_windows_with_the_rust_cli_diagnostic(
    capsys: pytest.CaptureFixture[str], arguments: List[str], reason: str,
) -> None:
    """Keep direct settings validation after execution switches to parse-only setup.

    Public settings(argv) must still load Rust, select its CLI spelling and
    raise argparse's SystemExit(2) immediately; execution's deferred observer
    must not silently turn this standalone helper into a parsing-only API.
    """
    with pytest.raises(SystemExit) as caught:
        cli.settings(arguments)
    assert caught.value.code == 2
    output, errors = capsys.readouterr()
    assert not output
    assert errors.endswith('benchset: error: ' + reason + '\n')
    assert 'usage: benchset' in errors


@pytest.mark.parametrize('stage', ['loading', 'inspection'])
@pytest.mark.parametrize('error_type', [ValueError, AttributeError, RuntimeError, OSError, OverflowError])
def test_standalone_settings_retains_the_optional_loading_and_inspection_error_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    stage: str, error_type: Type[Exception],
) -> None:
    """The safe helper's implicit-Native path retains ordinary prevalidation fallback.

    A direct settings call still returns parsed arguments when native loading
    or optional duration inspection fails, even if duration is invalid. Those
    failures belong to the eventual execution boundary; parser errors must not
    mask them. This complements the provided-Native observer cases above.
    """
    original_init = Native.__init__
    calls: List[str] = []

    def initialize(native: Native) -> None:
        calls.append('loading')
        if stage == 'loading':
            raise error_type('optional loading failed')
        original_init(native)

    def inspect(_native: Native, _warmup: float, _measurement: float) -> str:
        calls.append('inspection')
        raise error_type('optional inspection failed')

    monkeypatch.setattr(Native, '__init__', initialize)
    monkeypatch.setattr(Native, 'duration_error', inspect)
    config = cli.settings(['--bench=single_core', '--warmup=0', '--duration=-1'])
    assert config.bench == 'single_core'
    assert config.warmup == 0.0
    assert config.duration == -1.0
    assert calls == (['loading'] if stage == 'loading' else ['loading', 'inspection'])
    assert capsys.readouterr() == ('', '')
