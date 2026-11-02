"""Bound the aggregation bridge's scratch storage without cyclic collection."""

import weakref
from typing import Any, Callable, List, Optional, Type

import pytest

from benchset import BenchmarkResult, SuiteResult, _native
from tests.test_resources import (
    without_cyclic_gc,  # noqa: F401 — Shared pytest fixture.
)


@pytest.mark.usefixtures('without_cyclic_gc')
def test_aggregate_scratch_type_does_not_grow_with_suite_length(monkeypatch: pytest.MonkeyPatch) -> None:
    """Different incomplete tuple lengths must not leave a distinct cyclic ctypes class per call.

    Reuse one prepared Native to isolate scratch types from DLL wrappers. The
    borrowed pointer may use a permanent scalar or fixed array type; only the
    number of distinct surviving types must stay bounded. HEAD uses no bridge.
    """
    native = _native.Native()
    calculate = native.lib.benchset_aggregate_read
    observed: List[Callable[[], Optional[Type[Any]]]] = []

    def inspect(  # noqa: PLR0913 — Mirrors the native reader entry point.
        names: Any, count: int, reader: Any, user: Any, out: Any, host_names: int,
    ) -> int:
        observed.append(weakref.ref(type(names)))
        return int(calculate(names, count, reader, user, out, host_names))

    monkeypatch.setattr(native.lib, 'benchset_aggregate_read', inspect)
    monkeypatch.setattr(_native, 'Native', lambda: native)
    first = BenchmarkResult('single_core', status='completed', pi_equivalent=1)
    second = BenchmarkResult('multi_core', status='completed', pi_equivalent=4)
    for count in range(3, 43):
        results = (first, second) + (first,) * (count - 2)
        assert SuiteResult.from_results(results).aggregated_scores.pi_equivalent is None
    retained_types = {reference() for reference in observed if reference() is not None}
    assert len(retained_types) <= 1, 'scratch types must not accumulate for each distinct input length'


@pytest.mark.parametrize('count', [0, 1, 2, 3, 8, 64])
def test_aggregate_scratch_capacity_does_not_replace_result_count(count: int) -> None:
    """A fixed native pointer must still carry the actual tuple count, including empty and complete suites."""
    first = BenchmarkResult('single_core', status='completed', pi_equivalent=1)
    second = BenchmarkResult('multi_core', status='completed', pi_equivalent=4)
    results = (first, second)[:count] if count <= 2 else (first, second) + (first,) * (count - 2)
    suite = SuiteResult.from_results(results)
    assert suite.results is results
    assert suite.aggregated_scores.pi_equivalent == (2 if count == 2 else None)
