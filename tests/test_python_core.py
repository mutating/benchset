"""Cross-boundary behavior that must survive Rust-owned suite execution."""

import ctypes
import io
import math
import signal
import subprocess
import sys
from threading import Event, Thread, get_ident
from typing import Any, List, TextIO, Type

import pytest

from benchset import (
    AggregatedScores,
    BenchmarkResult,
    BenchmarkRunner,
    Progress,
    SuiteResult,
    api,
    cli,
    run,
    run_all,
)
from benchset._native import CSuiteResult, Native
from tests.helpers import assert_runner_reusable
from tests.test_semantics import ManagedNative, ResultLibrary


@pytest.mark.parametrize('failure', ['summary', 'free'])
def test_cli_output_abort_does_not_wait_for_a_second_backend_error(failure: str) -> None:
    """After broken output, later backend failures must not park the worker again.

    A real suite delivers its first result before a summary or cleanup failure.
    A child process bounds deadlocks and exposes both waiting thread stacks;
    success requires exit 1, completed cleanup, and no traceback or leaked thread.
    """
    program = r'''
import faulthandler
import signal
import sys
from benchset import api, cli
from benchset._native import Native

native = Native()
if sys.argv[1] == 'summary':
    native.lib.benchset_suite_result = lambda *_: 0
else:
    free = native.free
    def broken_free(handle, suite=False):
        free(handle, suite)
        raise RuntimeError('backend cleanup failed')
    native.free = broken_free
api.Native = lambda: native
seen = []
def broken_output(result, _stream):
    seen.append(result.benchmark)
    raise BrokenPipeError('output closed')
cli.render = broken_output
previous = signal.getsignal(signal.SIGINT)
faulthandler.dump_traceback_later(3, exit=True)
assert cli.main(['--warmup=0', '--duration=.1']) == 1
faulthandler.cancel_dump_traceback_later()
assert seen == ['single_core']
assert signal.getsignal(signal.SIGINT) is previous
print('worker released')
'''
    result = subprocess.run([sys.executable, '-c', program, failure], capture_output=True,
                            text=True, check=False, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == 'worker released\n'
    assert not result.stderr


@pytest.mark.parametrize('name', [None, 0, b'single_core', [], {}])
def test_invalid_benchmark_types_cannot_select_a_suite(name: object) -> None:
    """Untyped callers retain TypeError rather than accidentally starting a full suite."""
    with pytest.raises(TypeError):
        run(name, warmup_seconds=0, measurement_seconds=0)  # type: ignore[arg-type]


def test_numeric_conversion_keeps_priority_over_invalid_benchmark_type() -> None:
    """Host float conversion still precedes string transport and native loading."""
    with pytest.raises(ValueError, match='invalid'):
        run(None, warmup_seconds='invalid', measurement_seconds=0)  # type: ignore[arg-type]


def test_busy_runner_uses_its_loaded_core_when_new_loading_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid losing call keeps the busy diagnostic and cannot disturb its owner."""
    runner = BenchmarkRunner()
    entered, release = Event(), Event()
    results = []

    def progress(_value: Progress) -> None:
        entered.set()
        assert release.wait(5)

    worker = Thread(target=lambda: results.append(runner.run('single_core', warmup_seconds=0,
                                                             measurement_seconds=1, on_progress=progress)))
    worker.start()
    try:
        assert entered.wait(5)

        def missing() -> Native:
            raise OSError('loader now unavailable')

        monkeypatch.setattr(api, 'Native', missing)
        contender = runner.run('multi_core', warmup_seconds=0, measurement_seconds=.01)
        assert contender.reason == 'this runner is already running'
    finally:
        runner.cancel()
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert results[0].status == 'cancelled'


@pytest.mark.parametrize('stage', ['loading', 'allocation'])
@pytest.mark.parametrize('error_type', [OSError, RuntimeError])
def test_busy_initialization_load_failure_keeps_priority_without_releasing_owner(
    monkeypatch: pytest.MonkeyPatch, stage: str, error_type: Type[Exception],
) -> None:
    """Before a core is published, the agreed loader priority cannot clear the winning call."""
    backend = ManagedNative()
    entered, release = Event(), Event()
    calls = []
    if stage == 'allocation':
        backend.delay_create = True
        entered, release = backend.created, backend.release

    def factory() -> ManagedNative:
        calls.append(1)
        if len(calls) > 1:
            raise error_type('contender loader failed')
        if stage == 'loading':
            entered.set()
            assert release.wait(5)
        return backend

    monkeypatch.setattr(api, 'Native', factory)
    runner = BenchmarkRunner()
    results = []
    worker = Thread(target=lambda: results.append(runner.run('single_core', warmup_seconds=0, measurement_seconds=.01)))
    worker.start()
    try:
        assert entered.wait(5)
        owner = runner._active
        contender = runner.run('multi_core', warmup_seconds=0, measurement_seconds=.01)
        assert contender.reason == 'contender loader failed'
        assert runner._active is owner
        assert backend.calls.count('free') == 0
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert results[0].success
    assert backend.calls == ['create', 'execute', 'free']
    monkeypatch.setattr(api, 'Native', lambda: backend)
    assert runner.run('single_core', warmup_seconds=0, measurement_seconds=.01).success


def test_cli_interrupt_during_first_result_render_stops_the_suite(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """An interrupt while printing the first completed snapshot suppresses later work and aggregate."""
    original = cli.render
    names = []

    def render(result: BenchmarkResult, stream: TextIO) -> None:
        names.append(result.benchmark)
        original(result, stream)
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)

    monkeypatch.setattr(cli, 'render', render)
    assert cli.main(['--warmup=0', '--duration=.03']) == 130
    assert names == ['single_core']
    captured = capsys.readouterr()
    assert 'single_core' in captured.out
    # HEAD's restored default signal handler also bypassed the footer here.
    assert 'Full Pi equivalent' not in captured.out + captured.err


@pytest.mark.parametrize('entrypoint', ['runner', 'run', 'run_all'])
@pytest.mark.parametrize(('warmup', 'measurement', 'error_type'), [
    ('invalid', 1, ValueError), (-1, 'invalid', ValueError), (0, None, TypeError), (0, 10**1000, OverflowError),
    ('invalid', None, ValueError),
])
def test_all_entrypoints_convert_both_numbers_before_validation_or_loading(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str, warmup: object, measurement: object,
    error_type: Type[Exception],
) -> None:
    """Numeric conversion errors keep precedence even with an unknown name or invalid warmup."""
    monkeypatch.setattr(api, 'Native', lambda: pytest.fail('conversion errors must precede native loading'))
    def execute() -> None:
        if entrypoint == 'run_all':
            run_all(warmup_seconds=warmup, measurement_seconds=measurement)  # type: ignore[arg-type]
        else:
            call = BenchmarkRunner().run if entrypoint == 'runner' else run
            call('unknown', warmup_seconds=warmup, measurement_seconds=measurement)  # type: ignore[arg-type]

    with pytest.raises(error_type):
        execute()


@pytest.mark.parametrize(('warmup', 'measurement', 'reason'), [
    (math.nan, 0, 'warmup_seconds must be finite and nonnegative'),
    (-1, math.nan, 'warmup_seconds must be finite and nonnegative'),
    (-0.0, -0.0, 'measurement_seconds must be finite and positive'),
    (0, math.inf, 'measurement_seconds must be finite and positive'),
])
def test_suite_invalid_durations_keep_exact_diagnostics_without_allocation(
    monkeypatch: pytest.MonkeyPatch, warmup: float, measurement: float, reason: str,
) -> None:
    """Rust validation retains exact suite diagnostics, signed zeros and NaN without starting workers."""
    native = Native()
    monkeypatch.setattr(api, 'Native', lambda: native)
    monkeypatch.setattr(native, 'create', lambda *_args: pytest.fail('invalid durations must not allocate a handle'))
    events: List[Progress] = []
    suite = run_all(warmup_seconds=warmup, measurement_seconds=measurement, on_progress=events.append)
    assert len(suite.results) == 1
    result = suite.results[0]
    assert result.reason == reason
    assert result.status == 'failed'
    assert not events
    for actual, expected in [(result.warmup_seconds, warmup), (result.measurement_seconds, measurement)]:
        if math.isnan(expected):
            assert math.isnan(actual)
        else:
            assert actual == expected
            assert math.copysign(1, actual) == math.copysign(1, expected)


def test_cli_invalid_warmup_precedes_invalid_measurement(capsys: pytest.CaptureFixture[str]) -> None:
    """Moving validation into Rust must not swap the first actionable CLI diagnostic."""
    assert cli.main(['--warmup=-1', '--duration=0']) == 2
    output = capsys.readouterr()
    assert not output.out
    assert '--warmup must be finite and nonnegative' in output.err


def test_real_native_cli_flushes_first_result_before_second_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """Actual sequencing waits for main-thread printing and flushing of each prior snapshot.

    Hold the first flush behind an event so prematurely acknowledging a result
    cannot escape detection merely because printing happens to win a race.
    """
    writers = []
    first_flush, allow_flush, second_progress = Event(), Event(), Event()
    premature = []

    class Stream(io.StringIO):
        flushed = ''

        def write(self, text: str) -> int:
            writers.append(get_ident())
            return super().write(text)

        def flush(self) -> None:
            if not first_flush.is_set():
                first_flush.set()
                assert allow_flush.wait(5)
            self.flushed = self.getvalue()

    output = Stream()
    checked = []

    class ProgressProbe(cli._Progress):
        def __call__(self, value: Progress) -> None:
            if value.benchmark == 'multi_core':
                second_progress.set()
                assert 'single_core' in output.flushed
                assert 'Samples:' in output.flushed
                checked.append(1)

        def clear(self) -> None:
            pass

    monkeypatch.setattr(cli.sys, 'stdout', output)
    monkeypatch.setattr(cli, '_Progress', ProgressProbe)

    def observe() -> None:
        assert first_flush.wait(5)
        premature.append(second_progress.wait(.05))
        allow_flush.set()

    observer = Thread(target=observe, daemon=True)
    observer.start()
    try:
        status = cli.main(['--warmup=0', '--duration=.05'])
    finally:
        first_flush.set()
        allow_flush.set()
        observer.join(5)
    assert status == 0
    assert premature == [False]
    assert not observer.is_alive()
    assert checked
    assert writers
    assert set(writers) == {get_ident()}


def test_real_native_cli_render_failure_releases_blocked_observer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed intermediate write cancels, releases and joins the blocked native callback before returning."""
    names = []
    native = Native()
    original_cancel = native.cancel
    cancellations = []

    def cancel(handle: int, suite: bool = False) -> None:
        cancellations.append(suite)
        original_cancel(handle, suite)

    class ProgressProbe:
        def __call__(self, value: Progress) -> None:
            names.append(value.benchmark)

        def clear(self) -> None:
            pass

    def broken(_result: BenchmarkResult, _stream: TextIO) -> None:
        raise OSError('intermediate render failed')

    monkeypatch.setattr(cli, '_Progress', ProgressProbe)
    monkeypatch.setattr(cli, 'render', broken)
    monkeypatch.setattr(api, 'Native', lambda: native)
    monkeypatch.setattr(native, 'cancel', cancel)
    assert cli.main(['--warmup=0', '--duration=.05']) == 1
    assert names
    assert set(names) == {'single_core'}
    assert cancellations
    assert all(cancellations)
    assert 'intermediate render failed' in capsys.readouterr().err
    assert_runner_reusable(BenchmarkRunner())


@pytest.mark.parametrize(('name', 'warmup', 'measurement', 'reason'), [
    ('unknown', -1, 0, 'unknown benchmark: unknown'),
    ('single_core', -1, 0, 'warmup_seconds must be finite and nonnegative'),
    ('single_core', 0, 0, 'measurement_seconds must be finite and positive'),
])
def test_reentrant_invalid_request_keeps_validation_precedence(
    name: str, warmup: float, measurement: float, reason: str,
) -> None:
    """Invalid reentry reports the same option error without releasing the owner."""
    runner = BenchmarkRunner()
    failures = []

    def callback(_progress: Progress) -> None:
        failures.append(runner.run(name, warmup_seconds=warmup, measurement_seconds=measurement))
        runner.cancel()

    result = runner.run('single_core', warmup_seconds=0, measurement_seconds=1, on_progress=callback)
    assert result.status == 'cancelled'
    assert failures
    assert all(item.reason == reason for item in failures)
    assert_runner_reusable(runner)


@pytest.mark.parametrize('benchmark', ['single_core', 'multi_core'])
@pytest.mark.parametrize('phase', ['measurement', 'completed'])
@pytest.mark.parametrize('error_type', [ValueError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_suite_callback_failure_stops_notifications_and_keeps_completed_prefix(
    benchmark: str, phase: str, error_type: Type[BaseException],
) -> None:
    """A callback failure keeps prior snapshots, stops later work and permits reuse.

    Failing in a terminal callback catches implementations that accidentally
    publish the native completed score after the host callback has failed.
    Retry only a genuine no-work failure before the callback under test can
    run. A callback that ran must satisfy the assertions on its first attempt.
    """
    events: List[Progress] = []

    def callback(value: Progress) -> None:
        events.append(value)
        if value.benchmark == benchmark and value.phase == phase:
            raise error_type('suite callback failed')

    for duration in (0.1, 0.3, 0.8):
        events.clear()
        suite = run_all(warmup_seconds=0, measurement_seconds=duration, on_progress=callback)
        if any((event.benchmark, event.phase) == (benchmark, phase) for event in events):
            break
        names = ('single_core', 'multi_core')
        assert suite.results
        assert tuple(result.benchmark for result in suite.results) == names[:len(suite.results)]
        assert all(result.success for result in suite.results[:-1])
        missed = suite.results[-1]
        assert missed.status == 'failed', missed
        assert missed.reason == 'measurement window was too short to complete any work', missed
        assert missed.iterations == 0 or missed.elapsed_seconds == 0, missed
        assert missed.score is missed.pi_equivalent is suite.aggregated_scores.pi_equivalent is None
        assert events
        assert (events[-1].benchmark, events[-1].phase) == (missed.benchmark, 'failed')
    else:
        pytest.fail('Could not reach the target callback after three measurement windows')
    assert suite.results[-1].benchmark == benchmark
    assert suite.results[-1].status == 'failed'
    assert suite.results[-1].reason == f'progress callback failed: {error_type.__name__}: suite callback failed'
    assert suite.results[-1].score is suite.results[-1].pi_equivalent is None
    assert suite.aggregated_scores.pi_equivalent is None
    assert events[-1].benchmark == benchmark
    assert events[-1].phase == phase
    if benchmark == 'multi_core':
        assert len(suite.results) == 2
        assert suite.results[0].success
    else:
        assert len(suite.results) == 1
    assert_runner_reusable(BenchmarkRunner())


@pytest.mark.parametrize('trigger', ['error', 'cancel'])
def test_suite_cancellation_targets_its_owned_native_handle(
    monkeypatch: pytest.MonkeyPatch, trigger: str,
) -> None:
    """Both a callback error and explicit cancellation must actually cancel the owned suite token."""
    native = Native()
    original = native.cancel
    calls = []

    def cancel(handle: int, suite: bool = False) -> None:
        calls.append((handle, suite))
        original(handle, suite)

    monkeypatch.setattr(native, 'cancel', cancel)
    monkeypatch.setattr(api, 'Native', lambda: native)
    runner = BenchmarkRunner()
    requested = False

    def progress(_value: Progress) -> None:
        nonlocal requested
        if not requested:
            requested = True
            if trigger == 'error':
                raise ValueError('cancel native suite')
            runner.cancel()

    result = runner._run_all(0, .03, progress)
    assert calls
    assert all(mode is True for _, mode in calls)
    assert len({handle for handle, _ in calls}) == 1
    assert result.results[-1].status == ('failed' if trigger == 'error' else 'cancelled')


@pytest.mark.parametrize('entrypoint', ['runner', 'run', 'run_all'])
@pytest.mark.parametrize('error_type', [OSError, RuntimeError])
def test_loading_failure_has_priority_over_invalid_arguments(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str, error_type: Type[Exception],
) -> None:
    """The agreed loading-first contract retains ordinary failed result objects."""
    def missing() -> Native:
        raise error_type('native unavailable')

    monkeypatch.setattr(api, 'Native', missing)
    if entrypoint == 'run_all':
        suite = run_all(warmup_seconds=-1, measurement_seconds=0)
        assert len(suite.results) == 1
        assert suite.aggregated_scores.pi_equivalent is None
        result = suite.results[0]
    else:
        execute = BenchmarkRunner().run if entrypoint == 'runner' else run
        result = execute('unknown', warmup_seconds=-1, measurement_seconds=0)
    assert result.status == 'failed'
    assert result.reason == 'native unavailable'
    assert result.score is result.pi_equivalent is None


@pytest.mark.parametrize('error_type', [OSError, RuntimeError])
def test_cli_loader_failure_is_rendered_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error_type: Type[Exception],
) -> None:
    """The agreed CLI load-error priority overrides invalid durations without a traceback."""
    def missing() -> Native:
        raise error_type('native library version/ABI or symbol unavailable')

    monkeypatch.setattr(cli, 'Native', missing)
    monkeypatch.setattr(api, 'Native', missing)
    assert cli.main(['--warmup=-1', '--duration=0']) == 1
    output = capsys.readouterr()
    assert not output.out
    assert 'native library version/ABI or symbol unavailable' in output.err
    assert 'Full Pi equivalent : —' in output.err
    assert 'Traceback' not in output.err


@pytest.mark.parametrize('operation', ['run', 'aggregate'])
def test_string_subclasses_do_not_control_native_name_encoding(operation: str) -> None:
    """Keep accepting str subclasses without calling a newly introduced encoding hook.

    Name transport must read the string value as the original ID lookup did;
    subclass encode methods must not replace validation errors or aggregation.
    """
    class Name(str):
        __slots__ = ()

        def encode(self, encoding: str = 'utf-8', errors: str = 'strict') -> bytes:
            raise AssertionError(f'the original API never called encode({encoding!r}, {errors!r})')

    name = Name('single_core')
    if operation == 'run':
        result = run(name, warmup_seconds=-1, measurement_seconds=0)
        assert result.benchmark is name
        assert result.reason == 'warmup_seconds must be finite and nonnegative'
    else:
        results = (BenchmarkResult(name, status='completed', pi_equivalent=.5),
                   BenchmarkResult(Name('multi_core'), status='completed', pi_equivalent=2))
        suite = SuiteResult.from_results(results)
        assert suite.results is results
        assert suite.aggregated_scores.pi_equivalent == 1


@pytest.mark.parametrize('name', ['много_ядер', 'single_core\x00suffix', '🦀', '\ud800'])
def test_python_transports_native_registry_ids_and_utf8_byte_lengths(name: str) -> None:
    """A binding forwards native IDs and complete encoded names without its own registry."""
    native = Native()
    library = ResultLibrary(status=0, reason=None, has_variation=0)
    calls = []

    def lookup(name: bytes, size: int) -> int:
        calls.append((name, size))
        return 73

    library.benchset_benchmark_id = lookup  # type: ignore[method-assign]
    native.lib = library  # type: ignore[assignment]
    result = native.execute(123, name, 0, .01, None)
    encoded = name.encode('utf-8', errors='surrogatepass')
    assert calls == [(encoded, len(encoded))]
    assert library.config is not None
    assert library.config.benchmark == 73
    assert result.benchmark == name


@pytest.mark.parametrize('fail_at', [1, 2])
@pytest.mark.parametrize('getter', ['benchset_result_v2', 'benchset_sample'])
def test_suite_child_decoder_failure_keeps_prefix_and_frees_once(
    monkeypatch: pytest.MonkeyPatch, fail_at: int, getter: str,
) -> None:
    """A missing child result stops the suite without losing earlier snapshots or leaking its handle."""
    native = Native()
    original_result, original_free = getattr(native.lib, getter), native.lib.benchset_suite_free
    calls = []
    frees = []

    def result(*args: object) -> int:
        calls.append(1)
        return 0 if len(calls) == fail_at else int(original_result(*args))

    def free(handle: int) -> None:
        frees.append(handle)
        original_free(handle)

    monkeypatch.setattr(native.lib, getter, result)
    monkeypatch.setattr(native.lib, 'benchset_suite_free', free)
    monkeypatch.setattr(api, 'Native', lambda: native)
    suite = run_all(warmup_seconds=0, measurement_seconds=.03)
    assert len(suite.results) == fail_at
    assert all(item.success for item in suite.results[:-1])
    assert suite.results[-1].status == 'failed'
    assert suite.results[-1].reason == ('native sample is unavailable' if getter == 'benchset_sample'
                                       else 'native library did not return a result')
    assert suite.aggregated_scores.pi_equivalent is None
    assert len(frees) == 1


def test_cli_suite_getter_failure_after_completed_children_is_not_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing final suite snapshot cannot be hidden by previously streamed completed children."""
    native = Native()
    original_free = native.lib.benchset_suite_free
    frees = []

    def free(handle: int) -> None:
        frees.append(handle)
        original_free(handle)

    monkeypatch.setattr(native.lib, 'benchset_suite_result', lambda *_args: 0)
    monkeypatch.setattr(native.lib, 'benchset_suite_free', free)
    monkeypatch.setattr(api, 'Native', lambda: native)
    assert cli.main(['--warmup=0', '--duration=.03']) == 1
    captured = capsys.readouterr()
    assert 'single_core' in captured.out
    assert 'native library did not return a suite result' in captured.err
    assert 'Full Pi equivalent : —' in captured.err
    assert len(frees) == 1


@pytest.mark.parametrize('presence', [0, 7, 2**32 - 1])
@pytest.mark.parametrize('equivalent', [0.0, 1e-300, 1.2345678901234567])
def test_suite_forwards_native_aggregate_without_recalculation(
    monkeypatch: pytest.MonkeyPatch, presence: int, equivalent: float,
) -> None:
    """Native aggregate values, including zero and noncanonical presence flags, cross the wrapper losslessly."""
    native = Native()
    original = native.lib.benchset_suite_result

    def getter(handle: int, output: Any, size: int) -> int:
        status = int(original(handle, output, size))
        value = ctypes.cast(output, ctypes.POINTER(CSuiteResult)).contents
        value.aggregated_scores.has_pi_equivalent = presence
        value.aggregated_scores.pi_equivalent = equivalent
        return status

    monkeypatch.setattr(native.lib, 'benchset_suite_result', getter)
    monkeypatch.setattr(api, 'Native', lambda: native)
    suite = run_all(warmup_seconds=0, measurement_seconds=.03)
    assert len(suite.results) == 2
    actual = suite.aggregated_scores.pi_equivalent
    if presence:
        assert actual is not None
        assert actual.hex() == equivalent.hex()
    else:
        assert actual is None


def test_suite_rejects_mismatched_native_snapshot_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """A summary whose count disagrees with delivered child snapshots cannot become partial success."""
    native = Native()
    original = native.lib.benchset_suite_result

    def getter(handle: int, output: Any, size: int) -> int:
        status = int(original(handle, output, size))
        ctypes.cast(output, ctypes.POINTER(CSuiteResult)).contents.result_count += 1
        return status

    monkeypatch.setattr(native.lib, 'benchset_suite_result', getter)
    monkeypatch.setattr(api, 'Native', lambda: native)
    suite = run_all(warmup_seconds=0, measurement_seconds=.03)
    assert suite.results[-1].status == 'failed'
    assert suite.results[-1].reason == 'native suite results are unavailable'
    assert suite.aggregated_scores.pi_equivalent is None


@pytest.mark.parametrize('interrupted', [False, True])
def test_cli_rejects_a_prefix_only_backend_and_restores_worker_ownership(
    monkeypatch: pytest.MonkeyPatch, interrupted: bool,
) -> None:
    """A prefix-only backend cannot report success or leak a worker; late Ctrl+C retains precedence."""
    workers = []
    previous = signal.getsignal(signal.SIGINT)

    class Worker(Thread):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            workers.append(self)

        def join(self, *args: Any, **kwargs: Any) -> None:
            if interrupted:
                signal.raise_signal(signal.SIGINT)
            super().join(*args, **kwargs)

    def malformed(
        _runner: BenchmarkRunner, _warmup: float, _measurement: float, _progress: object, on_result: Any,
    ) -> SuiteResult:
        result = BenchmarkResult('single_core', status='completed', score=1, pi_equivalent=1)
        assert not on_result(result)
        return SuiteResult((result,), AggregatedScores())

    monkeypatch.setattr(BenchmarkRunner, '_run_all', malformed)
    monkeypatch.setattr(cli, 'Thread', Worker)
    if interrupted:
        assert cli.main(['--warmup=0', '--duration=.02']) == 130
    else:
        with pytest.raises(RuntimeError, match='native execution did not return a result'):
            cli.main(['--warmup=0', '--duration=.02'])
    assert workers
    assert all(not worker.is_alive() for worker in workers)
    assert signal.getsignal(signal.SIGINT) is previous
