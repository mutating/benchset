"""Caller string operators in unknown-benchmark diagnostics retain Python semantics."""

from typing import List, Type

import pytest

from benchset import BenchmarkRunner
from benchset._native import Native


def test_unknown_name_concatenation_preserves_prefix_and_result() -> None:
    """The caller receives the original prefix and its returned string is stored by identity."""
    calls: List[str] = []

    class Diagnostic(str):
        __slots__ = ()

    diagnostic = Diagnostic('caller-defined diagnostic')

    class Name(str):
        __slots__ = ()

        def __radd__(self, prefix: str) -> str:
            calls.append(prefix)
            return diagnostic

    name = Name('unknown')
    result = BenchmarkRunner().run(name, warmup_seconds=0, measurement_seconds=0.01)
    assert result.status == 'failed'
    assert result.benchmark is name
    assert result.reason is diagnostic
    assert calls == ['unknown benchmark: ']


@pytest.mark.parametrize('error_type', [RuntimeError, OSError, OverflowError, ValueError,
                                      KeyboardInterrupt, SystemExit, GeneratorExit])
def test_unknown_name_concatenation_preserves_exception_and_runner_reuse(error_type: Type[BaseException]) -> None:
    """Diagnostic operators preserve exception identity/context and leave the runner reusable."""
    calls: List[str] = []
    original = error_type('caller concatenation failed')
    context = LookupError('caller context')

    class Name(str):
        __slots__ = ()

        def __radd__(self, prefix: str) -> str:
            calls.append(prefix)
            try:
                raise context
            except LookupError:
                raise original from context

    runner = BenchmarkRunner()
    with pytest.raises(error_type, match='caller concatenation failed') as caught:
        runner.run(Name('unknown'), warmup_seconds=0, measurement_seconds=0.01)
    assert caught.value is original
    assert original.__context__ is context
    assert original.__cause__ is context
    assert original.__suppress_context__
    assert calls == ['unknown benchmark: ']
    result = runner.run('single_core', warmup_seconds=0, measurement_seconds=0.01,
                        on_progress=lambda _progress: runner.cancel())
    assert result.status == 'cancelled'


@pytest.mark.parametrize('benchmark', ['single_core', 'multi_core'])
def test_valid_names_do_not_evaluate_diagnostic_concatenation(benchmark: str) -> None:
    """A valid name never invokes an unused diagnostic operator, including on cancellation."""
    class Name(str):
        __slots__ = ()

        def __radd__(self, prefix: str) -> str:
            raise AssertionError('valid names must not create diagnostics')

    runner = BenchmarkRunner()
    result = runner.run(Name(benchmark), warmup_seconds=0, measurement_seconds=0.01,
                        on_progress=lambda _progress: runner.cancel())
    assert result.status == 'cancelled'


@pytest.mark.parametrize('error_type', [RuntimeError, OSError, OverflowError, ValueError,
                                      KeyboardInterrupt, SystemExit, GeneratorExit])
def test_native_diagnostic_without_carrier_preserves_original_exception(error_type: Type[BaseException]) -> None:
    """The new private validation boundary directly re-raises diagnostics when no carrier is requested."""
    original = error_type('direct diagnostic failed')
    context = LookupError('direct caller context')
    calls: List[str] = []

    class Name(str):
        __slots__ = ()

        def __radd__(self, prefix: str) -> str:
            calls.append(prefix)
            try:
                raise context
            except LookupError:
                raise original from context

    native = Native()
    with pytest.raises(error_type, match='direct diagnostic failed') as caught:
        native.validation_error(Name('unknown'), 0, 0.01)
    assert caught.value is original
    assert original.__context__ is context
    assert original.__cause__ is context
    assert original.__suppress_context__
    assert calls == ['unknown benchmark: ']
