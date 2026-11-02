"""Aggregate actual tuple contents independently of overridden tuple protocols."""

import sys
from typing import Any, List, Tuple

import pytest

from benchset import BenchmarkResult, SuiteResult


def _valid_results() -> Tuple[BenchmarkResult, ...]:
    return (BenchmarkResult('single_core', status='completed', pi_equivalent=4.0),
            BenchmarkResult('multi_core', status='completed', pi_equivalent=9.0))


@pytest.mark.parametrize('protocol', [
    'getitem-error', 'getitem-lie', 'len-error', 'len-zero', 'len-one', 'len-large',
    'iter-error', 'iter-empty', 'iter-wrong', 'iter-changing',
])
def test_tuple_subclass_protocols_cannot_change_its_actual_aggregate(protocol: str) -> None:
    """Use actual tuple elements and length, as explicitly chosen by the caller.

    Tuple subclasses can override iteration, indexing and length independently.
    None of those hooks may run during aggregation: lying values must not alter
    a genuine complete suite, and raising hooks must not replace its score.
    Keep the caller's original object in SuiteResult.results.
    """
    calls: List[str] = []
    fake = (BenchmarkResult('wrong', status='completed', pi_equivalent=100.0),)

    class Results(tuple):
        __slots__ = ()

        def __getitem__(self, index: Any) -> Any:
            if protocol.startswith('getitem'):
                calls.append('getitem')
                if protocol == 'getitem-error':
                    raise ValueError('overridden tuple indexing must not run')
                return fake[0]
            return tuple.__getitem__(self, index)

        def __len__(self) -> int:
            if protocol.startswith('len'):
                calls.append('len')
                if protocol == 'len-error':
                    raise ValueError('overridden tuple length must not run')
                return {'len-zero': 0, 'len-one': 1, 'len-large': sys.maxsize}[protocol]
            return tuple.__len__(self)

        def __iter__(self) -> Any:
            if protocol.startswith('iter'):
                calls.append('iter')
                if protocol == 'iter-error':
                    raise ValueError('overridden tuple iteration must not run')
                if protocol == 'iter-empty':
                    return iter(())
                if protocol == 'iter-changing' and len(calls) > 1:
                    return tuple.__iter__(self)
                return iter(fake)
            return tuple.__iter__(self)

    results = Results(_valid_results())
    suite = SuiteResult.from_results(results)
    assert suite.results is results
    assert suite.aggregated_scores.pi_equivalent == 6.0
    assert calls == []


@pytest.mark.parametrize('contents', ['empty', 'short', 'extra', 'reversed', 'wrong-name', 'failed', 'missing-ratio'])
def test_tuple_subclass_cannot_hide_invalid_actual_contents_with_valid_protocols(contents: str) -> None:
    """An invalid actual tuple remains unavailable even when all hooks fake a suite.

    Reusing an overridden iterator's snapshot would silently validate fictional
    results. Rust must receive the actual empty, incomplete, reordered, failed
    or coefficient-less tuple while retaining the original public container.
    """
    valid = _valid_results()
    actual = {
        'empty': (),
        'short': valid[:1],
        'extra': (*valid, valid[0]),
        'reversed': valid[::-1],
        'wrong-name': (BenchmarkResult('wrong', status='completed', pi_equivalent=4.0), valid[1]),
        'failed': (BenchmarkResult('single_core', status='failed', pi_equivalent=4.0), valid[1]),
        'missing-ratio': (BenchmarkResult('single_core', status='completed'), valid[1]),
    }[contents]
    calls: List[str] = []

    class Results(tuple):
        __slots__ = ()

        def __getitem__(self, index: Any) -> Any:
            calls.append('getitem')
            return valid[index]

        def __len__(self) -> int:
            calls.append('len')
            return len(valid)

        def __iter__(self) -> Any:
            calls.append('iter')
            return iter(valid)

    results = Results(actual)
    suite = SuiteResult.from_results(results)
    assert suite.results is results
    assert suite.aggregated_scores.pi_equivalent is None
    assert calls == []


def test_normalizing_tuple_inputs_does_not_reject_existing_list_inputs() -> None:
    """Restrict the new protocol bypass to tuples without narrowing list behavior."""
    results = list(_valid_results())
    suite = SuiteResult.from_results(results)  # type: ignore[arg-type]
    assert suite.results is results
    assert suite.aggregated_scores.pi_equivalent == 6.0
