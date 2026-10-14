"""Behavioral boundaries beyond line coverage: ownership, callbacks and decoding."""

import ctypes
import io
import math
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from threading import Event, Thread, get_ident
from typing import Any, Callable, List, Optional, Type

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
    ABI_VERSION,
    Callback,
    Config,
    CProgress,
    CResult,
    CSample,
    Native,
)


@pytest.mark.parametrize(('warmup', 'measurement'), [
    (math.nan, 1), (math.inf, 1), (-math.inf, 1), (-1, 1),
    (0, math.nan), (0, math.inf), (0, -math.inf), (0, -1), (0, -0.0),
])
def test_python_validation_does_not_allocate_native(
    monkeypatch: pytest.MonkeyPatch, warmup: float, measurement: float,
) -> None:
    def forbidden() -> Native:
        pytest.fail('invalid options must not load native code')
    monkeypatch.setattr(api, 'Native', forbidden)
    result = run('single_core', warmup_seconds=warmup, measurement_seconds=measurement)
    assert result.status == 'failed'
    assert result.score is None
    assert result.reason
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
    assert result.status == 'failed'
    assert result.score is None
    assert result.warmup_seconds == warmup
    assert result.measurement_seconds == measurement
    assert result.reason


@pytest.mark.parametrize('phase', ['warmup', 'measurement', 'completed'])
@pytest.mark.parametrize('error_type', [ValueError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_callback_base_exceptions_stop_notifications_and_allow_reuse(
    monkeypatch: pytest.MonkeyPatch, phase: str, error_type: Type[BaseException],
) -> None:
    native = Native()
    library = CallbackLibrary()
    monkeypatch.setattr(native, 'lib', library)
    monkeypatch.setattr(api, 'Native', lambda: native)
    runner = BenchmarkRunner()
    events: List[Progress] = []
    caller = get_ident()

    def callback(progress: Progress) -> None:
        assert get_ident() == caller
        events.append(progress)
        if progress.phase == phase:
            raise error_type('callback boundary')

    result = runner.run('single_core', warmup_seconds=0.02, measurement_seconds=0.04, on_progress=callback)
    assert result.status == 'failed'
    assert result.score is None
    assert result.reason == f'progress callback failed: {error_type.__name__}: callback boundary'
    assert events[-1].phase == phase
    assert sum(event.phase == phase for event in events) == 1
    assert library.cancelled == 1
    assert library.freed == [1]
    if phase == 'completed':
        assert result.iterations > 0
        assert result.samples
    assert runner.run('single_core', warmup_seconds=0, measurement_seconds=0.02).success
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
        assert same.reason == 'this runner is already running'
        other = run('multi_core', warmup_seconds=0, measurement_seconds=0.01)
        assert other.reason == 'another benchmark is already running'
    assert runner.run('single_core', warmup_seconds=-0.0, measurement_seconds=0.03, on_progress=callback).success
    assert checked
    runner.cancel()
    assert runner.run('single_core', warmup_seconds=0, measurement_seconds=0.02).success


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
    assert result.status == 'failed'
    assert result.reason == stage + ' failure'
    assert backend.calls.count('free') == (1 if stage == 'execute' else 0)
    fail_init = False
    backend.error_at = None
    assert runner.run('single_core', warmup_seconds=0, measurement_seconds=0.1).success
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
    assert backend.calls == ['create', 'cancel', 'execute', 'free']
    runner.cancel()
    assert backend.calls[-1] == 'free'


class ResultLibrary:
    def __init__(self, status: int, reason: Optional[bytes], has_variation: int) -> None:
        self.value = CResult(ABI_VERSION, status, 2, 2**32 - 1, 123.5, 2.5, 3.5, 4.5, 0,
                             2**64 - 1, 2**64 - 2, 2, 7, has_variation)
        self.reason = reason
        self.config: Optional[Config] = None
        self.cancelled = 0
        self.phases = [1, 2, 3, 4, 5]

    def benchset_run(self, _handle: int, config: Any, callback: Callable[..., None], _user: object) -> int:
        raw = ctypes.cast(config, ctypes.POINTER(Config)).contents
        self.config = Config.from_buffer_copy(raw)
        for phase in self.phases:
            progress = CProgress(phase, 2**32 - 1, 0.375, 1.25, 2.5, 2**64 - 1)
            callback(ctypes.pointer(progress), None)
        return self.value.status

    def benchset_result(self, _handle: int, output: Any) -> int:
        ctypes.memmove(output, ctypes.byref(self.value), ctypes.sizeof(self.value))
        return 1

    def benchset_sample(self, _handle: int, index: int, output: Any) -> int:
        sample = CSample(4.5 + index, 1.5 + index, 2**64 - 1 - index, 42.5 + index)
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


@pytest.mark.parametrize('status', [0, 1, 2])
@pytest.mark.parametrize('reason', [None, b'', 'Причина'.encode()])
@pytest.mark.parametrize('has_variation', [0, 1])
def test_ctypes_preserves_every_result_sample_and_progress_field(
    monkeypatch: pytest.MonkeyPatch, status: int, reason: Optional[bytes], has_variation: int,
) -> None:
    native = Native()
    library = ResultLibrary(status, reason, has_variation)
    monkeypatch.setattr(native, 'lib', library)
    events: List[Progress] = []
    result = native.execute(123, 'multi_core', 0.75, 1.25, events.append)
    assert result.benchmark == 'multi_core'
    assert result.status == ['completed', 'cancelled', 'failed'][status]
    assert result.success == (status == 0)
    assert result.reason == (reason.decode() if reason else None)
    assert result.score == (123.5 if status == 0 else None)
    assert result.workers == 2**32 - 1
    assert (result.warmup_seconds, result.measurement_seconds, result.elapsed_seconds) == (2.5, 3.5, 4.5)
    assert (result.iterations, result.checksum) == (2**64 - 1, 2**64 - 2)
    assert result.variation_percent == (0 if has_variation else None)
    assert result.unstable
    assert result.samples == (Sample(4.5, 1.5, 2**64 - 1, 42.5), Sample(5.5, 2.5, 2**64 - 2, 43.5))
    assert [event.phase for event in events] == ['warmup', 'measurement', 'completed', 'cancelled', 'failed']
    assert all((p.benchmark, p.workers, p.fraction, p.phase_elapsed_seconds,
                p.phase_duration_seconds, p.iterations) == ('multi_core', 2**32 - 1, 0.375, 1.25, 2.5, 2**64 - 1) for p in events)
    assert library.config is not None
    assert (library.config.abi_version, library.config.struct_size, library.config.benchmark,
            library.config.reserved, library.config.warmup_seconds, library.config.measurement_seconds) == (1, 32, 2, 0, 0.75, 1.25)


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


def test_native_package_version_mismatch_is_independent_of_abi(monkeypatch: pytest.MonkeyPatch) -> None:
    library = Native().lib
    monkeypatch.setattr(library, 'benchset_version', lambda: b'other-version')
    monkeypatch.setattr(ctypes, 'CDLL', lambda _path: library)
    with pytest.raises(RuntimeError, match='version/ABI'):
        Native()


@pytest.mark.parametrize('status', ['completed', 'cancelled', 'failed'])
def test_models_are_immutable_with_independent_result_defaults(status: str) -> None:
    result = BenchmarkResult('single_core', status=status)
    assert result.success == (status == 'completed')
    for value, field in [(result, 'score'), (Sample(1, 1, 8, 8), 'iterations'), (Progress('single_core', 'warmup', 1, 0, 0, 1, 8), 'fraction')]:
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, 0)
    assert result.samples == BenchmarkResult('multi_core').samples == ()


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
    results = run_all(warmup_seconds=0.75, measurement_seconds=1.25, on_progress=callback)
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
    result = BenchmarkResult('multi_core', status='completed', reason='Причина', score=123.5, workers=7,
                             iterations=2**64 - 1, checksum=2**64 - 2, variation_percent=0,
                             samples=(Sample(4.5, 1.5, 999, 666),))
    output = io.StringIO()
    cli.render(result, output)
    for value in ['multi_core', 'Причина', '123.500', 'iterations/s', str(2**64 - 1), str(2**64 - 2), '4.500000', '1.500000', '999', '666.000']:
        assert value in output.getvalue()
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
    assert runner.run('single_core', warmup_seconds=0, measurement_seconds=0.02).success


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
        if 'warmup' in value and not self.failed_once:
            self.failed_once = True
            raise OSError('terminal disconnected')
        return super().write(value)


def test_cli_output_callback_error_is_reported_as_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    stream = BrokenProgressOutput()
    monkeypatch.setattr(cli.sys, 'stdout', stream)
    assert cli.main(['--bench', 'single_core', '--warmup', '1', '--duration', '1']) == 1
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
    assert result.score == result.variation_percent == 0
    output = io.StringIO()
    cli.render(result, output)
    assert '—' not in next(line for line in output.getvalue().splitlines() if line.startswith('Score'))
    assert '0.000' in next(line for line in output.getvalue().splitlines() if line.startswith('Score'))
