"""Reject invalid numeric snapshots without disturbing the caller's FPU state."""

import ctypes
import gc
import math
import os
import shutil
import struct
import subprocess
import sys
from contextlib import contextmanager
from typing import Any, Iterator, List, Tuple

import pytest

from benchset import (
    BenchmarkResult,
    BenchmarkRunner,
    SuiteResult,
    _native,
    api,
    run_all,
)

_BITS = (
    0x7ff0000000000001, 0xfff0000000000001,
    0x7ff8000000000001, 0xfff8000000000001,
    0x7ff0000000000000, 0xfff0000000000000,
)

_ENVIRONMENT = '''#include <fenv.h>
static fenv_t saved;
int begin(int seed) {
    if (fegetenv(&saved) || feclearexcept(FE_ALL_EXCEPT)) return -1;
    return seed ? feraiseexcept(FE_DIVBYZERO) : 0;
}
int flags(void) { return fetestexcept(FE_ALL_EXCEPT); }
int rounding(void) { return fegetround(); }
int end(void) { return fesetenv(&saved); }
'''


@pytest.fixture(scope='module')
def quiet_environment(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Read real platform exception flags and restore the complete FPU environment."""
    if os.name == 'nt':
        pytest.skip('POSIX fenv probe; MSVC floating environment differs')
    compiler = shutil.which('cc') or shutil.which('clang') or shutil.which('gcc')
    if compiler is None:
        pytest.skip('No C compiler for floating-environment validation')
    directory = tmp_path_factory.mktemp('quiet-validation')
    source = directory / 'environment.c'
    source.write_text(_ENVIRONMENT)
    library = directory / 'environment.so'
    built = subprocess.run([compiler, '-shared', '-fPIC', str(source), '-lm', '-o', str(library)],
                           capture_output=True, text=True, check=False)
    assert built.returncode == 0, built.stderr
    probe = ctypes.CDLL(str(library))
    probe.begin.argtypes = [ctypes.c_int]
    probe.begin.restype = ctypes.c_int
    for name in ('flags', 'rounding', 'end'):
        getattr(probe, name).argtypes = []
        getattr(probe, name).restype = ctypes.c_int
    # Import/ctypes initialization is outside every measured validation window.
    _native.Native()
    SuiteResult.from_results(tuple(BenchmarkResult(name, status='completed', pi_equivalent=1.0)
                                   for name in ('single_core', 'multi_core')))
    return probe


@contextmanager
def quiet_flags(probe: Any, seed: bool) -> Iterator[int]:
    # CPython 3.15 GC statistics set FE_INEXACT independently of validation.
    collecting = gc.isenabled()
    gc.disable()
    before = probe.rounding(), probe.flags()
    try:
        assert probe.begin(seed) == 0
        yield probe.flags()
    finally:
        try:
            assert probe.end() == 0
            assert (probe.rounding(), probe.flags()) == before
        finally:
            if collecting:
                gc.enable()


def number(bits: int) -> float:
    return struct.unpack('=d', struct.pack('=Q', bits))[0]


@pytest.mark.parametrize('bits', _BITS, ids=['snan', 'negative-snan', 'qnan', 'negative-qnan', 'inf', 'negative-inf'])
@pytest.mark.parametrize('position', [0, 1])
@pytest.mark.parametrize('seed', [False, True])
def test_aggregate_rejects_nonfinite_values_without_new_fpu_flags(
    quiet_environment: Any, bits: int, position: int, seed: bool,
) -> None:
    """Both coefficient positions quietly reject every NaN/infinity encoding.

    Signaling NaNs must not reach speculative floating comparisons. Retaining
    a preexisting divide-by-zero flag also rejects implementations that clear
    the caller's flags instead of avoiding the invalid operation.
    """
    values = [1.0, 1.0]
    values[position] = number(bits)
    results = tuple(BenchmarkResult(name, status='completed', pi_equivalent=value)
                    for name, value in zip(('single_core', 'multi_core'), values))
    with quiet_flags(quiet_environment, seed) as initial:
        result = SuiteResult.from_results(results)
        flags = quiet_environment.flags()
    assert result.aggregated_scores.pi_equivalent is None
    assert flags == initial


@pytest.mark.parametrize('bits', [*_BITS, 0xbff0000000000000])
@pytest.mark.parametrize('window', ['warmup', 'measurement'])
@pytest.mark.parametrize('seed', [False, True])
def test_duration_validation_rejects_invalid_values_without_new_fpu_flags(
    quiet_environment: Any, bits: int, window: str, seed: bool,
) -> None:
    """Rust validation classifies invalid durations before any floating comparison.

    Warmup keeps priority over an independently invalid measurement; rejected
    names likewise precede duration inspection. No new flag may replace an
    existing caller flag, including when the input is a signaling NaN.
    """
    native = _native.Native()
    warmup, measurement = (number(bits), 0.0) if window == 'warmup' else (0.0, number(bits))
    expected = 2 if window == 'warmup' else 3
    with quiet_flags(quiet_environment, seed) as initial:
        code = native.lib.benchset_duration_error(native.double(warmup), native.double(measurement))
        flags = quiet_environment.flags()
        unknown = native.lib.benchset_validation_error(0, native.double(warmup), native.double(measurement))
        unknown_flags = quiet_environment.flags()
    assert code == expected
    assert unknown == 1
    assert flags == unknown_flags == initial


@pytest.mark.parametrize('bits', _BITS)
@pytest.mark.parametrize('window', ['warmup', 'measurement'])
@pytest.mark.parametrize('suite', [False, True])
def test_public_run_rejects_nonfinite_windows_quietly_and_preserves_requested_bits(
    quiet_environment: Any, monkeypatch: pytest.MonkeyPatch, bits: int, window: str, suite: bool,
) -> None:
    """Public Python entry points return the old failure and original duration bits.

    No worker/progress may start for a rejected window. The adapter must not
    quiet or replace the caller's NaN just to avoid a hardware exception.
    """
    progress: List[Any] = []
    options = {'warmup_seconds': number(bits) if window == 'warmup' else 0.0,
               'measurement_seconds': number(bits) if window == 'measurement' else 1.0}
    runner = BenchmarkRunner()
    monkeypatch.setattr(api, 'BenchmarkRunner', lambda: runner)
    with quiet_flags(quiet_environment, True) as initial:
        outcome = (run_all(on_progress=progress.append, **options) if suite else
                   runner.run('single_core', on_progress=progress.append, **options))
        flags = quiet_environment.flags()
    result = outcome.results[0] if isinstance(outcome, SuiteResult) else outcome
    assert result.status == 'failed'
    expected = 'warmup_seconds must be finite and nonnegative' if window == 'warmup' else (
        'measurement_seconds must be finite and positive'
    )
    assert result.reason == expected
    actual = result.warmup_seconds if window == 'warmup' else result.measurement_seconds
    assert struct.unpack('=Q', struct.pack('=d', actual))[0] == bits
    assert flags == initial
    assert not progress
    assert runner._active is None
    assert runner._native is None
    assert runner._handle is None


@pytest.mark.parametrize('value', [-1.0, -.25, -sys.float_info.max])
@pytest.mark.parametrize('position', [0, 1])
@pytest.mark.parametrize('seed', [False, True])
def test_negative_host_log_domain_preserves_python_flags_and_zero_comparison_operand(
    quiet_environment: Any, value: float, position: int, seed: bool,
) -> None:
    """A custom accepted negative ratio raises Python's domain error without a cast.

    The log primitive's floating operand must never be converted to an unsigned
    status field. Only ge/eq need that field, and their operand is integer zero.
    """
    calls: List[Tuple[str, object]] = []

    class Ratio(float):
        __slots__ = ()
        __hash__ = float.__hash__

        def __ge__(self, other: object) -> bool:
            calls.append(('ge', other))
            return True

        def __eq__(self, other: object) -> bool:
            calls.append(('eq', other))
            return False

    values = [1.0, 1.0]
    values[position] = Ratio(value)
    results = tuple(BenchmarkResult(name, status='completed', pi_equivalent=ratio)
                    for name, ratio in zip(('single_core', 'multi_core'), values))
    with quiet_flags(quiet_environment, seed):
        with pytest.raises(ValueError, match=r'.') as expected:
            math.log(value)
        expected_flags = quiet_environment.flags()
    with quiet_flags(quiet_environment, seed):
        with pytest.raises(ValueError, match=r'.') as actual:
            SuiteResult.from_results(results)
        actual_flags = quiet_environment.flags()
    assert str(actual.value) == str(expected.value)
    assert actual_flags == expected_flags
    assert calls == [('ge', 0), ('eq', 0)]
    assert all(type(operand) is int for _name, operand in calls)


@pytest.mark.parametrize('bits', [0, 0x8000000000000000, 0x7fefffffffffffff, 1])
def test_finite_controls_and_negative_zero_remain_accepted_by_duration_validation(
    quiet_environment: Any, bits: int,
) -> None:
    """Bit classification must keep finite endpoints and warmup negative zero valid.

    These values need not be representable as Rust Duration: the validation API
    deliberately checks the public numeric range independently of conversion.
    """
    native = _native.Native()
    warmup, measurement = native.double(number(bits)), native.double(1.0)
    with quiet_flags(quiet_environment, True) as initial:
        code = native.lib.benchset_duration_error(warmup, measurement)
        flags = quiet_environment.flags()
    assert code == 0
    assert flags == initial


@pytest.mark.parametrize('bits', [0, 0x8000000000000000])
@pytest.mark.parametrize('position', [0, 1])
def test_aggregate_zero_remains_present_positive_zero_without_new_flags(
    quiet_environment: Any, bits: int, position: int,
) -> None:
    """Quiet sign checks must accept either zero sign and suppress all logarithms."""
    values = [1.0, 1.0]
    values[position] = number(bits)
    results = tuple(BenchmarkResult(name, status='completed', pi_equivalent=value)
                    for name, value in zip(('single_core', 'multi_core'), values))
    with quiet_flags(quiet_environment, True) as initial:
        result = SuiteResult.from_results(results).aggregated_scores.pi_equivalent
        flags = quiet_environment.flags()
    assert result is not None
    assert struct.unpack('=Q', struct.pack('=d', result))[0] == 0
    assert flags == initial


_C_CONSUMER = r'''
#include "benchset.h"
#include <assert.h>
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static uint64_t bits(double value) { uint64_t result; memcpy(&result, &value, 8); return result; }
static double value(uint64_t raw) { double result; memcpy(&result, &raw, 8); return result; }
static void single(BenchsetConfig *config, int seed, const char *reason) {
    BenchsetHandle *handle = benchset_create(); assert(handle);
    assert(begin(seed) == 0); int previous = flags();
    assert(benchset_run(handle, config, NULL, NULL) == BENCHSET_FAILED);
    BenchsetResult result; assert(benchset_result(handle, &result) == 1);
    assert(strcmp(benchset_reason(handle), reason) == 0);
    assert(bits(result.warmup_seconds) == bits(config->warmup_seconds));
    assert(bits(result.measurement_seconds) == bits(config->measurement_seconds));
    int raised = flags(); assert(end() == 0); benchset_free(handle);
    assert(raised == previous);
}
static void suite(BenchsetConfig *config, int seed) {
    BenchsetSuiteHandle *handle = benchset_suite_create(); assert(handle);
    assert(begin(seed) == 0); int previous = flags();
    assert(benchset_run_all(handle, config, NULL, NULL) == BENCHSET_FAILED);
    BenchsetSuiteResult result; assert(benchset_suite_result(handle, &result, sizeof result) == 1);
    assert(result.result_count == 1 && result.results[0].base.base.status == BENCHSET_FAILED);
    assert(bits(result.results[0].base.base.warmup_seconds) == bits(config->warmup_seconds));
    assert(bits(result.results[0].base.base.measurement_seconds) == bits(config->measurement_seconds));
    assert(strcmp(benchset_suite_reason(handle, 0), "durations must be finite: warmup >= 0, measurement > 0") == 0);
    int raised = flags(); assert(end() == 0); benchset_suite_free(handle);
    assert(raised == previous);
}
typedef double (*NumberReader)(void *);
typedef int32_t (*CopyNumber)(NumberReader, void *, double *);
static unsigned reads;
static double read_number(void *user) {
    ++reads;
    uint64_t raw = UINT64_C(0x8000000000000000);
    if (user) memcpy(&raw, user, sizeof raw);
    return value(raw);
}
static void copy_number(const char *input, int seed) {
    CopyNumber copy = (CopyNumber)dlsym(RTLD_DEFAULT, "benchset_copy_double");
    assert(copy != NULL);
    uint64_t raw = strtoull(input, NULL, 16);
    struct { uint64_t before; double out; uint64_t after; } guarded = {
        UINT64_C(0x1122334455667788), 17, UINT64_C(0x8877665544332211)};
    reads = 0;
    assert(begin(seed) == 0); int previous = flags();
    assert(copy(NULL, &raw, &guarded.out) == 0 && reads == 0 && guarded.out == 17);
    assert(copy(read_number, &raw, NULL) == 0 && reads == 0);
    assert(copy(read_number, &raw, &guarded.out) == 1 && reads == 1);
    assert(bits(guarded.out) == raw);
    assert(copy(read_number, NULL, &guarded.out) == 1 && reads == 2);
    assert(bits(guarded.out) == UINT64_C(0x8000000000000000));
    double aliased = value(raw);
    assert(copy(read_number, &aliased, &aliased) == 1 && reads == 3 && bits(aliased) == raw);
    int raised = flags(); assert(end() == 0); assert(raised == previous);
    assert(guarded.before == UINT64_C(0x1122334455667788));
    assert(guarded.after == UINT64_C(0x8877665544332211));
}
int main(int argc, char **argv) {
    unsigned cases = 0;
    for (int arg = 1; arg < argc; ++arg) {
        uint64_t raw = strtoull(argv[arg], NULL, 16);
        copy_number(argv[arg], 0); copy_number(argv[arg], 1);
        for (int seed = 0; seed != 2; ++seed) for (int window = 0; window != 2; ++window) {
            BenchsetConfig config; assert(benchset_default_config(&config) == 1);
            config.warmup_seconds = window ? 0 : value(raw);
            config.measurement_seconds = window ? value(raw) : 1;
            assert(begin(seed) == 0); int previous = flags();
            assert(benchset_duration_error(config.warmup_seconds, config.measurement_seconds) == (window ? 3 : 2));
            assert(benchset_validation_error(0, config.warmup_seconds, config.measurement_seconds) == 1);
            int raised = flags(); assert(end() == 0); assert(raised == previous);
            single(&config, seed, "durations must be finite: warmup >= 0, measurement > 0");
            suite(&config, seed);
            config.benchmark = UINT32_MAX; config.reserved = 7;
            single(&config, seed, "unknown benchmark");
            config.benchmark = 1;
            single(&config, seed, "reserved config field must be zero");
            BenchsetAggregateInput inputs[] = {{1,0,1,1},{2,0,1,1}};
            for (unsigned position = 0; position != 2; ++position) {
                inputs[position].pi_equivalent = value(raw);
                assert(begin(seed) == 0); previous = flags();
                BenchsetAggregatedScores out = {7,7};
                assert(benchset_aggregate(inputs, 2, &out) == 1 && !out.has_pi_equivalent);
                raised = flags(); assert(end() == 0); assert(raised == previous);
                inputs[position].pi_equivalent = 1;
            }
            ++cases;
        }
    }
    printf("%u C validation/aggregate matrices passed\n", cases);
}
'''

_RUST_CONSUMER = r'''
use benchset::{Benchmark, BenchmarkResult, Status, SuiteResult, PI_REFERENCE_MACHINE, PI_REFERENCE_RUN};
extern "C" { fn begin(seed: i32) -> i32; fn flags() -> i32; fn end() -> i32; }
fn result(benchmark: Benchmark, value: f64) -> BenchmarkResult {
    BenchmarkResult { benchmark, version:"test", status:Status::Completed, reason:String::new(),
        score:None,workers:1,warmup_seconds:0.0,measurement_seconds:1.0,elapsed_seconds:1.0,
        iterations:1,checksum:0,samples:vec![],variation_percent:None,unstable:false,
        pi_equivalent:Some(value),reference_machine:&PI_REFERENCE_MACHINE,reference_run:&PI_REFERENCE_RUN }
}
fn main() {
    for raw in std::env::args().skip(1) {
        let bits=u64::from_str_radix(&raw,16).unwrap();
        for seed in [0,1] { for position in 0..2 {
            let mut values=vec![result(Benchmark::SingleCore,1.0),result(Benchmark::MultiCore,1.0)];
            values[position].pi_equivalent=Some(f64::from_bits(bits));
            assert_eq!(unsafe {begin(seed)},0); let previous=unsafe {flags()};
            let output=SuiteResult::new(values).aggregated_scores.pi_equivalent;
            let raised=unsafe {flags()}; assert_eq!(unsafe {end()},0);
            assert!(output.is_none()); assert_eq!(raised,previous,"input {bits:x}");
        }}
    }
}
'''


@pytest.fixture(scope='module')
def quiet_consumers(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Compile C/Rust consumers without executing the potentially broken adapter."""
    from pathlib import Path  # noqa: PLC0415 — Consumer-only filesystem context.

    if os.name == 'nt':
        pytest.skip('POSIX fenv probe; MSVC floating environment differs')
    root = Path(__file__).resolve().parents[1]
    directory = tmp_path_factory.mktemp('quiet-consumers')
    compiler = shutil.which('cc') or shutil.which('clang') or shutil.which('gcc')
    if compiler is None:
        pytest.skip('No C compiler for floating-environment validation')
    helper = directory / 'environment.c'
    helper.write_text(_ENVIRONMENT)
    source = directory / 'consumer.c'
    source.write_text(_ENVIRONMENT + _C_CONSUMER)
    executable = directory / 'consumer'
    library = root / 'benchset/_native'
    built = subprocess.run([compiler, '-std=c11', '-fno-builtin', str(source), '-I', str(root / 'include'),
                            '-L', str(library), '-lbenchset', '-lm', '-Wl,-rpath,' + str(library),
                            *(['-ldl'] if sys.platform != 'darwin' else []), '-o', str(executable)], capture_output=True, text=True, check=False)
    assert built.returncode == 0, built.stdout + built.stderr
    helper_object = directory / 'environment.o'
    built = subprocess.run([compiler, '-c', str(helper), '-o', str(helper_object)],
                           capture_output=True, text=True, check=False)
    assert built.returncode == 0, built.stdout + built.stderr
    return root, directory, executable, helper_object


def test_public_c_run_validation_and_aggregate_reject_invalid_values_quietly(quiet_consumers: Any) -> None:
    """Real C entry points reject runtime NaN/inf/negative payloads before conversion.

    The consumer checks generic one-read double copying (including NULLs and
    aliasing), both run modes, both windows, requested-bit preservation,
    unknown/reserved precedence, raw snapshot aggregation and preexisting flags.
    Runtime argv payloads prevent compile-time constant folding from masking a
    speculative comparison in an optimized Rust library.
    """
    _root, _directory, executable, _helper = quiet_consumers
    completed = subprocess.run([str(executable), *(format(bits, 'x') for bits in (*_BITS, 0xbfd0000000000000))],
                               capture_output=True, text=True, check=False, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert '28 C validation/aggregate matrices passed' in completed.stdout


@pytest.mark.parametrize('optimized', [False, True], ids=['debug', 'optimized'])
def test_public_rust_runtime_aggregate_rejects_nonfinite_values_without_flags(
    quiet_consumers: Any, tmp_path: Any, optimized: bool,
) -> None:
    """Downstream Rust keeps the new quiet contract with debug and optimized rlibs.

    Values arrive through argv rather than constants, and a C helper observes
    hardware flags around only aggregation. This catches LLVM speculation that
    can differ between generic scalar and callback-backed implementations.
    """
    from scripts.build_backend import rust_environment  # noqa: PLC0415
    from scripts.version import read_version  # noqa: PLC0415

    root, _directory, _executable, helper = quiet_consumers
    architecture = 'aarch64' if os.uname().machine in ('arm64', 'aarch64') else 'x86_64'
    target = architecture + ('-apple-darwin' if sys.platform == 'darwin' else '-unknown-linux-gnu')
    environment = rust_environment(root, target)
    environment['CARGO_PKG_VERSION'] = read_version(root)
    compiler = shutil.which('rustc', path=environment.get('PATH'))
    assert compiler is not None
    library = tmp_path / 'libbenchset.rlib'
    profile = ['-C', 'opt-level=3' if optimized else 'opt-level=0']
    built = subprocess.run([compiler, '--edition=2021', '--crate-name=benchset', '--crate-type=rlib',
                            str(root / 'native/src/lib.rs'), *profile, '-o', str(library)], env=environment,
                           capture_output=True, text=True, check=False, timeout=120)
    assert built.returncode == 0, built.stdout + built.stderr
    source = tmp_path / 'consumer.rs'
    source.write_text(_RUST_CONSUMER)
    executable = tmp_path / 'consumer'
    built = subprocess.run([compiler, '--edition=2021', str(source), '--extern', 'benchset=' + str(library),
                            '-C', 'link-arg=' + str(helper), *profile, '-o', str(executable)], env=environment,
                           capture_output=True, text=True, check=False, timeout=120)
    assert built.returncode == 0, built.stdout + built.stderr
    completed = subprocess.run([str(executable), *(format(bits, 'x') for bits in (*_BITS, 0xbfd0000000000000))],
                               capture_output=True, text=True, check=False, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
