"""Behavioral boundaries beyond line coverage: ownership, callbacks and decoding."""

import ctypes
import importlib.metadata
import importlib.util
import io
import math
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from threading import Event, Thread, get_ident
from typing import Any, Callable, List, Optional, Tuple, Type

import pytest

from benchset import (
    BenchmarkResult,
    BenchmarkRunner,
    Progress,
    Sample,
    api,
    cli,
    run,
    run_all,
)
from benchset._native import (
    Callback,
    Config,
    CProgress,
    CResult,
    CResultV2,
    CSample,
    Native,
)
from tests.helpers import assert_result_references, assert_runner_reusable


def test_model_and_result_versions_come_from_package_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: List[str] = []

    def distribution_version(name: str) -> str:
        requested.append(name)
        return '7.8.9'

    monkeypatch.setattr(importlib.metadata, 'version', distribution_version)
    path = Path(__file__).resolve().parents[1] / 'benchset/models.py'
    specification = importlib.util.spec_from_file_location('benchset_version_probe', path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    monkeypatch.setitem(sys.modules, specification.name, module)
    specification.loader.exec_module(module)
    assert requested == ['benchset']
    assert module.__version__ == '7.8.9'
    assert module.BenchmarkResult('single_core').version == '7.8.9'


@pytest.mark.parametrize(('warmup', 'measurement'), [
    (math.nan, 1), (math.inf, 1), (-math.inf, 1), (-1, 1),
    (0, math.nan), (0, math.inf), (0, -math.inf), (0, -1), (0, -0.0),
])
def test_python_validation_does_not_allocate_native(
    monkeypatch: pytest.MonkeyPatch, warmup: float, measurement: float,
) -> None:
    """Reject bad durations before loading Rust and identify the offending option.

    A generic failure reason hides whether the caller must fix warmup or
    measurement and whether zero is allowed for that particular duration.
    """
    def forbidden() -> Native:
        pytest.fail('invalid options must not load native code')
    monkeypatch.setattr(api, 'Native', forbidden)
    result = run('single_core', warmup_seconds=warmup, measurement_seconds=measurement)
    assert_result_references(result)
    assert result.status == 'failed'
    assert result.score is None
    assert result.pi_equivalent is None
    assert result.reason
    invalid_warmup = not math.isfinite(warmup) or warmup < 0
    assert ('warmup_seconds' if invalid_warmup else 'measurement_seconds') in result.reason
    assert 'finite' in result.reason
    assert ('nonnegative' if invalid_warmup else 'positive') in result.reason
    if math.isnan(warmup):
        assert math.isnan(result.warmup_seconds)
    else:
        assert result.warmup_seconds == warmup
    if math.isnan(measurement):
        assert math.isnan(result.measurement_seconds)
    else:
        assert result.measurement_seconds == measurement


@pytest.mark.parametrize(('warmup', 'measurement'), [(sys.float_info.max, 1), (0, sys.float_info.max), (0, 1e-12)])
def test_native_duration_limits_preserve_requested_diagnostics(warmup: float, measurement: float) -> None:
    result = run('single_core', warmup_seconds=warmup, measurement_seconds=measurement)
    assert_result_references(result)
    assert result.status == 'failed'
    assert result.score is None
    assert result.pi_equivalent is None
    assert result.warmup_seconds == warmup
    assert result.measurement_seconds == measurement
    assert result.reason


@pytest.mark.parametrize('phase', ['warmup', 'measurement', 'completed'])
@pytest.mark.parametrize('error_type', [ValueError, KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize('formatting_error', [None, ValueError, TypeError, RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_callback_base_exceptions_stop_notifications_and_allow_reuse(
    monkeypatch: pytest.MonkeyPatch, phase: str, error_type: Type[BaseException],
    formatting_error: Optional[Type[BaseException]],
) -> None:
    """Return a failed snapshot for callback errors, even if their message raises.

    Keep the original error's type as a fallback, suppress later notifications,
    preserve completed native data, free the handle and permit runner reuse.
    Secondary BaseExceptions must not escape from diagnostic formatting.
    """
    native = Native()
    library = CallbackLibrary()
    monkeypatch.setattr(native, 'lib', library)
    monkeypatch.setattr(api, 'Native', lambda: native)
    runner = BenchmarkRunner()
    events: List[Progress] = []
    caller = get_ident()
    error = error_type('callback boundary')
    if formatting_error is not None:
        formatter_failure = formatting_error

        def unprintable(_error: BaseException) -> str:
            raise formatter_failure('formatter failed')

        unprintable_type: Type[BaseException] = type('Unprintable', (error_type,), {'__str__': unprintable})
        error = unprintable_type()

    def callback(progress: Progress) -> None:
        assert get_ident() == caller
        events.append(progress)
        if progress.phase == phase:
            raise error

    try:
        result = runner.run('single_core', warmup_seconds=0.02, measurement_seconds=0.04, on_progress=callback)
    except BaseException as escaped:  # noqa: BLE001 — Report even an escaped interrupt as a test failure.
        pytest.fail(f'callback diagnostic escaped as {type(escaped).__name__}')
    assert_result_references(result)
    assert result.status == 'failed'
    assert result.score is None
    expected_reason = f'progress callback failed: {type(error).__name__}'
    if formatting_error is None:
        expected_reason += ': callback boundary'
    assert result.reason == expected_reason
    assert result.pi_equivalent is None
    assert events[-1].phase == phase
    assert sum(event.phase == phase for event in events) == 1
    assert library.cancelled == 1
    assert library.freed == [1]
    if phase == 'completed':
        assert result.iterations > 0
        assert result.samples
    recovered = runner.run('single_core', warmup_seconds=0, measurement_seconds=0.02)
    assert_result_references(recovered)
    assert recovered.success
    assert recovered.pi_equivalent == 0.375
    assert library.created == library.freed == [1, 2]
    assert library.cancelled == 1


def test_reentrant_runner_global_busy_and_idle_cancellation() -> None:
    runner = BenchmarkRunner()
    runner.cancel()
    checked = False

    def callback(progress: Progress) -> None:
        nonlocal checked
        if checked or progress.phase != 'measurement':
            return
        checked = True
        same = runner.run('multi_core', warmup_seconds=0, measurement_seconds=0.01)
        assert_result_references(same)
        assert same.reason == 'this runner is already running'
        assert same.pi_equivalent is None
        other = run('multi_core', warmup_seconds=0, measurement_seconds=0.01)
        assert_result_references(other)
        assert other.reason == 'another benchmark is already running'
        assert other.pi_equivalent is None
    completed = runner.run('single_core', warmup_seconds=-0.0, measurement_seconds=0.03, on_progress=callback)
    assert_result_references(completed)
    assert completed.success
    assert checked
    runner.cancel()
    assert_runner_reusable(runner)


class ManagedNative(Native):
    def __init__(self) -> None:
        self.calls: List[str] = []
        self.error_at: Optional[str] = None
        self.created, self.release = Event(), Event()
        self.delay_create = False

    def create(self) -> int:
        self.calls.append('create')
        if self.error_at == 'create':
            raise RuntimeError('create failure')
        if self.delay_create:
            self.created.set()
            assert self.release.wait(5)
        return 123

    def cancel(self, handle: int) -> None:
        assert handle == 123
        self.calls.append('cancel')

    def free(self, handle: int) -> None:
        assert handle == 123
        self.calls.append('free')

    def execute(
        self, handle: int, benchmark: str, warmup_seconds: float, measurement_seconds: float,
        on_progress: Optional[Callable[[Progress], None]],
    ) -> BenchmarkResult:
        assert handle == 123
        self.calls.append('execute')
        if self.error_at == 'execute':
            raise OverflowError('execute failure')
        if on_progress:
            on_progress(Progress(benchmark, 'completed', 1, 1, 0.1, 0.1, 8))
        return BenchmarkResult(benchmark, status='completed', score=80,
                               warmup_seconds=warmup_seconds, measurement_seconds=measurement_seconds)


@pytest.mark.parametrize('entrypoint', ['runner', 'run', 'run_all'])
@pytest.mark.parametrize('durations', [None, (1.2345678901234567, 9.876543210987654)])
def test_public_api_forwards_durations_and_callback_without_running_long_benchmarks(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str, durations: Optional[Tuple[float, float]],
) -> None:
    """Accept benchmark keywords, forward windows and preserve caller-thread callbacks.

    Recorded thread IDs expose accidental suite offloading without relying on
    how callback exceptions are converted into benchmark failures.
    """
    backend = ManagedNative()
    monkeypatch.setattr(api, 'Native', lambda: backend)
    options = {} if durations is None else {'warmup_seconds': durations[0], 'measurement_seconds': durations[1]}
    expected_durations = (120.0, 60.0) if durations is None else durations
    events: List[Progress] = []
    caller = get_ident()
    callback_threads: List[int] = []

    def record(progress: Progress) -> None:
        events.append(progress)
        callback_threads.append(get_ident())

    results: Tuple[BenchmarkResult, ...]
    if entrypoint == 'runner':
        results = (BenchmarkRunner().run(benchmark='single_core', on_progress=record, **options),)
    elif entrypoint == 'run':
        results = (run(benchmark='single_core', on_progress=record, **options),)
    else:
        results = run_all(on_progress=record, **options).results
    for result in results:
        assert_result_references(result)
    benchmarks = ['single_core', 'multi_core'] if entrypoint == 'run_all' else ['single_core']
    assert [(result.benchmark, result.warmup_seconds, result.measurement_seconds)
            for result in results] == [(benchmark, *expected_durations) for benchmark in benchmarks]
    assert [event.benchmark for event in events] == benchmarks
    assert callback_threads == [caller] * len(benchmarks)
    assert all(result.success for result in results)
    assert backend.calls == ['create', 'execute', 'free'] * len(benchmarks)


@pytest.mark.parametrize('stage', ['init', 'create', 'execute'])
def test_runner_cleanup_after_errors_and_reuse(monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    backend = ManagedNative()
    backend.error_at = stage
    fail_init = stage == 'init'

    def factory() -> Native:
        if fail_init:
            raise OSError('init failure')
        return backend
    monkeypatch.setattr(api, 'Native', factory)
    runner = BenchmarkRunner()
    result = runner.run('single_core', warmup_seconds=0, measurement_seconds=0.1)
    assert_result_references(result)
    assert result.status == 'failed'
    assert result.pi_equivalent is None
    assert result.reason == stage + ' failure'
    assert backend.calls.count('free') == (1 if stage == 'execute' else 0)
    fail_init = False
    backend.error_at = None
    recovered = runner.run('single_core', warmup_seconds=0, measurement_seconds=0.1)
    assert_result_references(recovered)
    assert recovered.success
    runner.cancel()
    assert backend.calls.count('free') == (2 if stage == 'execute' else 1)
    assert 'cancel' not in backend.calls


def test_cancel_during_handle_creation_is_delivered_before_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = ManagedNative()
    backend.delay_create = True
    monkeypatch.setattr(api, 'Native', lambda: backend)
    runner = BenchmarkRunner()
    results: List[BenchmarkResult] = []
    thread = Thread(target=lambda: results.append(runner.run('single_core', warmup_seconds=0, measurement_seconds=0.1)))
    thread.start()
    try:
        assert backend.created.wait(5)
        runner.cancel()
        runner.cancel()
    finally:
        backend.release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert len(results) == 1
    assert_result_references(results[0])
    assert backend.calls == ['create', 'cancel', 'execute', 'free']
    runner.cancel()
    assert backend.calls[-1] == 'free'


def test_busy_call_cannot_clear_another_threads_runner_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = ManagedNative()
    started, release = Event(), Event()
    execute = backend.execute

    def blocked(*args: Any) -> BenchmarkResult:
        started.set()
        assert release.wait(5)
        return execute(*args)

    monkeypatch.setattr(backend, 'execute', blocked)
    monkeypatch.setattr(api, 'Native', lambda: backend)
    runner = BenchmarkRunner()
    results: List[BenchmarkResult] = []
    thread = Thread(target=lambda: results.append(runner.run('single_core')))
    thread.start()
    try:
        assert started.wait(5)
        owner = runner._active
        assert owner
        busy = runner.run('multi_core', warmup_seconds=0, measurement_seconds=.01)
        assert_result_references(busy)
        assert busy.reason == 'this runner is already running'
        assert runner._active is owner
        assert runner._handle == 123
        assert runner._native is backend
        assert 'free' not in backend.calls
        runner.cancel()
        assert backend.calls == ['create', 'cancel']
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert len(results) == 1
    assert_result_references(results[0])
    assert backend.calls.count('create') == backend.calls.count('free') == 1
    assert not runner._active
    assert runner._handle is None
    assert runner._native is None


class ResultLibrary:
    def __init__(self, status: int, reason: Optional[bytes], has_variation: int) -> None:
        self.value = CResultV2(1, status, 2, 2**32 - 1, 123.5, 2.5, 3.5, 4.5, 0,
                               2**64 - 1, 2**64 - 2, 2, 7, has_variation, 0.375, 1)
        self.reason = reason
        self.config: Optional[Config] = None
        self.cancelled = 0
        self.phases = [1, 2, 3, 4, 5]

    def benchset_run(self, _handle: int, config: Any, callback: Callable[..., None], _user: object) -> int:
        raw = ctypes.cast(config, ctypes.POINTER(Config)).contents
        self.config = Config.from_buffer_copy(raw)
        for phase in self.phases:
            progress = CProgress(phase, 2**32 - 1, 0.12345678901234567,
                                 1.2345678901234567, 2.345678901234567, 2**64 - 1)
            callback(ctypes.pointer(progress), None)
        return self.value.status

    def benchset_result(self, _handle: int, output: Any) -> int:
        ctypes.memmove(output, ctypes.byref(self.value), ctypes.sizeof(CResult))
        return 1

    def benchset_result_v2(self, _handle: int, output: Any, output_size: int) -> int:
        assert output_size == ctypes.sizeof(CResultV2)
        # C padding after the uint32 presence flag is not guaranteed to be zero.
        # Poison it so widening that flag cannot fabricate a present value.
        padding = CResultV2.has_pi_equivalent.offset + ctypes.sizeof(ctypes.c_uint32)
        ctypes.memset(ctypes.addressof(self.value) + padding, 0xA5, ctypes.sizeof(self.value) - padding)
        ctypes.memmove(output, ctypes.byref(self.value), ctypes.sizeof(self.value))
        return 1

    def benchset_sample(self, _handle: int, index: int, output: Any) -> int:
        sample = CSample(4.5123456789012345 + index, 1.5 + index, 2**64 - 1 - index, 42.123456789012345 + index)
        ctypes.memmove(output, ctypes.byref(sample), ctypes.sizeof(sample))
        return 1

    def benchset_reason(self, _handle: int) -> Optional[bytes]:
        return self.reason


class CallbackLibrary(ResultLibrary):
    """Drive actual ctypes callbacks without relying on a timed warmup window."""

    def __init__(self) -> None:
        super().__init__(status=0, reason=None, has_variation=0)
        # Continue notifying after cancellation to exercise Python suppression.
        self.phases = [1, 1, 2, 2, 3, 3, 4, 5]
        self.created: List[int] = []
        self.freed: List[int] = []

    def benchset_create(self) -> int:
        handle = len(self.created) + 1
        self.created.append(handle)
        return handle

    def benchset_cancel(self, handle: int) -> None:
        assert handle in self.created
        assert handle not in self.freed
        self.cancelled += 1

    def benchset_free(self, handle: int) -> None:
        assert handle in self.created
        assert handle not in self.freed
        self.freed.append(handle)


@pytest.mark.parametrize('benchmark', ['single_core', 'multi_core'])
@pytest.mark.parametrize('unstable', [False, True])
@pytest.mark.parametrize('status', [0, 1, 2])
@pytest.mark.parametrize('reason', [None, b'', 'Причина'.encode()])
@pytest.mark.parametrize('has_variation', [0, 1])
def test_ctypes_preserves_every_result_sample_and_progress_field(  # noqa: PLR0913 — Independent native field matrix.
    monkeypatch: pytest.MonkeyPatch, benchmark: str, unstable: bool, status: int,
    reason: Optional[bytes], has_variation: int,
) -> None:
    """Preserve native fields, including unsigned counters and double sample times.

    Sample timestamps needing double precision expose a float32 wire regression
    that whole seconds and halves conceal even when the remaining layout is intact.
    """
    native = Native()
    library = ResultLibrary(status, reason, has_variation)
    library.value.unstable = int(unstable)
    monkeypatch.setattr(native, 'lib', library)
    events: List[Progress] = []
    result = native.execute(123, benchmark, 0.75, 1.25, events.append)
    assert_result_references(result)
    assert result.benchmark == benchmark
    assert result.status == ['completed', 'cancelled', 'failed'][status]
    assert result.success == (status == 0)
    assert result.reason == (reason.decode() if reason else None)
    assert result.score == (123.5 if status == 0 else None)
    assert result.pi_equivalent == (0.375 if status == 0 else None)
    assert result.workers == 2**32 - 1
    assert (result.warmup_seconds, result.measurement_seconds, result.elapsed_seconds) == (2.5, 3.5, 4.5)
    assert (result.iterations, result.checksum) == (2**64 - 1, 2**64 - 2)
    assert result.variation_percent == (0 if has_variation else None)
    assert result.unstable is unstable
    assert result.samples == (Sample(4.5123456789012345, 1.5, 2**64 - 1, 42.123456789012345),
                              Sample(5.5123456789012345, 2.5, 2**64 - 2, 43.123456789012345))
    assert [event.phase for event in events] == ['warmup', 'measurement', 'completed', 'cancelled', 'failed']
    assert all((p.benchmark, p.workers, p.fraction, p.phase_elapsed_seconds,
                p.phase_duration_seconds, p.iterations) == (benchmark, 2**32 - 1, 0.12345678901234567,
                                                           1.2345678901234567, 2.345678901234567, 2**64 - 1) for p in events)
    assert library.config is not None
    assert (library.config.abi_version, library.config.struct_size, library.config.benchmark,
            library.config.reserved, library.config.warmup_seconds, library.config.measurement_seconds) == (1, 32, 1 if benchmark == 'single_core' else 2, 0, 0.75, 1.25)


@pytest.mark.parametrize('status', [0, 1, 2])
@pytest.mark.parametrize('present', [0, 1, 7, 2**32 - 1])
def test_variation_mapping_preserves_double_precision_and_nonzero_presence(
    monkeypatch: pytest.MonkeyPatch, status: int, present: int,
) -> None:
    native = Native()
    library = ResultLibrary(status, b'partial diagnostics', present)
    variation = 12.345678901234567
    library.value.variation_percent = variation
    monkeypatch.setattr(native, 'lib', library)
    result = native.execute(123, 'single_core', 0, 1, None)
    assert_result_references(result)
    assert result.status == ['completed', 'cancelled', 'failed'][status]
    assert result.variation_percent == (variation if present else None)


def test_unavailable_variation_does_not_imply_instability(monkeypatch: pytest.MonkeyPatch) -> None:
    native = Native()
    library = ResultLibrary(0, None, 0)
    library.value.unstable = 0
    library.value.sample_count = 1
    monkeypatch.setattr(native, 'lib', library)
    result = native.execute(123, 'multi_core', 0, 1, None)
    assert_result_references(result)
    assert result.success
    assert result.variation_percent is None
    unrun = BenchmarkResult('single_core')
    assert unrun.status == 'failed'
    assert unrun.reason is None
    assert unrun.score is None
    assert unrun.unit == 'iterations/s'
    assert unrun.pi_equivalent is None
    assert not result.unstable


@pytest.mark.parametrize(('platform', 'filename'), [('darwin', 'libbenchset.dylib'), ('win32', 'benchset.dll'), ('linux', 'libbenchset.so')])
def test_native_platform_library_selection(monkeypatch: pytest.MonkeyPatch, platform: str, filename: str) -> None:
    library = Native().lib
    paths: List[str] = []

    def load(path: str) -> ctypes.CDLL:
        paths.append(path)
        return library
    monkeypatch.setattr(sys, 'platform', platform)
    monkeypatch.setattr(ctypes, 'CDLL', load)
    Native()
    assert Path(paths[0]).name == filename
    assert library.benchset_run.restype is ctypes.c_uint32
    assert library.benchset_run.argtypes == [ctypes.c_void_p, ctypes.POINTER(Config), Callback, ctypes.c_void_p]


@pytest.mark.parametrize('native_version', [b'1.9.9', b'2.0.1', b'other-version'])
def test_native_package_version_mismatch_is_independent_of_abi(
    monkeypatch: pytest.MonkeyPatch, native_version: bytes,
) -> None:
    """Reject both stale and newer binaries even when their C ABI still matches.

    A fixed wrapper version makes the two mismatch directions independent of
    the project's next release and catches one-sided compatibility checks.
    """
    library = Native().lib
    monkeypatch.setattr('benchset._native.__version__', '2.0.0')
    monkeypatch.setattr(library, 'benchset_version', lambda: native_version)
    monkeypatch.setattr(ctypes, 'CDLL', lambda _path: library)
    with pytest.raises(RuntimeError, match='version/ABI'):
        Native()


@pytest.mark.parametrize('status', ['completed', 'cancelled', 'failed'])
def test_models_are_immutable_with_independent_result_defaults(status: str) -> None:
    """Keep untouched diagnostics empty until work supplies actual measurements.

    Immutable default records must not invent CPU work, elapsed time, checksums,
    or instability; each result owns an independent empty sample tuple.
    """
    result = BenchmarkResult('single_core', status=status)
    assert_result_references(result)
    assert result.success == (status == 'completed')
    for value, field in [(result, 'score'), (Sample(1, 1, 8, 8), 'iterations'), (Progress('single_core', 'warmup', 1, 0, 0, 1, 8), 'fraction')]:
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, 0)
    assert result.samples == BenchmarkResult('multi_core').samples == ()
    assert (result.workers, result.elapsed_seconds, result.iterations, result.checksum) == (0, 0.0, 0, 0)
    assert result.unstable is False
    assert result.variation_percent is None


@pytest.mark.parametrize(('stop_index', 'status'), [(0, 'cancelled'), (0, 'failed'), (1, 'cancelled'), (1, 'failed'), (1, 'completed')])
def test_suite_order_forwarding_and_early_stop(monkeypatch: pytest.MonkeyPatch, stop_index: int, status: str) -> None:
    names: List[str] = []
    callback = lambda _progress: None

    def execute(name: str, *, warmup_seconds: float, measurement_seconds: float,
                on_progress: Optional[Callable[[Progress], None]]) -> BenchmarkResult:
        assert (warmup_seconds, measurement_seconds, on_progress) == (0.75, 1.25, callback)
        names.append(name)
        return BenchmarkResult(name, status=status if len(names) - 1 == stop_index else 'completed')
    monkeypatch.setattr(api, 'run', execute)
    results = run_all(warmup_seconds=0.75, measurement_seconds=1.25, on_progress=callback).results
    for result in results:
        assert_result_references(result)
    assert names == ['single_core', 'multi_core'][:stop_index + 1]
    assert [result.benchmark for result in results] == names


@pytest.mark.parametrize('arguments', [['--warmup'], ['--duration'], ['--warmup', 'inf'], ['--duration', '-inf'], ['--duration', 'oops'], ['unexpected']])
def test_cli_invalid_settings_restore_argv(arguments: List[str]) -> None:
    original = sys.argv
    with pytest.raises(SystemExit) as error:
        cli.settings(arguments)
    assert error.value.code == 2
    assert sys.argv is original


def test_cli_settings_repeated_calls_and_system_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    original = ['benchset', '--bench', 'multi_core', '--warmup', '0', '--duration', '0.1']
    monkeypatch.setattr(sys, 'argv', original)
    assert cli.settings(original[1:]).bench == 'multi_core'
    assert cli.settings([]).bench == 'all'
    assert sys.argv is original
    backend = ManagedNative()
    monkeypatch.setattr(api, 'Native', lambda: backend)
    assert cli.main() == 0
    assert backend.calls == ['create', 'execute', 'free']


@pytest.mark.parametrize(('status', 'code'), [('completed', 0), ('cancelled', 130), ('failed', 1)])
def test_cli_exit_stream_and_early_stop(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], status: str, code: int) -> None:
    calls: List[str] = []

    def execute(_self: BenchmarkRunner, name: str, **_kwargs: object) -> BenchmarkResult:
        calls.append(name)
        return BenchmarkResult(name, status=status, reason='diagnostic', score=80 if status == 'completed' else None,
                               variation_percent=0 if status == 'completed' else None,
                               samples=(Sample(1, 1, 80, 80),) if status == 'completed' else ())
    monkeypatch.setattr(BenchmarkRunner, 'run', execute)
    assert cli.main(['--warmup', '0', '--duration', '0.1']) == code
    captured = capsys.readouterr()
    assert calls == (['single_core', 'multi_core'] if status == 'completed' else ['single_core'])
    assert bool(captured.out) == (status == 'completed')
    assert bool(captured.err) == (status != 'completed')
    assert 'diagnostic' in captured.out + captured.err


def test_cli_render_preserves_semantic_fields_and_empty_optionals() -> None:
    result = BenchmarkResult('multi_core', version='mapping-test', status='completed', reason='Причина',
                             score=123.5, pi_equivalent=1.125, workers=7,
                             warmup_seconds=0.75, measurement_seconds=1.25, elapsed_seconds=4.5,
                             iterations=2**64 - 1, checksum=2**64 - 2, variation_percent=0,
                             unstable=True, samples=(Sample(4.5, 1.5, 999, 666), Sample(6.75, 2.25, 40, 17.777)))
    output = io.StringIO()
    cli.render(result, output)
    fields, samples = output.getvalue().split('Samples:\n')
    assert {name.strip(): value for name, value in (line.split(' : ', 1) for line in fields.splitlines())} == {
        'Benchmark': 'multi_core',
        'Version': 'mapping-test',
        'Status': 'completed',
        'Success': 'True',
        'Reason': 'Причина',
        'Score': '123.500',
        'Unit': 'iterations/s',
        'Reference machine': 'Raspberry Pi 5 Model B Rev 1.0 (pi)',
        'Pi equivalent': '1.125 \u00d7 pi',
        'Workers': '7',
        'Warmup seconds': '0.75',
        'Measurement seconds': '1.25',
        'Actual measurement seconds': '4.500000',
        'Iterations': '18446744073709551615',
        'Checksum': '18446744073709551614',
        'Variation percent': '0.000',
        'Unstable': 'True',
    }
    assert [[cell.strip() for cell in row.split('|')] for row in samples.splitlines()] == [
        ['elapsed s', 'duration s', 'iterations', 'iterations/s'],
        ['4.500000', '1.500000', '999', '666.000'],
        ['6.750000', '2.250000', '40', '17.777'],
    ]
    output = io.StringIO()
    cli.render(replace(result, score=None, reason=None, variation_percent=None, samples=()), output)
    assert '—' in output.getvalue()
    assert '123.500' not in output.getvalue()


@pytest.mark.parametrize(('field', 'value', 'error'), [
    ('warmup_seconds', 'not a number', ValueError),
    ('measurement_seconds', None, TypeError),
    ('measurement_seconds', 10**1000, OverflowError),
])
def test_invalid_python_types_raise_before_start_and_do_not_poison_runner(
    field: str, value: object, error: Type[Exception],
) -> None:
    runner = BenchmarkRunner()
    # Non-numeric objects violate the typed API: conversion errors are raised.
    with pytest.raises(error):
        runner.run('single_core', **{field: value})  # type: ignore[arg-type]
    assert_runner_reusable(runner)


@pytest.mark.parametrize('benchmark', ['single_core', 'multi_core'])
def test_cli_selected_benchmark_runs_once(monkeypatch: pytest.MonkeyPatch, benchmark: str) -> None:
    names: List[str] = []

    def execute(_self: BenchmarkRunner, name: str, **_kwargs: object) -> BenchmarkResult:
        names.append(name)
        return BenchmarkResult(name, status='completed', score=1)
    monkeypatch.setattr(BenchmarkRunner, 'run', execute)
    assert cli.main(['--bench', benchmark, '--warmup', '0', '--duration', '0.1']) == 0
    assert names == [benchmark]


class RepeatedInterrupt(Event):
    def __init__(self) -> None:
        super().__init__()
        self.interruptions = 0

    def is_set(self) -> bool:
        return self.interruptions >= 2 and super().is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        if self.interruptions < 2:
            self.interruptions += 1
            raise KeyboardInterrupt
        return super().wait(timeout)


def test_cli_repeated_interrupt_has_priority_over_completed_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, 'Event', RepeatedInterrupt)

    def execute(_self: BenchmarkRunner, name: str, **_kwargs: object) -> BenchmarkResult:
        return BenchmarkResult(name, status='completed', score=1)
    monkeypatch.setattr(BenchmarkRunner, 'run', execute)
    assert cli.main(['--bench', 'single_core']) == 130
    captured = capsys.readouterr()
    assert not captured.out
    assert 'completed' in captured.err


class BrokenProgressOutput(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.failed_once = False

    def isatty(self) -> bool:
        return True

    def write(self, value: str) -> int:
        if value.startswith('\r') and not self.failed_once:
            self.failed_once = True
            raise OSError('terminal disconnected')
        return super().write(value)


@pytest.mark.parametrize('columns', ['1', '10', '80'])
def test_cli_output_callback_error_is_reported_as_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], columns: str,
) -> None:
    stream = BrokenProgressOutput()
    monkeypatch.setenv('COLUMNS', columns)
    monkeypatch.setattr(cli.sys, 'stdout', stream)
    assert cli.main(['--bench', 'single_core', '--warmup', '0', '--duration', '.03']) == 1
    assert stream.failed_once
    assert 'terminal disconnected' in capsys.readouterr().err


def test_cancel_racing_cleanup_never_reuses_freed_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    freeing, release, cancelling = Event(), Event(), Event()

    class SlowFree(ManagedNative):
        def free(self, handle: int) -> None:
            super().free(handle)
            freeing.set()
            assert release.wait(5)
    backend = SlowFree()
    monkeypatch.setattr(api, 'Native', lambda: backend)
    runner = BenchmarkRunner()
    results: List[BenchmarkResult] = []
    execute = Thread(target=lambda: results.append(runner.run('single_core', warmup_seconds=0, measurement_seconds=0.1)))

    def cancel() -> None:
        cancelling.set()
        runner.cancel()
    cancel_thread = Thread(target=cancel)
    execute.start()
    try:
        assert freeing.wait(5)
        cancel_thread.start()
        assert cancelling.wait(5)
    finally:
        release.set()
        execute.join(5)
        if cancel_thread.ident is not None:
            cancel_thread.join(5)
    assert not execute.is_alive()
    assert not cancel_thread.is_alive()
    assert results[0].success
    assert_result_references(results[0])
    assert backend.calls == ['create', 'execute', 'free']


@pytest.mark.parametrize('argument', ['--help', '--version'])
def test_cli_information_does_not_initialize_native(monkeypatch: pytest.MonkeyPatch, argument: str) -> None:
    def forbidden() -> Native:
        pytest.fail('informational CLI commands must not load native code')
    monkeypatch.setattr(api, 'Native', forbidden)
    assert cli.main([argument]) == 0


def test_zero_score_and_variation_are_present_values(monkeypatch: pytest.MonkeyPatch) -> None:
    native = Native()
    library = ResultLibrary(0, None, 1)
    library.value.score = 0
    monkeypatch.setattr(native, 'lib', library)
    result = native.execute(123, 'multi_core', 0, 1, None)
    assert_result_references(result)
    assert result.score == result.variation_percent == 0
    output = io.StringIO()
    cli.render(result, output)
    assert '—' not in next(line for line in output.getvalue().splitlines() if line.startswith('Score'))
    assert '0.000' in next(line for line in output.getvalue().splitlines() if line.startswith('Score'))
