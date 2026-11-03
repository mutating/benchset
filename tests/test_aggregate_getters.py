"""Preserve staged public ratio reads instead of caching caller-owned fields."""

import math
from typing import Any, List, Optional, Tuple, Type, Union

import pytest

from benchset import BenchmarkResult, SuiteResult

_Read = Union[float, None, BaseException]
_STAGES = ('presence', 'finite', 'ge', 'zero', 'log-guard', 'log-value')
_ORDER = (
    'first:success', 'first:presence', 'first:finite', 'first:ge',
    'second:success', 'second:presence', 'second:finite', 'second:ge',
    'first:zero', 'second:zero', 'first:log-guard', 'first:log-value',
    'second:log-guard', 'second:log-value',
)


def _result(
    name: str, label: str, values: Tuple[_Read, ...], events: List[str], *, status: str = 'completed',
) -> BenchmarkResult:
    reads = 0

    class Result(BenchmarkResult):
        @property
        def success(self) -> bool:
            events.append(label + ':success')
            return self.status == 'completed'

        def __getattribute__(self, attribute: str) -> Any:
            nonlocal reads
            if attribute == 'pi_equivalent':
                assert reads < len(values), 'an unused ratio getter was evaluated'
                events.append(label + ':' + _STAGES[reads])
                value = values[reads]
                reads += 1
                if isinstance(value, BaseException):
                    raise value
                return value
            return super().__getattribute__(attribute)

    return Result(name, status=status, pi_equivalent=1.0)


def _aggregate(first: BenchmarkResult, second: BenchmarkResult) -> Optional[float]:
    return SuiteResult.from_results((first, second)).aggregated_scores.pi_equivalent


def test_ratio_getters_keep_validation_zero_and_logarithm_stages_in_order() -> None:
    """Each public read remains observable and only the final value enters log.

    HEAD reads presence, finiteness and >= separately for each result, scans
    every zero before logging, then independently reads the None guard and log
    operand. Distinct ordinary floats expose any cached-value substitution.
    """
    events: List[str] = []
    first = _result('single_core', 'first', (11.0, 17.0, 19.0, 23.0, 29.0, 4.0), events)
    second = _result('multi_core', 'second', (31.0, 37.0, 41.0, 43.0, 47.0, 9.0), events)
    assert _aggregate(first, second) == pytest.approx(6.0)
    assert tuple(events) == _ORDER


@pytest.mark.parametrize('position', [0, 1])
def test_later_zero_getter_controls_the_aggregate_and_hides_all_logs(position: int) -> None:
    """A zero discovered by the later zero scan overrides earlier positive reads."""
    events: List[str] = []
    values = [(1.0, 1.0, 1.0, 1.0), (4.0, 4.0, 4.0, 4.0)]
    values[position] = (*values[position][:3], 0.0)
    first = _result('single_core', 'first', values[0], events)
    second = _result('multi_core', 'second', values[1], events)
    assert _aggregate(first, second) == 0.0
    assert tuple(events) == _ORDER[:9 + position]


@pytest.mark.parametrize('skip', [(0,), (1,), (0, 1)])
def test_none_log_guard_skips_only_its_own_log_value(skip: Tuple[int, ...]) -> None:
    """A field may become None after validation and zero checks without a sixth read.

    The original log generator skips that term while retaining the full suite
    denominator. Both absent guards therefore produce exp(0 / 2), or one.
    """
    events: List[str] = []
    values: List[Tuple[_Read, ...]] = [(4.0,) * 6, (9.0,) * 6]
    for position in skip:
        values[position] = (*values[position][:4], None)
    first = _result('single_core', 'first', values[0], events)
    second = _result('multi_core', 'second', values[1], events)
    expected = math.exp(sum(math.log(number) for position, number in enumerate((4.0, 9.0))
                            if position not in skip) / 2)
    assert _aggregate(first, second) == pytest.approx(expected)
    omitted = {('first' if position == 0 else 'second') + ':log-value' for position in skip}
    assert events == [event for event in _ORDER if event not in omitted]


@pytest.mark.parametrize('position', [0, 1])
@pytest.mark.parametrize('stage', _STAGES)
@pytest.mark.parametrize('error_type', [ValueError, RuntimeError, OverflowError, OSError,
                                      KeyboardInterrupt, SystemExit, GeneratorExit])
def test_ratio_getter_exceptions_keep_identity_at_every_original_read(
    position: int, stage: str, error_type: Type[BaseException],
) -> None:
    """Every staged getter can raise; no cache may move, hide or replace its error."""
    events: List[str] = []
    original = error_type('ratio getter failed at ' + stage)
    values: List[Tuple[_Read, ...]] = [(2.0,) * 6, (2.0,) * 6]
    changed = list(values[position])
    changed[_STAGES.index(stage)] = original
    values[position] = tuple(changed)
    first = _result('single_core', 'first', values[0], events)
    second = _result('multi_core', 'second', values[1], events)
    with pytest.raises(error_type, match='ratio getter failed at ' + stage) as caught:
        _aggregate(first, second)
    assert caught.value is original
    boundary = ('first' if position == 0 else 'second') + ':' + stage
    assert tuple(events) == _ORDER[:_ORDER.index(boundary) + 1]


@pytest.mark.parametrize(('case', 'expected_events'), [
    ('incomplete', ()),
    ('reordered', ()),
    ('failed-first', _ORDER[:1]),
    ('failed-second', _ORDER[:5]),
    ('missing-first', _ORDER[:2]),
    ('missing-second', _ORDER[:6]),
    ('nonfinite-first', _ORDER[:3]),
    ('nonfinite-second', _ORDER[:7]),
    ('negative-first', _ORDER[:4]),
    ('negative-second', _ORDER[:8]),
])
def test_rejection_hides_every_unused_ratio_getter(case: str, expected_events: Tuple[str, ...]) -> None:
    """Structural/status/presence/finite/sign rejection stops at its original read.

    Each result supplies only the values HEAD actually needs; an additional
    getter raises immediately instead of silently returning a convenient value.
    """
    events: List[str] = []
    values: List[Tuple[_Read, ...]] = [(1.0, 1.0, 1.0), (4.0, 4.0, 4.0)]
    if case.startswith('missing'):
        values[0 if case.endswith('first') else 1] = (None,)
    elif case.startswith('nonfinite'):
        values[0 if case.endswith('first') else 1] = (1.0, math.nan)
    elif case.startswith('negative'):
        values[0 if case.endswith('first') else 1] = (1.0, 1.0, -1.0)
    first = _result('single_core', 'first', values[0], events,
                    status='failed' if case == 'failed-first' else 'completed')
    second = _result('multi_core', 'second', values[1], events,
                     status='failed' if case == 'failed-second' else 'completed')
    results = (first,) if case == 'incomplete' else ((second, first) if case == 'reordered' else (first, second))
    assert SuiteResult.from_results(results).aggregated_scores.pi_equivalent is None
    assert tuple(events) == expected_events
