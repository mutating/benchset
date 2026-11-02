"""Preserve Python's exponential overflow boundary without moving arithmetic out of Rust."""

import ctypes
import gc
import math
import os
import shutil
import subprocess
import sys
import weakref
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Callable, Iterator, List, Optional, Tuple, Type, cast

import pytest

from benchset import BenchmarkResult, SuiteResult, _native
from tests.test_aggregate_getters import _ORDER, _result


@pytest.fixture(scope='module')
def floating_environment(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Expose platform fenv constants and save/restore whole environments in C."""
    if os.name == 'nt':
        pytest.skip('POSIX shared-library fenv probe; MSVC floating environment differs')
    compiler = shutil.which('cc') or shutil.which('clang') or shutil.which('gcc')
    if compiler is None:
        pytest.skip('No C compiler for the floating-environment probe')
    directory = tmp_path_factory.mktemp('aggregate-exponential')
    source = directory / 'fenv.c'
    source.write_text('''#include <fenv.h>
#include <math.h>
static fenv_t saved;
static int modes[] = {FE_TONEAREST, FE_DOWNWARD, FE_UPWARD, FE_TOWARDZERO};
int begin(unsigned index) {
    if (index >= 4 || fegetenv(&saved)) return -1;
    if (fesetround(modes[index]) || feclearexcept(FE_ALL_EXCEPT)) return -1;
    return feraiseexcept(FE_INVALID);
}
int expected_mode(unsigned index) { return index < 4 ? modes[index] : -1; }
int rounding(void) { return fegetround(); }
int flags(void) { return fetestexcept(FE_ALL_EXCEPT); }
int end(void) { return fesetenv(&saved); }
double geometric(double first, double second) {
    return exp((log(first) + log(second)) / 2.0);
}
''')
    library = directory / 'fenv.so'
    built = subprocess.run([compiler, '-shared', '-fPIC', '-fno-builtin', str(source), '-lm', '-o', str(library)],
                           capture_output=True, text=True, check=False)
    assert built.returncode == 0, built.stderr
    probe = ctypes.CDLL(str(library))
    for name in ('begin', 'expected_mode'):
        getattr(probe, name).argtypes = [ctypes.c_uint]
        getattr(probe, name).restype = ctypes.c_int
    for name in ('rounding', 'flags', 'end'):
        getattr(probe, name).argtypes = []
        getattr(probe, name).restype = ctypes.c_int
    probe.geometric.argtypes = [ctypes.c_double, ctypes.c_double]
    probe.geometric.restype = ctypes.c_double
    return probe


@contextmanager
def rounding(probe: Any, mode: int) -> Iterator[None]:
    before = probe.rounding(), probe.flags()
    assert probe.begin(mode) == 0
    try:
        yield
    finally:
        assert probe.end() == 0
        assert (probe.rounding(), probe.flags()) == before


def prepared_native(monkeypatch: pytest.MonkeyPatch) -> _native.Native:
    native = _native.Native()
    monkeypatch.setattr(_native, 'Native', lambda: native)
    return native


def mean(results: Tuple[BenchmarkResult, ...]) -> Optional[float]:
    return SuiteResult.from_results(results).aggregated_scores.pi_equivalent


def endpoint_results(number: float) -> Tuple[BenchmarkResult, ...]:
    return tuple(BenchmarkResult(name, status='completed', pi_equivalent=number)
                 for name in ('single_core', 'multi_core'))


def observe(operation: Callable[[], Optional[float]]) -> Tuple[Optional[float], Optional[str]]:
    try:
        return operation(), None
    except OverflowError as error:
        return None, str(error)


@pytest.mark.parametrize('mode', range(4), ids=['nearest', 'downward', 'upward', 'towardzero'])
@pytest.mark.parametrize('number', [sys.float_info.max, float.fromhex('0x0.0000000000001p-1022')],
                         ids=['maximum', 'minimum-subnormal'])
def test_finite_endpoints_preserve_python_exp_overflow_and_caller_rounding(
    floating_environment: Any, monkeypatch: pytest.MonkeyPatch, mode: int, number: float,
) -> None:
    """A finite exponent may overflow under upward rounding and must raise as before.

    Derive the old Python outcome and floating exception flags under the same
    mode, then invoke the real Rust calculator. Other modes protect finite
    endpoints against clamping, direct products and unconditional overflow errors.
    Minimum subnormals also preserve exp underflow as a value rather than an error.
    The context manager restores every flag and rounding mode even on failure.
    """
    prepared_native(monkeypatch)
    results = endpoint_results(number)
    with rounding(floating_environment, mode):
        expected = observe(lambda: math.exp(sum(math.log(number) for _index in range(2)) / 2))
        expected_flags = floating_environment.flags()
    with rounding(floating_environment, mode):
        actual = observe(lambda: mean(results))
        actual_flags = floating_environment.flags()
        actual_mode = floating_environment.rounding()
    assert actual == expected
    if expected[0] is not None:
        assert float.hex(cast(float, actual[0])) == float.hex(expected[0])
    assert actual_flags == expected_flags
    assert actual_mode == floating_environment.expected_mode(mode)


@pytest.mark.parametrize('mode', range(4))
@pytest.mark.parametrize('log_value', [math.inf, math.nan], ids=['infinite', 'nan'])
def test_fresh_nonfinite_log_values_are_not_mistaken_for_finite_exp_overflow(
    floating_environment: Any, monkeypatch: pytest.MonkeyPatch, mode: int, log_value: float,
) -> None:
    """A late getter may yield infinity/NaN after finite validation without an exp error.

    math.exp(+inf) returns +inf; only a finite overflowing exponent raises.
    Preserve the original six reads and skip a blanket rejection of nonfinite
    aggregates, while the shared calculator still controls the complete order.
    """
    prepared_native(monkeypatch)
    events: List[str] = []
    results = (_result('single_core', 'first', (1.0,) * 5 + (log_value,), events),
               _result('multi_core', 'second', (1.0,) * 6, events))
    with rounding(floating_environment, mode):
        value = mean(results)
        actual_mode = floating_environment.rounding()
    assert value is not None
    assert math.isnan(value) if math.isnan(log_value) else value == math.inf
    assert tuple(events) == _ORDER
    assert actual_mode == floating_environment.expected_mode(mode)


@pytest.mark.parametrize('mode', range(4))
@pytest.mark.parametrize('position', [0, 1])
def test_zero_skips_the_entire_overflowing_log_and_exponential_path(
    floating_environment: Any, monkeypatch: pytest.MonkeyPatch, mode: int, position: int,
) -> None:
    """A zero suppresses every later getter and exp operation without new fenv flags.

    The other value is the maximum finite float, so evaluating logs eagerly
    could overflow under upward rounding. Four-value getters reject unused
    fifth/sixth reads and preserve short-circuiting at either zero position.
    """
    prepared_native(monkeypatch)
    events: List[str] = []
    values = [(sys.float_info.max,) * 4, (sys.float_info.max,) * 4]
    values[position] = (*values[position][:3], -0.0)
    results = (_result('single_core', 'first', values[0], events),
               _result('multi_core', 'second', values[1], events))
    with rounding(floating_environment, mode):
        initial_flags = floating_environment.flags()
        value = mean(results)
        flags = floating_environment.flags()
        actual_mode = floating_environment.rounding()
    assert value == 0.0
    assert math.copysign(1.0, value) == 1.0
    assert tuple(events) == _ORDER[:9 + position]
    assert flags == initial_flags
    assert actual_mode == floating_environment.expected_mode(mode)


@pytest.mark.parametrize('mode', range(4))
def test_raw_native_snapshot_aggregation_retains_c_exponential_semantics(
    floating_environment: Any, monkeypatch: pytest.MonkeyPatch, mode: int,
) -> None:
    """The Python-only overflow translation must not alter raw C/Rust infinity results."""
    from benchset import _aggregate  # noqa: I001, PLC0415 — Only the new raw protocol control needs its layout.

    class Input(ctypes.Structure):
        _fields_ = [('benchmark', ctypes.c_uint32), ('status', ctypes.c_uint32),  # noqa: RUF012
                    ('pi_equivalent', ctypes.c_double), ('has_pi_equivalent', ctypes.c_uint32)]

    native = prepared_native(monkeypatch)
    inputs = (Input * 2)(Input(1, 0, sys.float_info.max, 1), Input(2, 0, sys.float_info.max, 1))
    scores = _aggregate._Scores()
    with rounding(floating_environment, mode):
        expected = floating_environment.geometric(sys.float_info.max, sys.float_info.max)
        assert native.lib.benchset_aggregate(inputs, 2, ctypes.byref(scores)) == 1
        actual_mode = floating_environment.rounding()
    assert scores.has_pi_equivalent == 1
    assert scores.pi_equivalent == expected
    assert actual_mode == floating_environment.expected_mode(mode)


def request_exponential(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the real Python reader directly, isolating its new exp transport field."""
    from benchset import _aggregate  # noqa: PLC0415

    def calculate(_names: object, _count: int, reader: Any, user: object, _out: object, _host: int) -> int:
        request = _aggregate._Input()
        request.pi_equivalent = 1500.0
        assert reader(0, 7, ctypes.byref(request), user) == 0
        return 1

    native = _native.Native.__new__(_native.Native)
    cast(Any, native).lib = SimpleNamespace(benchset_aggregate_read=calculate)
    monkeypatch.setattr(_native, 'Native', lambda: native)
    mean((BenchmarkResult('single_core'),))


@pytest.mark.parametrize('error_type', [OverflowError, RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_exponential_host_errors_preserve_exact_identity_and_original_context(
    monkeypatch: pytest.MonkeyPatch, error_type: Type[BaseException],
) -> None:
    """The exp primitive transports every BaseException unchanged across the C callback."""
    original = error_type('host exponential failed')

    def exponentiate(value: float) -> float:
        assert value == 1500.0
        raise original

    monkeypatch.setattr(math, 'exp', exponentiate)
    try:
        raise LookupError('caller context')
    except LookupError as context:
        with pytest.raises(error_type, match='host exponential failed') as caught:
            request_exponential(monkeypatch)
        assert caught.value is original
        assert caught.value.__context__ is context  # noqa: PT017 — Preserve the active caller exception state.
        assert caught.value.__cause__ is None


def test_exponential_error_does_not_retain_its_own_callback_traceback_without_gc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping an exp failure must release its callback frames without cyclic collection."""
    class Error(RuntimeError):
        pass

    errors: List[weakref.ReferenceType[Error]] = []

    def make_error() -> Error:
        error = Error('unretained exp failure')
        errors.append(weakref.ref(error))
        return error

    def exponentiate(_value: float) -> float:
        raise make_error()

    monkeypatch.setattr(math, 'exp', exponentiate)
    enabled = gc.isenabled()
    gc.disable()
    try:
        try:
            request_exponential(monkeypatch)
        except Error:
            pass
        assert len(errors) == 1
        assert errors[0]() is None
    finally:
        if enabled:
            gc.enable()
