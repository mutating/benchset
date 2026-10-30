import ctypes
import locale
import math
import os
import signal
import subprocess
import sys
from dataclasses import FrozenInstanceError
from threading import Event, Thread
from typing import Dict, List

import pytest

from benchset import (
    PI_REFERENCE_RUN,
    BenchmarkRunner,
    Progress,
    __version__,
    run,
    run_all,
)
from benchset._native import ABI_VERSION, Config, Native
from tests.helpers import assert_result_references, assert_runner_reusable


def test_measurement_and_samples() -> None:
    progress: List[Progress] = []
    result = run('single_core', warmup_seconds=0.05, measurement_seconds=1.1,
                 on_progress=progress.append)
    assert_result_references(result)
    assert result.success, result.reason
    assert result.version == __version__
    assert result.workers == 1
    assert result.score is not None
    assert result.score > 0
    assert result.elapsed_seconds >= 1.1
    assert result.score == result.iterations / result.elapsed_seconds
    reference = next(value.score for value in PI_REFERENCE_RUN.results if value.benchmark == result.benchmark)
    assert result.pi_equivalent == result.score / reference
    assert sum(sample.iterations for sample in result.samples) == result.iterations
    assert sum(sample.duration_seconds for sample in result.samples) == pytest.approx(result.elapsed_seconds)
    # A delayed coordinator can miss the intermediate one-second snapshot.
    # Exact multi-sample statistics and transport are checked with fixed inputs.
    assert result.samples
    assert (result.variation_percent is not None) == (len(result.samples) >= 2)
    assert result.unstable == (result.variation_percent is not None and result.variation_percent > 10)
    assert progress[0].phase in ('warmup', 'measurement')
    assert any(item.phase == 'measurement' for item in progress)
    assert progress[-1].phase == 'completed'
    assert progress[-1].fraction == 1
    assert progress[-1].phase_elapsed_seconds == result.elapsed_seconds
    assert [item.fraction for item in progress] == sorted(item.fraction for item in progress)
    with pytest.raises(FrozenInstanceError):
        result.score = 0  # type: ignore[misc]


def test_all_benchmarks() -> None:
    results = run_all(warmup_seconds=0, measurement_seconds=0.08).results
    assert [result.benchmark for result in results] == ['single_core', 'multi_core']
    assert all(result.success for result in results)
    references = {value.benchmark: value.score for value in PI_REFERENCE_RUN.results}
    for result in results:
        assert_result_references(result)
        assert result.score is not None
        assert result.pi_equivalent == result.score / references[result.benchmark]
    assert results[1].workers >= 1
    assert results[0].variation_percent is None


@pytest.mark.parametrize('phase', ['warmup', 'measurement'])
def test_cancel_and_reuse(phase: str) -> None:
    runner = BenchmarkRunner()
    events: List[Progress] = []

    def progress(value: Progress) -> None:
        events.append(value)
        if value.phase == phase:
            runner.cancel()

    # Start in the phase under test; cancellation does not wait for a short
    # warmup-to-measurement transition that the scheduler can skip observing.
    warmup = 10 if phase == 'warmup' else 0
    result = runner.run('single_core', warmup_seconds=warmup, measurement_seconds=0.2, on_progress=progress)
    assert_result_references(result)
    assert result.status == 'cancelled'
    assert result.score is None
    assert result.pi_equivalent is None
    assert result.reason == 'cancelled'
    assert events[-1].phase == 'cancelled'
    runner.cancel()  # A finished runner may still be cancelled safely.
    assert_runner_reusable(runner)


@pytest.mark.parametrize('warmup', [0, 1])
def test_callback_exception_does_not_cross_c(warmup: float) -> None:
    def broken(_progress: Progress) -> None:
        raise ValueError('intentional callback error')
    runner = BenchmarkRunner()
    result = runner.run('single_core', warmup_seconds=warmup, measurement_seconds=1, on_progress=broken)
    assert_result_references(result)
    assert result.status == 'failed'
    assert result.score is None
    assert result.pi_equivalent is None
    assert result.reason is not None
    assert 'intentional callback error' in result.reason
    assert_runner_reusable(runner)


def test_busy_and_external_cancellation() -> None:
    runner = BenchmarkRunner()
    started = Event()
    results = []

    def execute() -> None:
        results.append(runner.run('multi_core', warmup_seconds=10, measurement_seconds=10,
                                  on_progress=lambda _value: started.set()))
    thread = Thread(target=execute)
    thread.start()
    try:
        assert started.wait(5)
        same = runner.run('single_core', warmup_seconds=0, measurement_seconds=0.01)
        assert_result_references(same)
        assert not same.success
        other = run('single_core', warmup_seconds=0, measurement_seconds=0.01)
        assert_result_references(other)
        assert other.reason == 'another benchmark is already running'
    finally:
        runner.cancel()
        thread.join(5)
    assert not thread.is_alive()
    assert results[0].status == 'cancelled'
    assert_result_references(results[0])


@pytest.mark.parametrize(('name', 'warmup', 'duration'), [
    ('unknown', 0, 1), ('single_core', -1, 1), ('single_core', math.nan, 1),
    ('single_core', 0, 0), ('single_core', 0, math.inf),
])
def test_invalid_options(name: str, warmup: float, duration: float) -> None:
    """Reject invalid options with a reason that identifies the caller's mistake.

    Returning only the unknown name, or a generic failure, leaves users unable
    to distinguish an unsupported benchmark from invalid timing parameters.
    """
    result = run(name, warmup_seconds=warmup, measurement_seconds=duration)
    assert_result_references(result)
    assert not result.success
    assert result.score is None
    assert result.pi_equivalent is None
    assert result.reason
    if name == 'unknown':
        assert 'benchmark' in result.reason
        assert name in result.reason


def test_native_invalid_abi_and_pre_cancel() -> None:
    native = Native()
    handle = native.create()
    try:
        config = Config(ABI_VERSION + 1, ctypes.sizeof(Config), 1, 0, 0, 0.01)
        from benchset._native import Callback  # noqa: PLC0415
        assert native.lib.benchset_run(handle, ctypes.byref(config), Callback(), None) == 2
        assert b'incompatible' in native.lib.benchset_reason(handle)
    finally:
        native.free(handle)
    handle = native.create()
    try:
        native.cancel(handle)
        result = native.execute(handle, 'single_core', 0, 0.01, None)
        assert_result_references(result)
        assert result.status == 'cancelled'
        assert result.score is None
        assert result.pi_equivalent is None
    finally:
        native.free(handle)


@pytest.mark.parametrize(('arguments', 'code'), [
    (['--help'], 0), (['--version'], 0), (['--unknown'], 2),
    (['--bench'], 2), (['--bench', 'missing'], 2),
    (['--duration', '0'], 2), (['--duration', 'nan'], 2),
    (['--warmup', '-1'], 2), (['--warmup', 'oops'], 2),
])
def test_cli_validation(arguments: List[str], code: int) -> None:
    result = subprocess.run([sys.executable, '-m', 'benchset', *arguments], capture_output=True, text=True, check=False)
    assert result.returncode == code
    if code:
        assert result.stderr
        assert not result.stdout


@pytest.fixture
def cli_environment(monkeypatch: pytest.MonkeyPatch) -> Dict[str, str]:
    # Exercise a non-UTF-8 parent independently of the interpreter's UTF-8 mode.
    monkeypatch.setattr(subprocess, '_text_encoding', lambda: 'ascii', raising=False)
    monkeypatch.setattr(locale, 'getpreferredencoding', lambda _setlocale=True: 'ascii')
    return dict(os.environ, PYTHONIOENCODING='utf-8')


def test_cli_success(cli_environment: Dict[str, str]) -> None:
    result = subprocess.run([sys.executable, '-m', 'benchset', '--warmup', '0', '--duration', '0.03'],
                            env=cli_environment, capture_output=True, encoding='utf-8', check=False)
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    for field in ('single_core', 'multi_core', 'Version', 'Samples', 'Score', 'Unstable', 'Checksum'):
        assert field in result.stdout


@pytest.mark.skipif(os.name == 'nt', reason='Windows console Ctrl+C is tested through native cancellation')
def test_cli_sigint(cli_environment: Dict[str, str]) -> None:
    import pty  # noqa: PLC0415
    import select  # noqa: PLC0415
    import time  # noqa: PLC0415

    master, slave = pty.openpty()
    process = subprocess.Popen([sys.executable, '-m', 'benchset', '--warmup', '20', '--duration', '1'],
                               env=dict(cli_environment, COLUMNS='80'), stdout=slave, stderr=subprocess.PIPE, encoding='utf-8')
    os.close(slave)
    try:
        # Wait for native warmup rather than assuming interpreter startup latency.
        observed = b''
        deadline = time.monotonic() + 10
        while b'warmup' not in observed and time.monotonic() < deadline:
            readable, _writable, _errors = select.select([master], [], [], 0.1)
            if readable:
                observed += os.read(master, 4096)
        assert b'warmup' in observed
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 130
        assert not stdout
        assert 'cancelled' in stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
