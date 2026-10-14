import ctypes
import math
import os
import signal
import subprocess
import sys
from dataclasses import FrozenInstanceError
from threading import Event, Thread
from typing import List

import pytest

from benchset import BenchmarkRunner, Progress, __version__, run, run_all
from benchset._native import ABI_VERSION, Config, Native


def test_measurement_and_samples() -> None:
    progress: List[Progress] = []
    result = run('single_core', warmup_seconds=0.05, measurement_seconds=1.1,
                 on_progress=progress.append)
    assert result.success, result.reason
    assert result.version == __version__
    assert result.workers == 1
    assert result.score is not None
    assert result.score > 0
    assert result.elapsed_seconds >= 1.1
    assert result.score == result.iterations / result.elapsed_seconds
    assert sum(sample.iterations for sample in result.samples) == result.iterations
    assert sum(sample.duration_seconds for sample in result.samples) == pytest.approx(result.elapsed_seconds)
    assert len(result.samples) >= 2
    assert result.variation_percent is not None
    assert result.unstable == (result.variation_percent > 10)
    assert progress[0].phase == 'warmup'
    assert any(item.phase == 'measurement' for item in progress)
    assert progress[-1].phase == 'completed'
    assert progress[-1].fraction == 1
    assert [item.fraction for item in progress] == sorted(item.fraction for item in progress)
    with pytest.raises(FrozenInstanceError):
        result.score = 0  # type: ignore[misc]


def test_all_benchmarks() -> None:
    results = run_all(warmup_seconds=0, measurement_seconds=0.08)
    assert [result.benchmark for result in results] == ['single_core', 'multi_core']
    assert all(result.success for result in results)
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

    result = runner.run('single_core', warmup_seconds=0.1, measurement_seconds=0.2, on_progress=progress)
    assert result.status == 'cancelled'
    assert result.score is None
    assert result.reason == 'cancelled'
    assert events[-1].phase == 'cancelled'
    runner.cancel()  # A finished runner may still be cancelled safely.
    assert runner.run('single_core', warmup_seconds=0, measurement_seconds=0.03).success


def test_callback_exception_does_not_cross_c() -> None:
    def broken(_progress: Progress) -> None:
        raise ValueError('intentional callback error')
    result = run('single_core', warmup_seconds=1, measurement_seconds=1, on_progress=broken)
    assert result.status == 'failed'
    assert result.score is None
    assert result.reason is not None
    assert 'intentional callback error' in result.reason


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
        assert not runner.run('single_core', warmup_seconds=0, measurement_seconds=0.01).success
        other = run('single_core', warmup_seconds=0, measurement_seconds=0.01)
        assert other.reason == 'another benchmark is already running'
    finally:
        runner.cancel()
        thread.join(5)
    assert not thread.is_alive()
    assert results[0].status == 'cancelled'


@pytest.mark.parametrize(('name', 'warmup', 'duration'), [
    ('unknown', 0, 1), ('single_core', -1, 1), ('single_core', math.nan, 1),
    ('single_core', 0, 0), ('single_core', 0, math.inf),
])
def test_invalid_options(name: str, warmup: float, duration: float) -> None:
    result = run(name, warmup_seconds=warmup, measurement_seconds=duration)
    assert not result.success
    assert result.score is None
    assert result.reason


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
        assert result.status == 'cancelled'
        assert result.score is None
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


def test_cli_success() -> None:
    result = subprocess.run([sys.executable, '-m', 'benchset', '--warmup', '0', '--duration', '0.03'],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    for field in ('single_core', 'multi_core', 'Version', 'Samples', 'Score', 'Unstable', 'Checksum'):
        assert field in result.stdout


@pytest.mark.skipif(os.name == 'nt', reason='Windows console Ctrl+C is tested through native cancellation')
def test_cli_sigint() -> None:
    import pty  # noqa: PLC0415
    import select  # noqa: PLC0415
    import time  # noqa: PLC0415

    master, slave = pty.openpty()
    process = subprocess.Popen([sys.executable, '-m', 'benchset', '--warmup', '20', '--duration', '1'],
                               stdout=slave, stderr=subprocess.PIPE, text=True)
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
