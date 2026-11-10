"""Preserve public APIs on ctypes implementations without CPython's C API."""

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest

from benchset import _native
from tests import test_quiet_validation

quiet_environment = test_quiet_validation.quiet_environment

_PUBLIC = '''import ctypes, _ctypes, math, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
mock = sys.argv[2] == 'mock'
if mock:
    del ctypes.pythonapi
import benchset
from benchset import _native
loads = []
if mock:
    path = root / 'benchset/_native' / ('benchset.dll' if sys.platform == 'win32' else 'libbenchset.dylib' if sys.platform == 'darwin' else 'libbenchset.so')
    library = ctypes.CDLL(str(path))
    process = ctypes.CDLL(None) if sys.platform != 'win32' else None
    def factory(name, **kwargs):
        if name is None:
            assert process is not None and not kwargs
            return process
        assert str(path) == name and not kwargs
        loads.append(name)
        return library
    ctypes.CDLL = ctypes.cdll._dlltype = factory
    def forbidden(*args): raise AssertionError('manual dlopen cannot own this handle')
    ctypes._dlopen = forbidden
first, second = _native.Native(), _native.Native()
assert first.lib.benchset_abi_version is not second.lib.benchset_abi_version
ratios = tuple(benchset.BenchmarkResult(name, status='completed', pi_equivalent=ratio) for name, ratio in zip(benchset.BENCHMARKS, (1., 4.)))
assert benchset.SuiteResult.from_results(ratios).aggregated_scores.pi_equivalent == 2.
unknown = benchset.run('unknown', warmup_seconds=0, measurement_seconds=.1)
assert (unknown.status, unknown.reason) == ('failed', 'unknown benchmark: unknown')
single = benchset.run('single_core', warmup_seconds=0, measurement_seconds=.1)
assert single.success and single.pi_equivalent is not None, single
suite = benchset.run_all(warmup_seconds=0, measurement_seconds=.1)
assert [item.status for item in suite.results] == ['completed', 'completed'], suite
assert suite.aggregated_scores.pi_equivalent is not None
runner = benchset.BenchmarkRunner()
assert runner.run('single_core', warmup_seconds=0, measurement_seconds=.1, on_progress=lambda p: runner.cancel()).status == 'cancelled'
assert runner.run('single_core', warmup_seconds=0, measurement_seconds=.1).success
for parent in (float, int):
    calls = []
    class Ratio(parent):
        def __float__(self): calls.append('float'); return 4.
    values = (benchset.BenchmarkResult('single_core', status='completed', pi_equivalent=Ratio(16)), benchset.BenchmarkResult('multi_core', status='completed', pi_equivalent=1.))
    assert benchset.SuiteResult.from_results(values).aggregated_scores.pi_equivalent == 2.
    assert calls == ['float', 'float']
for stage in (0, 1):
    calls = []
    original, cause = RuntimeError('conversion failed'), LookupError('cause')
    class Ratio:
        def __float__(self):
            calls.append('float')
            if calls.count('float') - 1 == stage: raise original from cause
            return 4.
        def __ge__(self, other): return True
        def __eq__(self, other): return False
    values = (benchset.BenchmarkResult('single_core', status='completed', pi_equivalent=Ratio()), benchset.BenchmarkResult('multi_core', status='completed', pi_equivalent=1.))
    try: benchset.SuiteResult.from_results(values)
    except RuntimeError as caught:
        assert caught is original and caught.__cause__ is cause and caught.__context__ is None
    else: raise AssertionError('conversion error was lost')
    assert calls == ['float'] * (stage + 1)
values = (benchset.BenchmarkResult('single_core', status='completed', pi_equivalent='4'), benchset.BenchmarkResult('multi_core', status='completed', pi_equivalent=1.))
try: math.isfinite('4')
except TypeError as expected: message = str(expected)
try: benchset.SuiteResult.from_results(values)
except TypeError as error: assert str(error) == message
else: raise AssertionError('numeric strings were accepted')
if mock: assert len(loads) == 1
print('interpreter API compatible')
'''


def _pypy() -> str:
    executable = os.environ.get('BENCHSET_TEST_PYPY') or shutil.which('pypy3')
    if executable is None:
        pytest.skip('Set BENCHSET_TEST_PYPY or provide pypy3 for interpreter compatibility checks')
    return str(Path(executable).resolve())


@pytest.mark.parametrize('implementation', ['mock', 'pypy'])
def test_public_api_without_cpython_api(implementation: str) -> None:
    """Missing pythonapi must not break imports, runs, suites or host conversion.

    Real PyPy characterizes the supported alternative interpreter. A CPython
    capability mock additionally exercises these branches during ordinary
    coverage runs, including the standard loader for opaque handles and the
    process handle used to read native signal flags on Unix. The child
    checks subclass conversion, fresh log reads, original errors and strings.
    """
    root = Path(__file__).resolve().parents[1]
    executable = _pypy() if implementation == 'pypy' else sys.executable
    environment = dict(os.environ, LC_ALL='C', LANG='C', PYTHONPATH=str(root))
    completed = subprocess.run([executable, '-c', _PUBLIC, str(root), implementation], env=environment,
                               capture_output=True, text=True, check=False, timeout=60)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert 'interpreter API compatible' in completed.stdout


@pytest.mark.parametrize('kind', ['float', 'int', 'object'])
@pytest.mark.parametrize('write_output', [False, True])
def test_missing_c_api_numeric_copy_preserves_conversion_and_destination(
    monkeypatch: pytest.MonkeyPatch, kind: str, write_output: bool,
) -> None:
    """The fallback converts once and writes either its holder or a borrowed field.

    PyPy math converts numeric subclasses through __float__; its ctypes scalar
    constructor alone ignores that override. Normalize such subclasses before
    copying while preserving the ordinary protocol for other numeric objects.
    Canaries around the output also guard the borrowed field's exact width.
    """
    calls: List[object] = []

    class Number:
        def __float__(self) -> float:
            calls.append(self)
            return 4.0

    parent = {'float': float, 'int': int, 'object': object}[kind]
    value_type = type('Ratio', (Number, parent), {})
    value = value_type() if kind == 'object' else value_type(16)
    native = _native.Native()
    scalar_type = ctypes.c_double
    output = (scalar_type * 3)(11.0, -123.0, 13.0)

    def scalar(value: Any = 0.0) -> Any:
        # Model PyPy's scalar constructor, which reads numeric subclass storage.
        if issubclass(type(value), float):
            value = float.__float__(value)
        elif issubclass(type(value), int):
            value = int.__float__(value)
        return scalar_type(value)

    monkeypatch.setattr(_native, '_AsDouble', None)
    monkeypatch.setattr(ctypes, 'c_double', scalar)
    number = native.double(value, ctypes.byref(output, ctypes.sizeof(scalar_type)) if write_output else None)
    assert (output[1] if write_output else number.value) == 4.0
    assert output[0] == 11.0
    assert output[2] == 13.0
    if not write_output:
        assert output[1] == -123.0
    assert calls == [value]


def test_alternative_ctypes_function_owner_keeps_instances_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A function exposing its owner as dll must still receive a separate signature.

    PyPy uses dll rather than CPython's ownership-dictionary entry. Exercise
    that shape with a real address-bound ctypes function, so ordinary CPython
    runs cover the independent-binding branch without a fake function object.
    """
    original = _native.Native().lib.benchset_abi_version
    library: Any = SimpleNamespace(benchset_abi_version=original)
    monkeypatch.setattr(original, 'dll', library, raising=False)
    first = _native._Functions(library).benchset_abi_version
    second = _native._Functions(library).benchset_abi_version
    assert first is not original
    assert first is not second
    first.argtypes, first.restype = [], ctypes.c_uint32
    second.argtypes, second.restype = [ctypes.c_int], ctypes.c_uint64
    assert first() == _native.ABI_VERSION
    assert first.argtypes == []
    assert first.restype is ctypes.c_uint32


def test_alternative_interpreter_quiet_rejection_preserves_flags_and_payloads(quiet_environment: Any) -> None:
    """Real PyPy rejects NaN/inf quietly without losing payloads or existing flags.

    PyPy's interpreter can already set FE_INEXACT during ctypes dispatch, so
    compare against the immediately preceding state rather than assume zero.
    A seeded divide-by-zero flag must survive aggregate and both run modes.
    """
    executable = _pypy()
    root = Path(__file__).resolve().parents[1]
    code = '''import ctypes, struct, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from benchset import BENCHMARKS, BenchmarkResult, SuiteResult, run, run_all
from benchset._native import Native
native = Native()
SuiteResult.from_results(tuple(BenchmarkResult(name, status='completed', pi_equivalent=1.) for name in BENCHMARKS))
probe = ctypes.CDLL(sys.argv[2])
probe.begin.argtypes, probe.begin.restype = [ctypes.c_int], ctypes.c_int
for name in ('flags', 'end'):
    getattr(probe, name).argtypes = []
    getattr(probe, name).restype = ctypes.c_int
for bits in (0x7ff0000000000001, 0xfff0000000000001, 0x7ff8000000000001, 0xfff8000000000001, 0x7ff0000000000000, 0xfff0000000000000):
    value = struct.unpack('=d', struct.pack('=Q', bits))[0]
    for seed in (0, 1):
        assert probe.begin(seed) == 0
        before = probe.flags()
        try:
            wire = native.double(value)
            after = probe.flags()
        finally: assert probe.end() == 0
        assert struct.unpack('=Q', struct.pack('=d', wire.value))[0] == bits
        assert after == before, (hex(bits), seed, 'raw copy', before, after)
        for position in (0, 1):
            ratios = [1., 1.]; ratios[position] = value
            results = tuple(BenchmarkResult(name, status='completed', pi_equivalent=ratio) for name, ratio in zip(BENCHMARKS, ratios))
            assert probe.begin(seed) == 0
            before = probe.flags()
            try:
                result = SuiteResult.from_results(results)
                after = probe.flags()
            finally: assert probe.end() == 0
            assert result.aggregated_scores.pi_equivalent is None
            assert after == before, (hex(bits), seed, position, before, after)
        for window in ('warmup_seconds', 'measurement_seconds'):
            options = dict(warmup_seconds=0., measurement_seconds=1.)
            options[window] = value
            for suite in (False, True):
                assert probe.begin(seed) == 0
                before = probe.flags()
                try:
                    result = run_all(**options).results[0] if suite else run('single_core', **options)
                    after = probe.flags()
                finally: assert probe.end() == 0
                assert result.status == 'failed'
                assert result.reason == window + (' must be finite and nonnegative' if window == 'warmup_seconds' else ' must be finite and positive')
                assert struct.unpack('=Q', struct.pack('=d', getattr(result, window)))[0] == bits
                assert after == before, (hex(bits), seed, window, suite, before, after)
print('quiet interpreter validation compatible')
'''
    environment = dict(os.environ, LC_ALL='C', LANG='C', PYTHONPATH=str(root))
    completed = subprocess.run([executable, '-c', code, str(root), quiet_environment._name], env=environment,
                               capture_output=True, text=True, check=False, timeout=60)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert 'quiet interpreter validation compatible' in completed.stdout
