"""Preserve caller exception graphs when a native callback transports an existing error."""

from typing import Any, Callable, Dict, NoReturn, Optional, Type

import pytest

from benchset import BenchmarkResult, BenchmarkRunner, SuiteResult, api, run_all


class _GuardExceptionAttributes:
    def __getattribute__(self, name: str) -> Any:
        if name in ('__context__', '__cause__', '__suppress_context__'):
            raise AssertionError('exception attribute reader must not be called')
        return super().__getattribute__(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in ('__context__', '__cause__', '__suppress_context__'):
            raise AssertionError('exception attribute writer must not be called')
        super().__setattr__(name, value)


class _GuardedValueError(_GuardExceptionAttributes, ValueError):
    pass


class _GuardedRuntimeError(_GuardExceptionAttributes, RuntimeError):
    pass


class _GuardedKeyboardInterrupt(_GuardExceptionAttributes, KeyboardInterrupt):
    pass


_GUARDED: Dict[Type[BaseException], Type[BaseException]] = {
    ValueError: _GuardedValueError,
    RuntimeError: _GuardedRuntimeError,
    KeyboardInterrupt: _GuardedKeyboardInterrupt,
}


def _field(error: BaseException, name: str) -> Any:
    return BaseException.__dict__[name].__get__(error)


def _check_exception_graph(
    operation: Callable[[Callable[[], NoReturn]], object],
    error_type: Type[BaseException], chain: str, guarded: bool,
) -> None:
    outer = LookupError('outer')
    inner = ValueError('inner')
    cause = TypeError('explicit cause')
    error_class = _GUARDED[error_type] if guarded else error_type
    original = error_class('caller error')
    caught: Optional[BaseException] = None
    returned: object = None

    def raise_caller_error() -> NoReturn:
        try:
            raise inner
        except ValueError:
            if chain == 'explicit':
                raise original from cause
            if chain == 'suppressed':
                raise original from None
            raise original  # noqa: B904 — The implicit context is the behavior under test.

    try:
        raise outer
    except LookupError:
        try:
            returned = operation(raise_caller_error)
        except BaseException as error:  # noqa: BLE001 — Assert identity without formatting caller-owned exception chains.
            caught = error
    assert caught is original, returned
    assert _field(original, 'args') == ('caller error',)
    assert _field(original, '__context__') is inner
    assert _field(inner, '__context__') is outer
    assert _field(original, '__cause__') is (cause if chain == 'explicit' else None)
    assert _field(original, '__suppress_context__') == (chain != 'implicit')


@pytest.mark.parametrize('window', ['warmup', 'measurement'])
@pytest.mark.parametrize('error_type', [ValueError, RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize('chain', ['implicit', 'explicit', 'suppressed'])
@pytest.mark.parametrize('guarded', [False, True])
def test_suite_second_conversion_keeps_caller_exception_graph(
    monkeypatch: pytest.MonkeyPatch, window: str, error_type: Type[BaseException], chain: str, guarded: bool,
) -> None:
    """An enclosing caller except must not replace the inner converter context after suite cleanup."""
    runner = BenchmarkRunner()
    monkeypatch.setattr(api, 'BenchmarkRunner', lambda: runner)

    def operation(raise_error: Callable[[], NoReturn]) -> object:
        class Seconds(float):
            calls = 0

            def __float__(self) -> float:
                self.calls += 1
                if self.calls == 2:
                    raise_error()
                return float.__float__(self)

        return run_all(warmup_seconds=Seconds(0) if window == 'warmup' else 0,
                       measurement_seconds=Seconds(.1) if window == 'measurement' else .1)

    _check_exception_graph(operation, error_type, chain, guarded)
    assert runner.run('single_core', warmup_seconds=0, measurement_seconds=.01).success


@pytest.mark.parametrize('phase', ['validation', 'execution'])
@pytest.mark.parametrize('chain', ['implicit', 'explicit', 'suppressed'])
@pytest.mark.parametrize('guarded', [False, True])
def test_name_matcher_keeps_caller_exception_graph(phase: str, chain: str, guarded: bool) -> None:
    """Both name-comparison boundaries retain the user's graph before or after handle allocation."""
    runner = BenchmarkRunner()

    def operation(raise_error: Callable[[], NoReturn]) -> object:
        class Name(str):
            __slots__ = ('__dict__',)
            __hash__ = str.__hash__
            calls = 0

            def __eq__(self, other: object) -> bool:
                self.calls += 1
                if self.calls == (1 if phase == 'validation' else 2):
                    raise_error()
                return str.__eq__(self, other)

        return runner.run(Name('single_core'), warmup_seconds=0, measurement_seconds=.01)

    _check_exception_graph(operation, ValueError, chain, guarded)
    assert runner.run('single_core', warmup_seconds=0, measurement_seconds=.01).success


@pytest.mark.parametrize('primitive', ['success', 'ratio', 'name', 'ge', 'eq'])
@pytest.mark.parametrize('chain', ['implicit', 'explicit', 'suppressed'])
@pytest.mark.parametrize('guarded', [False, True])
def test_aggregate_callback_keeps_caller_exception_graph(primitive: str, chain: str, guarded: bool) -> None:
    """Lazy aggregate readers rethrow each caller property/operator with its complete original graph."""
    def operation(raise_error: Callable[[], NoReturn]) -> object:
        class Result(BenchmarkResult):
            @property
            def success(self) -> bool:
                if primitive == 'success':
                    raise_error()
                return super().success

            def __getattribute__(self, name: str) -> Any:
                if primitive == 'ratio' and name == 'pi_equivalent':
                    raise_error()
                return super().__getattribute__(name)

        class Name(str):
            __slots__ = ()
            __hash__ = str.__hash__

            def __eq__(self, other: object) -> bool:
                raise_error()

        class Ratio(float):
            __hash__ = float.__hash__

            def __ge__(self, other: float) -> bool:
                if primitive == 'ge':
                    raise_error()
                return float.__ge__(self, other)

            def __eq__(self, other: object) -> bool:
                raise_error()

        name = Name('single_core') if primitive == 'name' else 'single_core'
        ratio = Ratio(1) if primitive in ('ge', 'eq') else 1
        return SuiteResult.from_results((Result(name, status='completed', pi_equivalent=ratio),
                                         BenchmarkResult('multi_core', status='completed', pi_equivalent=4)))

    _check_exception_graph(operation, ValueError, chain, guarded)
    valid = (BenchmarkResult('single_core', status='completed', pi_equivalent=1),
             BenchmarkResult('multi_core', status='completed', pi_equivalent=4))
    assert SuiteResult.from_results(valid).aggregated_scores.pi_equivalent == 2
