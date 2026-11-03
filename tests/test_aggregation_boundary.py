"""Field-preserving transport for caller-built full-suite snapshots."""

import ctypes
import gc
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from benchset import AggregatedScores, BenchmarkResult, SuiteResult, _aggregate, _native


@pytest.mark.parametrize('position', [0, 1])
@pytest.mark.parametrize('name', ['', 'single_core\0', 'single_core\0ignored', 'много_ядер', '\ud800', 'single_core\udfff'])
def test_aggregate_does_not_alias_unknown_benchmark_names(position: int, name: str) -> None:
    """Unknown names remain invalid, including UTF-8, NUL and lone surrogates.

    Length-delimited transport must never truncate an unknown string into a
    valid benchmark name or silently substitute an expected suite member.
    """
    results = [BenchmarkResult('single_core', status='completed', pi_equivalent=0.5),
               BenchmarkResult('multi_core', status='completed', pi_equivalent=2)]
    results[position] = replace(results[position], benchmark=name)
    assert SuiteResult.from_results(tuple(results)).aggregated_scores.pi_equivalent is None


@pytest.mark.parametrize('presence', [0, 1, 7, 2**32 - 1])
@pytest.mark.parametrize('ratio', [0.0, 8.0])
def test_aggregate_boundary_preserves_inputs_and_native_output(
    monkeypatch: pytest.MonkeyPatch, presence: int, ratio: float,
) -> None:
    """Read requested public success/ratio fields and preserve the native result without recalculation.

    Presence and numeric reads stay separate, so an absent value needs no
    conversion. A deliberately unrelated output and a nonstandard presence flag
    expose Python-side aggregation, truthiness mistakes for zero and lossy coefficients.
    """
    native = _native.Native()
    received: List[Tuple[int, int, float, int]] = []

    def calculate(  # noqa: PLR0913 — Mirrors the native reader entry point.
        names: Any, count: int, reader: Any, user: Any, out: Any, host_names: int,
    ) -> int:
        assert count == 3
        assert host_names == 1
        for index in range(count):
            value = _aggregate._Input()
            assert reader(index, 0, ctypes.byref(value), user) == 1
            assert reader(index, 1, ctypes.byref(value), user) == 1
            assert value.pi_equivalent == 0.0
            if value.has_pi_equivalent:
                assert reader(index, 6, ctypes.byref(value), user) == 1
            received.append((names[0], value.status, value.pi_equivalent, value.has_pi_equivalent))
        scores = ctypes.cast(out, ctypes.POINTER(_aggregate._Scores)).contents
        scores.pi_equivalent = 0.0
        scores.has_pi_equivalent = presence
        return 1

    monkeypatch.setattr(native.lib, 'benchset_aggregate_read', calculate)
    monkeypatch.setattr(_native, 'Native', lambda: native)
    values = (BenchmarkResult('single_core', status='failed', pi_equivalent=0.12345678901234567),
              BenchmarkResult('multi_core', status='cancelled', pi_equivalent=None),
              BenchmarkResult('unknown', status='unknown', pi_equivalent=ratio))
    suite = SuiteResult.from_results(values)
    assert suite.results is values
    assert received == [(0, 2, 0.12345678901234567, 1), (0, 2, 0.0, 0), (0, 2, ratio, 1)]
    assert suite.aggregated_scores == AggregatedScores(0.0 if presence else None)


def test_aggregate_boundary_reports_unavailable_native_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejected native getter cannot silently manufacture an absent aggregate."""
    native = _native.Native()
    monkeypatch.setattr(native.lib, 'benchset_aggregate_read', lambda *_args: 0)
    monkeypatch.setattr(_native, 'Native', lambda: native)
    with pytest.raises(RuntimeError, match='native aggregate is unavailable'):
        SuiteResult.from_results(())


@pytest.mark.parametrize('symbol', ['benchset_aggregate', 'benchset_aggregate_read', 'benchset_copy_double'])
def test_aggregate_is_a_required_native_symbol_before_allocation(monkeypatch: pytest.MonkeyPatch, symbol: str) -> None:
    """A same-version older library must report its missing aggregate symbol before allocating handles."""
    library = _native.Native().lib

    class OlderLibrary:
        def __getattr__(self, name: str) -> Any:
            if name == symbol:
                raise AttributeError(name)
            if name == 'benchset_create':
                return forbidden
            return getattr(library, name)

    def forbidden() -> None:
        pytest.fail('a missing symbol must be detected before allocation')

    monkeypatch.setattr(ctypes, 'CDLL', lambda _path: OlderLibrary())
    with pytest.raises(RuntimeError, match='missing required symbol: ' + symbol):
        _native.Native()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX shared-library fenv probe; MSVC floating environment differs')
@pytest.mark.parametrize('position', [0, 1])
@pytest.mark.parametrize('other', [0.0, 2.0])
def test_zero_aggregate_preserves_floating_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, position: int, other: float,
) -> None:
    """Any zero skips every logarithm, preserving caller floating-point exception flags.

    A tiny C probe supplies platform fenv macros and saves/restores the complete
    environment in a finally block. An existing FE_INVALID flag must survive,
    while neither log(0)'s divide-by-zero nor log(other)'s inexact flag may appear.
    Native bindings are prepared first to keep loader activity outside the probe.
    Disable automatic GC during measurement: CPython 3.15 collection statistics
    independently set FE_INEXACT and must not be attributed to the calculator.
    """
    compiler = shutil.which('cc') or shutil.which('clang') or shutil.which('gcc')
    if compiler is None:
        pytest.skip('No C compiler for the floating-environment probe')
    source = tmp_path / 'fenv.c'
    source.write_text('''#include <fenv.h>
static fenv_t saved;
int begin(void) {
    if (fegetenv(&saved) || feclearexcept(FE_ALL_EXCEPT)) return -1;
    if (feraiseexcept(FE_INVALID)) return -1;
    return fetestexcept(FE_ALL_EXCEPT);
}
int flags(void) { return fetestexcept(FE_ALL_EXCEPT); }
int end(void) { return fesetenv(&saved); }
''')
    library = tmp_path / 'fenv.so'
    built = subprocess.run([compiler, '-shared', '-fPIC', str(source), '-lm', '-o', str(library)],
                           capture_output=True, text=True, check=False)
    assert built.returncode == 0, built.stderr
    probe = ctypes.CDLL(str(library))
    for name in ('begin', 'flags', 'end'):
        getattr(probe, name).argtypes = []
        getattr(probe, name).restype = ctypes.c_int
    native = _native.Native()
    monkeypatch.setattr(_native, 'Native', lambda: native)
    results = [BenchmarkResult('single_core', status='completed', pi_equivalent=other),
               BenchmarkResult('multi_core', status='completed', pi_equivalent=other)]
    results[position] = replace(results[position], pi_equivalent=-0.0)
    collecting = gc.isenabled()
    gc.disable()
    try:
        initial = probe.begin()
        try:
            value = SuiteResult.from_results(tuple(results)).aggregated_scores.pi_equivalent
            observed = probe.flags()
        finally:
            restored = probe.end()
    finally:
        if collecting:
            gc.enable()
    assert initial >= 0
    assert restored == 0
    assert value == 0.0
    assert observed == initial
