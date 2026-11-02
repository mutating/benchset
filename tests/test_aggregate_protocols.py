"""Characterize float-subclass operators at the public aggregate boundary."""

import math
from typing import Any, Callable, List, Optional, Tuple, Type

import pytest

from benchset import BenchmarkResult, BenchmarkRunner, SuiteResult, _native


def ratio(
    number: float, name: str, calls: List[str], *,
    ge: Optional[Callable[[], bool]] = None, eq: Optional[Callable[[], bool]] = None,
) -> float:
    class Ratio(float):
        __slots__ = ()
        __hash__ = float.__hash__

        def __ge__(self, other: float) -> bool:
            assert type(other) is int
            assert other == 0
            calls.append(name + ':ge')
            return float.__ge__(self, other) if ge is None else ge()

        def __eq__(self, other: object) -> bool:
            assert type(other) is int
            assert other == 0
            calls.append(name + ':eq')
            return float.__eq__(self, other) if eq is None else eq()

    return Ratio(number)


def aggregate(first: float, second: float) -> Optional[float]:
    values = (BenchmarkResult('single_core', status='completed', pi_equivalent=first),
              BenchmarkResult('multi_core', status='completed', pi_equivalent=second))
    return SuiteResult.from_results(values).aggregated_scores.pi_equivalent


@pytest.mark.parametrize(('ge', 'eq', 'expected'), [(False, False, None), (True, True, 0.0)])
def test_float_operators_control_validation_and_zero(ge: bool, eq: bool, expected: Optional[float]) -> None:
    """The original >= and == operators, with integer zero, decide rejection and zero."""
    calls: List[str] = []
    first = ratio(1, 'first', calls, ge=lambda: ge, eq=lambda: eq)
    assert aggregate(first, 4) == expected
    assert calls == (['first:ge', 'first:eq'] if ge else ['first:ge'])


@pytest.mark.parametrize('number', [math.nan, math.inf, -math.inf])
def test_nonfinite_float_skips_overridden_operators(number: float) -> None:
    """math.isfinite rejects a nonfinite float before either overridden operator runs."""
    calls: List[str] = []
    assert aggregate(ratio(number, 'first', calls), 4) is None
    assert calls == []


@pytest.mark.parametrize(('stage', 'error_type'), [
    (stage, error_type) for stage in ('ge', 'eq')
    for error_type in (ValueError, RuntimeError, OverflowError, KeyboardInterrupt, SystemExit, GeneratorExit)
])
def test_float_operator_exceptions_keep_identity(stage: str, error_type: Type[BaseException]) -> None:
    """Operator exceptions return from the C callback as the exact original host object."""
    original = error_type('custom float operator failed')
    calls: List[str] = []

    def fail() -> bool:
        raise original

    value = ratio(1, 'first', calls, **{stage: fail})
    with pytest.raises(error_type, match='custom float operator failed') as caught:
        aggregate(value, 4)
    assert caught.value is original
    assert calls == (['first:ge'] if stage == 'ge' else ['first:ge', 'first:eq'])


@pytest.mark.parametrize('second_valid', [False, True])
def test_zero_operators_follow_complete_validation_and_short_circuit(second_valid: bool) -> None:
    """Validate both coefficients before zero checks, then stop after the first true equality."""
    calls: List[str] = []

    def forbidden() -> bool:
        raise AssertionError('later zero operator must not run')

    first = ratio(1, 'first', calls, eq=lambda: True)
    second = ratio(4, 'second', calls, ge=lambda: second_valid, eq=forbidden)
    assert aggregate(first, second) == (0 if second_valid else None)
    assert calls == ['first:ge', 'second:ge'] + (['first:eq'] if second_valid else [])


@pytest.mark.parametrize('number', [-1.0, 0.0, -0.0])
def test_custom_operators_preserve_nonpositive_log_domain_errors(number: float) -> None:
    """A nonpositive number accepted by custom operators still raises math.log's ValueError."""
    calls: List[str] = []
    with pytest.raises(ValueError, match=r'.+') as expected:
        math.log(number)
    first = ratio(number, 'first', calls, ge=lambda: True, eq=lambda: False)
    with pytest.raises(ValueError, match=r'.+') as caught:
        aggregate(first, ratio(4, 'second', calls))
    assert type(caught.value) is type(expected.value)
    assert str(caught.value) == str(expected.value)
    assert calls == ['first:ge', 'second:ge', 'first:eq', 'second:eq']


def test_later_custom_zero_skips_an_earlier_nonpositive_log() -> None:
    """The complete zero scan hides an earlier invalid logarithm when a later equality is true."""
    calls: List[str] = []
    first = ratio(-1, 'first', calls, ge=lambda: True, eq=lambda: False)
    second = ratio(4, 'second', calls, eq=lambda: True)
    assert aggregate(first, second) == 0
    assert calls == ['first:ge', 'second:ge', 'first:eq', 'second:eq']


@pytest.mark.parametrize(('matches', 'count'), [(False, 2), (True, 2), (True, 1), (True, 3)])
def test_aggregate_names_compare_lazily_at_their_positions(matches: bool, count: int) -> None:
    """Tuple equality compares the shared prefix before rejecting a different length."""
    calls: List[str] = []

    class Name(str):
        __slots__ = ()
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
            calls.append(str(other))
            return matches

    names = [Name('single_core'), Name('multi_core'), Name('extra')][:count]
    values = tuple(BenchmarkResult(name, status='completed', pi_equivalent=2) for name in names)
    value = SuiteResult.from_results(values).aggregated_scores.pi_equivalent
    assert value == (2 if count == 2 and matches else None)
    assert calls == (['single_core', 'multi_core'][:min(count, 2)] if matches else ['single_core'])


def test_aggregate_names_can_match_both_positions_without_matching_their_bytes() -> None:
    """A global first matching ID cannot replace positional tuple equality for subclass names."""
    class Name(str):
        __slots__ = ()
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
            return True

    values = (BenchmarkResult(Name('anything'), status='completed', pi_equivalent=1),
              BenchmarkResult(Name('anything'), status='completed', pi_equivalent=4))
    assert SuiteResult.from_results(values).aggregated_scores.pi_equivalent == 2


@pytest.mark.parametrize('error_type', [ValueError, RuntimeError, OverflowError, OSError,
                                      KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize('factory', ['aggregate', 'run'])
def test_name_comparison_exceptions_keep_identity(error_type: Type[BaseException], factory: str) -> None:
    """Name validation preserves the original comparison exception, including runner validation."""
    original = error_type('custom name comparison failed')

    class Name(str):
        __slots__ = ()
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
            raise original

    def invoke() -> None:
        if factory == 'aggregate':
            values = (BenchmarkResult(Name('single_core'), status='completed', pi_equivalent=1),
                      BenchmarkResult('multi_core', status='completed', pi_equivalent=4))
            SuiteResult.from_results(values)
        else:
            BenchmarkRunner().run(Name('single_core'), warmup_seconds=0, measurement_seconds=0.01)

    with pytest.raises(error_type, match='custom name comparison failed') as caught:
        invoke()
    assert caught.value is original


@pytest.mark.parametrize('matches', [False, True])
def test_runner_name_comparisons_keep_validation_and_execution_order(matches: bool) -> None:
    """Membership validates first; execution compares single_core once and never hashes the name."""
    calls: List[str] = []

    class Name(str):
        __slots__ = ()

        def __eq__(self, other: object) -> bool:
            calls.append(str(other))
            return matches

        def __hash__(self) -> int:
            raise AssertionError('benchmark lookup must not hash caller names')

    runner = BenchmarkRunner()
    result = runner.run(Name('multi_core'), warmup_seconds=0, measurement_seconds=0.01,
                        on_progress=lambda _progress: runner.cancel())
    assert calls == (['single_core', 'single_core'] if matches else ['single_core', 'multi_core'])
    assert result.status == ('cancelled' if matches else 'failed')
    if matches:
        assert result.workers == 1
    else:
        assert result.reason == 'unknown benchmark: multi_core'


def test_runner_execution_compares_once_before_multicore_fallback() -> None:
    """A stateful name accepted in validation reaches the old single comparison/fallback execution path."""
    calls: List[str] = []

    class Name(str):
        __slots__ = ()
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
            calls.append(str(other))
            if len(calls) > 2:
                raise AssertionError('execution must not compare a second benchmark')
            return len(calls) == 1

    runner = BenchmarkRunner()
    result = runner.run(Name('multi_core'), warmup_seconds=0, measurement_seconds=0.01,
                        on_progress=lambda _progress: runner.cancel())
    assert calls == ['single_core', 'single_core']
    assert result.status == 'cancelled'


@pytest.mark.parametrize('error_type', [RuntimeError, OverflowError, OSError, ValueError])
def test_execution_name_errors_keep_runner_failure_handling(error_type: Type[Exception]) -> None:
    """Execution comparisons retain the runner's existing caught/propagated exception boundary."""
    original = error_type('execution name failed')
    calls: List[str] = []

    class Name(str):
        __slots__ = ()
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
            calls.append(str(other))
            if len(calls) == 1:
                return True
            raise original

    runner = BenchmarkRunner()
    if error_type is ValueError:
        with pytest.raises(ValueError, match='execution name failed') as caught:
            runner.run(Name('single_core'), warmup_seconds=0, measurement_seconds=0.01)
        assert caught.value is original
    else:
        result = runner.run(Name('single_core'), warmup_seconds=0, measurement_seconds=0.01)
        assert result.status == 'failed'
        assert result.reason == 'execution name failed'
    assert calls == ['single_core', 'single_core']


@pytest.mark.parametrize('case', ['complete', 'extra', 'first-mismatch', 'comparison-error'])
def test_aggregate_materializes_all_name_attributes_before_comparing(case: str) -> None:
    """The factory snapshots every name before tuple equality can reject or raise."""
    calls: List[str] = []
    original = ValueError('name comparison failed')

    def name(text: str, label: str) -> str:
        class Name(str):
            __slots__ = ()
            __hash__ = str.__hash__

            def __eq__(self, other: object) -> bool:
                calls.append('eq:' + label)
                if case == 'comparison-error':
                    raise original
                return False if case == 'first-mismatch' else str.__eq__(self, other)

        return Name(text)

    class Result(BenchmarkResult):
        def __getattribute__(self, attribute: str) -> Any:
            if attribute == 'benchmark':
                calls.append('get:' + object.__getattribute__(self, 'reason'))
            return object.__getattribute__(self, attribute)

    values: Tuple[BenchmarkResult, ...] = (
        Result(name('single_core', 'first'), reason='first', status='completed', pi_equivalent=2),
        Result(name('multi_core', 'second'), reason='second', status='completed', pi_equivalent=2),
    )
    if case == 'extra':
        values += (Result(name('extra', 'third'), reason='third', status='completed', pi_equivalent=2),)
    if case == 'comparison-error':
        with pytest.raises(ValueError, match='name comparison failed') as caught:
            SuiteResult.from_results(values)
        assert caught.value is original
    else:
        suite = SuiteResult.from_results(values)
        assert suite.aggregated_scores.pi_equivalent == (2 if case == 'complete' else None)
    expected = ['get:first', 'get:second'] + (['get:third'] if case == 'extra' else [])
    expected += ['eq:first'] if case in ('first-mismatch', 'comparison-error') else ['eq:first', 'eq:second']
    assert calls == expected


@pytest.mark.parametrize('position', [0, 1])
@pytest.mark.parametrize('error_type', [ValueError, RuntimeError, OverflowError,
                                      KeyboardInterrupt, SystemExit, GeneratorExit])
def test_aggregate_name_getter_errors_precede_native_loading(
    monkeypatch: pytest.MonkeyPatch, position: int, error_type: Type[BaseException],
) -> None:
    """Name snapshots preserve getter exception identity before native availability matters."""
    calls: List[str] = []
    original = error_type('name getter failed')

    class Result(BenchmarkResult):
        def __getattribute__(self, attribute: str) -> Any:
            if attribute == 'benchmark':
                label = object.__getattribute__(self, 'reason')
                calls.append('get:' + label)
                if label == str(position):
                    raise original
            return object.__getattribute__(self, attribute)

    def unavailable() -> None:
        calls.append('native')
        raise OSError('native loader failed')

    monkeypatch.setattr(_native, 'Native', unavailable)
    values = (Result('single_core', reason='0', status='completed', pi_equivalent=1),
              Result('multi_core', reason='1', status='completed', pi_equivalent=4))
    with pytest.raises(error_type, match='name getter failed') as caught:
        SuiteResult.from_results(values)
    assert caught.value is original
    assert calls == ['get:' + str(index) for index in range(position + 1)]
