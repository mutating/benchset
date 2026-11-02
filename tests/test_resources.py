"""Resource ownership, including failures with cyclic garbage collection off."""

import gc
import weakref
from types import TracebackType
from typing import Any, Callable, Iterator, Optional, Type

import pytest

from benchset import (
    BenchmarkResult,
    BenchmarkRunner,
    Progress,
    SuiteResult,
    api,
    run_all,
)
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
@pytest.mark.parametrize('suite', [False, True])
def test_live_callback_capture_released_without_gc(outcome: str, suite: bool) -> None:
    """Single and suite paths release callback payloads after success, cancel or failure without GC."""
    runner = BenchmarkRunner()
    callback = CallbackProbe(ValueError if outcome == 'failed' else None, runner if outcome == 'cancelled' else None)
    observed = weakref.ref(callback)
    result = (runner._run_all(0, .02, callback).results[-1] if suite else
              runner.run('single_core', warmup_seconds=0, measurement_seconds=.02, on_progress=callback))
    del callback
    assert result.status == outcome
    assert (result.pi_equivalent is not None) == (outcome == 'completed')
    assert observed() is None
    assert_runner_reusable(runner)


@pytest.mark.usefixtures('without_cyclic_gc')
@pytest.mark.parametrize('getter', ['benchset_result_v2', 'benchset_sample', 'benchset_suite_result'])
@pytest.mark.parametrize('error_type', [None, ValueError, KeyboardInterrupt])
def test_suite_getter_failures_release_callback_captures_without_gc(
    monkeypatch: pytest.MonkeyPatch, getter: str, error_type: Optional[Type[BaseException]],
) -> None:
    """Getter failures cannot retain a suite callback through captured native exception frames."""
    native = Native()
    monkeypatch.setattr(api, 'Native', lambda: native)
    monkeypatch.setattr(native.lib, getter, lambda *_args: 0)
    callback = CallbackProbe(error_type)
    observed = weakref.ref(callback)
    suite = run_all(warmup_seconds=0, measurement_seconds=.02, on_progress=callback)
    del callback
    assert suite.results[-1].status == 'failed'
    assert suite.aggregated_scores.pi_equivalent is None
    assert observed() is None


@pytest.mark.usefixtures('without_cyclic_gc')
@pytest.mark.parametrize('error_type', [RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_result_observer_exceptions_wait_for_native_return_and_release_captures(
    monkeypatch: pytest.MonkeyPatch, error_type: Type[BaseException],
) -> None:
    """Result-observer errors cross C only after return, cancel the suite and release captures without GC."""
    native = Native()
    original_run, original_cancel = native.lib.benchset_run_all_configured, native.cancel
    returned, cancellations = [], []

    def execute(*args: Any) -> int:
        status = int(original_run(*args))
        returned.append(True)
        return status

    def cancel(handle: int, suite: bool = False) -> None:
        cancellations.append((handle, suite))
        original_cancel(handle, suite)

    class Observer:
        def __call__(self, _result: BenchmarkResult) -> bool:
            assert not returned, 'the observer runs before the native call returns'
            raise error_type('result observer failed')

    callback = Observer()
    observed = weakref.ref(callback)
    monkeypatch.setattr(native.lib, 'benchset_run_all_configured', execute)
    monkeypatch.setattr(native, 'cancel', cancel)
    handle = native.create(True)
    try:
        with pytest.raises(error_type, match='result observer failed'):
            native.execute_suite(handle, 0, .02, None, callback)
        assert returned == [True]
        assert cancellations == [(handle, True)]
    finally:
        native.free(handle, True)
    del callback
    assert observed() is None
    assert_runner_reusable(BenchmarkRunner())


@pytest.mark.parametrize('error_type', [KeyboardInterrupt, RuntimeError])
def test_handle_freed_if_interrupted_before_publication(
    monkeypatch: pytest.MonkeyPatch, error_type: Type[BaseException],
) -> None:
    """Interrupt the first lock entry after allocation, regardless of prior ownership checks.

    The guard fires once so cleanup can reacquire the lock and free the
    unpublished handle before the same runner is used again.
    """
    backend = ManagedNative()
    monkeypatch.setattr(api, 'Native', lambda: backend)
    runner = BenchmarkRunner()

    class InterruptedLock:
        armed = True

        def __enter__(self) -> None:
            if self.armed and backend.calls == ['create']:
                self.armed = False
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


def _fresh_error_payload(
    operation: Callable[[CallbackProbe], None], error_type: Type[BaseException],
) -> Callable[[], Optional[CallbackProbe]]:
    """Leave the complete exercising frame before checking a weak reference.

    Do not retain ExceptionInfo or the raised exception: either would itself
    own its traceback and make intentional caller retention look like a leak.
    """
    payload = CallbackProbe(None)
    observed = weakref.ref(payload)
    try:
        operation(payload)
    except error_type:
        pass
    else:
        pytest.fail('the caller primitive must raise its fresh exception')
    return observed


@pytest.mark.usefixtures('without_cyclic_gc')
@pytest.mark.parametrize('primitive', ['success', 'name', 'ratio', 'ge', 'eq'])
@pytest.mark.parametrize('error_type', [ValueError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_aggregate_host_errors_release_caller_payloads_without_gc(
    primitive: str, error_type: Type[BaseException],
) -> None:
    """Fresh aggregate property/operator errors must not retain inputs through bridge traceback cycles."""
    def operation(payload: CallbackProbe) -> None:
        class Result(BenchmarkResult):
            @property
            def success(self) -> bool:
                if primitive == 'success':
                    raise error_type('fresh aggregate host error')
                return super().success

            def __getattribute__(self, name: str) -> Any:
                if primitive == 'ratio' and name == 'pi_equivalent':
                    raise error_type('fresh aggregate host error')
                return super().__getattribute__(name)

        class Name(str):
            __slots__ = ('__dict__',)
            __hash__ = str.__hash__
            payload: CallbackProbe

            def __eq__(self, other: object) -> bool:
                raise error_type('fresh aggregate host error')

        class Ratio(float):
            __slots__ = ('__dict__',)
            __hash__ = float.__hash__
            payload: CallbackProbe

            def __ge__(self, other: float) -> bool:
                if primitive == 'ge':
                    raise error_type('fresh aggregate host error')
                return float.__ge__(self, other)

            def __eq__(self, other: object) -> bool:
                raise error_type('fresh aggregate host error')

        name = Name('single_core') if primitive == 'name' else 'single_core'
        if isinstance(name, Name):
            name.payload = payload
        value = Ratio(1) if primitive in ('ge', 'eq') else 1
        if isinstance(value, Ratio):
            value.payload = payload
        first = Result(name, status='completed', pi_equivalent=value)
        object.__setattr__(first, 'payload', payload)
        SuiteResult.from_results((first, BenchmarkResult('multi_core', status='completed', pi_equivalent=4)))

    for _attempt in range(3):
        observed = _fresh_error_payload(operation, error_type)
        assert observed() is None, 'a discarded aggregate error must not retain its caller payload'


@pytest.mark.usefixtures('without_cyclic_gc')
@pytest.mark.parametrize('primitive', ['validation', 'execution', 'diagnostic'])
@pytest.mark.parametrize('error_type', [ValueError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_name_host_errors_release_caller_payloads_without_gc(
    primitive: str, error_type: Type[BaseException],
) -> None:
    """Name comparisons and diagnostic concatenation release their payload after an unretained error."""
    def operation(payload: CallbackProbe) -> None:
        class Name(str):
            __slots__ = ('__dict__',)
            __hash__ = str.__hash__
            payload: CallbackProbe
            comparisons = 0

            def __eq__(self, other: object) -> bool:
                self.comparisons += 1
                if primitive == 'validation' or (primitive == 'execution' and self.comparisons == 2):
                    raise error_type('fresh name host error')
                return str.__eq__(self, other)

            def __radd__(self, _prefix: str) -> str:
                raise error_type('fresh name host error')

        name = Name('unknown' if primitive == 'diagnostic' else 'single_core')
        name.payload = payload
        BenchmarkRunner().run(name, warmup_seconds=0, measurement_seconds=.1)

    for _attempt in range(3):
        observed = _fresh_error_payload(operation, error_type)
        assert observed() is None, 'a discarded name error must not retain its caller payload'


@pytest.mark.usefixtures('without_cyclic_gc')
@pytest.mark.parametrize('window', ['warmup', 'measurement'])
@pytest.mark.parametrize('error_type', [ValueError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_suite_configuration_errors_release_caller_payloads_without_gc(
    window: str, error_type: Type[BaseException],
) -> None:
    """Second-child conversion errors release option payloads after suite cleanup without cyclic collection."""
    def operation(payload: CallbackProbe) -> None:
        class Seconds(float):
            __slots__ = ('__dict__',)
            payload: CallbackProbe
            conversions = 0

            def __float__(self) -> float:
                self.conversions += 1
                if self.conversions == 2:
                    raise error_type('fresh configuration host error')
                return float.__float__(self)

        seconds = Seconds(0 if window == 'warmup' else .1)
        seconds.payload = payload
        run_all(warmup_seconds=seconds if window == 'warmup' else 0,
                measurement_seconds=seconds if window == 'measurement' else .1)

    for _attempt in range(3):
        observed = _fresh_error_payload(operation, error_type)
        assert observed() is None, 'a discarded configuration error must not retain its caller payload'
