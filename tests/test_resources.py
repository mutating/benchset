"""Resource ownership, including failures with cyclic garbage collection off."""

import gc
import weakref
from types import TracebackType
from typing import Any, Callable, Iterator, Optional, Type

import pytest

from benchset import BenchmarkRunner, Progress, api
from benchset._native import Native
from tests.helpers import assert_runner_reusable
from tests.test_semantics import CallbackLibrary, ManagedNative


@pytest.fixture
def without_cyclic_gc() -> Iterator[None]:
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()
        gc.collect()


class CallbackProbe:
    def __init__(self, error_type: Optional[Type[BaseException]], cancel: Optional[BenchmarkRunner] = None) -> None:
        self.error_type = error_type
        self.cancel = cancel
        self.payload = bytearray(64 * 1024)

    def __call__(self, _progress: Progress) -> None:
        if self.error_type is not None:
            raise self.error_type('callback resource probe')
        if self.cancel is not None:
            self.cancel.cancel()


@pytest.mark.usefixtures('without_cyclic_gc')
@pytest.mark.parametrize('error_type', [None, ValueError, KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize('stage', ['complete', 'run', 'result', 'sample'])
def test_callback_capture_released_without_gc_even_after_native_failure(
    monkeypatch: pytest.MonkeyPatch, error_type: Optional[Type[BaseException]], stage: str,
) -> None:
    native = Native()
    library = CallbackLibrary()
    monkeypatch.setattr(native, 'lib', library)
    monkeypatch.setattr(api, 'Native', lambda: native)
    if stage == 'run':
        original_run = library.benchset_run

        def broken_run(handle: int, config: Any, callback: Callable[..., None], user: object) -> int:
            original_run(handle, config, callback, user)
            raise RuntimeError('native run failed after callback')
        monkeypatch.setattr(library, 'benchset_run', broken_run)
    elif stage != 'complete':
        getter = 'benchset_result_v2' if stage == 'result' else 'benchset_sample'
        monkeypatch.setattr(library, getter, lambda *_arguments: 0)

    callback = CallbackProbe(error_type)
    observed = weakref.ref(callback)
    result = BenchmarkRunner().run('single_core', warmup_seconds=0, measurement_seconds=0.01, on_progress=callback)
    del callback
    assert result.status == ('completed' if error_type is None and stage == 'complete' else 'failed')
    assert result.pi_equivalent == (0.375 if result.success else None)
    assert library.created == library.freed == [1]
    assert observed() is None, 'a completed run must not retain the callback or its captured data'


@pytest.mark.usefixtures('without_cyclic_gc')
@pytest.mark.parametrize('outcome', ['completed', 'cancelled', 'failed'])
def test_live_callback_capture_released_without_gc(outcome: str) -> None:
    runner = BenchmarkRunner()
    callback = CallbackProbe(ValueError if outcome == 'failed' else None, runner if outcome == 'cancelled' else None)
    observed = weakref.ref(callback)
    result = runner.run('single_core', warmup_seconds=0, measurement_seconds=0.02, on_progress=callback)
    del callback
    assert result.status == outcome
    assert (result.pi_equivalent is not None) == (outcome == 'completed')
    assert observed() is None
    assert_runner_reusable(runner)


@pytest.mark.parametrize('error_type', [KeyboardInterrupt, RuntimeError])
def test_handle_freed_if_interrupted_before_publication(
    monkeypatch: pytest.MonkeyPatch, error_type: Type[BaseException],
) -> None:
    backend = ManagedNative()
    monkeypatch.setattr(api, 'Native', lambda: backend)
    runner = BenchmarkRunner()

    class InterruptedLock:
        entries = 0

        def __enter__(self) -> None:
            self.entries += 1
            if self.entries == 2:
                raise error_type('interrupted after allocation')

        def __exit__(
            self, _kind: Optional[Type[BaseException]], _error: Optional[BaseException],
            _traceback: Optional[TracebackType],
        ) -> None:
            pass

    monkeypatch.setattr(runner, '_lock', InterruptedLock())
    if error_type is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            runner.run('single_core', warmup_seconds=0, measurement_seconds=0.01)
    else:
        result = runner.run('single_core', warmup_seconds=0, measurement_seconds=0.01)
        assert result.reason == 'interrupted after allocation'
    assert backend.calls == ['create', 'free']
    runner.cancel()
    assert runner.run('single_core', warmup_seconds=0, measurement_seconds=0.01).success
    assert backend.calls == ['create', 'free', 'create', 'execute', 'free']
