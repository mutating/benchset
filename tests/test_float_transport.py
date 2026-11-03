"""Keep Python's existing numeric-conversion protocol across quiet FFI copies."""

import ctypes
import subprocess
import sys
from typing import Any, List, Type, cast

import pytest

from benchset import BENCHMARKS, BenchmarkResult, SuiteResult, _native


@pytest.mark.parametrize('position', [0, 1])
@pytest.mark.parametrize('override', ['different', 'error'])
def test_float_subclass_payload_does_not_invoke_its_float_override(position: int, override: str) -> None:
    """PyFloat_AsDouble reads float-subclass storage without calling __float__.

    Quiet copying must retain this protocol rather than normalize every value
    with Python float(), which would change either the score or its exception.
    Integer subclasses have a separately approved conversion-based contract.
    """
    calls: List[str] = []

    class Ratio(float):
        __slots__ = ()

        def __float__(self) -> float:
            calls.append('float')
            if override == 'error':
                raise RuntimeError('must not be called')
            return 4.0

    ratios = [1.0, 1.0]
    ratios[position] = Ratio(16.0)
    results = tuple(BenchmarkResult(name, status='completed', pi_equivalent=value)
                    for name, value in zip(BENCHMARKS, ratios))
    assert SuiteResult.from_results(results).aggregated_scores.pi_equivalent == pytest.approx(4.0)
    assert not calls


@pytest.mark.parametrize('position', [0, 1])
@pytest.mark.parametrize('stage', [0, 1], ids=['validation', 'logarithm'])
@pytest.mark.parametrize('error_type', [TypeError, RuntimeError, KeyboardInterrupt])
def test_numeric_conversion_errors_preserve_identity_and_context(
    position: int, stage: int, error_type: Type[BaseException],
) -> None:
    """A C-API numeric conversion error must survive the quiet Rust copy unchanged.

    The getter may fail at validation or at the fresh log read. Python's error
    indicator must cross the primitive without executing any further user
    operations, replacing the exception, or changing its explicit cause.
    """
    original, cause = error_type('conversion failed'), LookupError('cause')
    events: List[str] = []

    class Ratio:
        __hash__ = object.__hash__

        def __float__(self) -> float:
            events.append('float')
            if events.count('float') - 1 == stage:
                raise original from cause
            return 4.0

        def __ge__(self, other: Any) -> bool:
            assert type(other) is int
            assert other == 0
            events.append('ge')
            return True

        def __eq__(self, other: object) -> bool:
            assert type(other) is int
            assert other == 0
            events.append('eq')
            return False

    ratios: List[Any] = [1.0, 1.0]
    ratios[position] = Ratio()
    results = tuple(BenchmarkResult(name, status='completed', pi_equivalent=value)
                    for name, value in zip(BENCHMARKS, ratios))
    with pytest.raises(error_type) as caught:
        SuiteResult.from_results(results)
    assert caught.value is original
    assert original.__cause__ is cause
    assert original.__context__ is None
    assert original.__suppress_context__
    assert events == (['float'] if stage == 0 else ['float', 'ge', 'eq', 'float'])


@pytest.mark.parametrize('position', [0, 1])
def test_non_numeric_strings_are_not_accepted_by_the_copy_primitive(position: int) -> None:
    """Numeric-looking strings keep PyFloat_AsDouble's TypeError, not float(str)."""
    value = '4'
    with pytest.raises(TypeError) as expected:
        ctypes.c_double(cast(Any, value))
    ratios: List[Any] = [1.0, 1.0]
    ratios[position] = value
    results = tuple(BenchmarkResult(name, status='completed', pi_equivalent=ratio)
                    for name, ratio in zip(BENCHMARKS, ratios))
    with pytest.raises(TypeError) as actual:
        SuiteResult.from_results(results)
    assert str(actual.value) == str(expected.value)


def test_rejected_copy_cannot_publish_an_uninitialized_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed native copy is an error, never a fabricated zero coefficient."""
    monkeypatch.setattr(_native, '_DoubleCopy', lambda _address: lambda *_arguments: 0)
    results = tuple(BenchmarkResult(name, status='completed', pi_equivalent=4.0) for name in BENCHMARKS)
    with pytest.raises(RuntimeError, match='native numeric conversion is unavailable'):
        SuiteResult.from_results(results)


def test_numeric_reader_keeps_python_thread_state_in_a_subprocess() -> None:
    """The C-API reader must retain the GIL/thread state during user conversion.

    A regular ctypes call detaches that state and can crash while the C API
    invokes __float__. Isolate this regression so a bad bridge cannot terminate
    the test runner; the child verifies both conversions and the final score.
    """
    code = '''import ctypes
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from benchset import BENCHMARKS, BenchmarkResult, SuiteResult
check = ctypes.pythonapi.PyGILState_Check
check.restype = ctypes.c_int
check.argtypes = []
calls = []
class Ratio:
    def __float__(self):
        assert check() == 1
        calls.append('float')
        return 4.0
    def __ge__(self, other): return True
    def __eq__(self, other): return False
results = tuple(BenchmarkResult(name, status='completed', pi_equivalent=Ratio()) for name in BENCHMARKS)
assert SuiteResult.from_results(results).aggregated_scores.pi_equivalent == 4.0
assert calls == ['float'] * 4
'''
    completed = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=False, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
