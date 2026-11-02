"""Full-suite result shape, aggregation, early stopping and console summary."""

import io
import json
import math
import signal
import subprocess
import sys
from dataclasses import FrozenInstanceError, asdict, replace
from typing import Callable, Dict, List, Optional, Tuple, Type

import pytest

from benchset import (
    BENCHMARKS,
    AggregatedScores,
    BenchmarkResult,
    BenchmarkRunner,
    Progress,
    Sample,
    SuiteResult,
    cli,
    run_all,
)
from tests.helpers import mock_suite


def completed(name: str, equivalent: float) -> BenchmarkResult:
    return BenchmarkResult(name, status='completed', score=999, pi_equivalent=equivalent)


def test_direct_suite_constructor_stores_supplied_fields_without_aggregation() -> None:
    """Keep the documented storage constructor distinct from from_results.

    A supplied aggregate may intentionally disagree with incomplete results;
    construction must preserve both objects instead of recalculating either.
    """
    results = (completed('single_core', 0.5),)
    scores = AggregatedScores(pi_equivalent=123.45678901234567)
    suite = SuiteResult(results, scores)
    assert suite.results is results
    assert suite.aggregated_scores is scores


def test_direct_suite_constructor_remains_native_free_in_a_fresh_interpreter() -> None:
    """The storage constructor must work without Rust, unlike the computing factory.

    A fresh process detects accidental eager imports or native recalculation
    that an already imported/cached test process could conceal.
    """
    program = """
import ctypes
def forbidden(*args, **kwargs):
    raise AssertionError('storage construction must not load Rust')
ctypes.CDLL = forbidden
from benchset import AggregatedScores, BenchmarkResult, SuiteResult
results = (BenchmarkResult('single_core'),)
scores = AggregatedScores(123.45678901234567)
suite = SuiteResult(results, scores)
assert suite.results is results and suite.aggregated_scores is scores
"""
    result = subprocess.run([sys.executable, '-c', program], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert not result.stdout
    assert not result.stderr


@pytest.mark.parametrize('missing', [(0,), (1,), (0, 1)])
def test_aggregate_requires_each_ratio_independently(missing: Tuple[int, ...]) -> None:
    """Missing first, second or both coefficients cannot become present zero sentinels."""
    results = [completed('single_core', 0.0), completed('multi_core', 2.0)]
    for position in missing:
        results[position] = replace(results[position], pi_equivalent=None)
    assert SuiteResult.from_results(tuple(results)).aggregated_scores.pi_equivalent is None


@pytest.mark.parametrize('position', [0, 1])
def test_aggregate_negative_zero_is_present_canonical_positive_zero(position: int) -> None:
    """A negative-zero input stays present but produces the documented positive-zero aggregate."""
    results = [completed('single_core', 0.5), completed('multi_core', 2.0)]
    results[position] = replace(results[position], pi_equivalent=-0.0)
    value = SuiteResult.from_results(tuple(results)).aggregated_scores.pi_equivalent
    assert value is not None
    assert value == 0.0
    assert math.copysign(1.0, value) == 1.0


def test_aggregation_is_available_inside_active_benchmark_callbacks() -> None:
    """Pure aggregation must remain usable while Rust's measurement runner is busy.

    Computing inside the first active progress callback catches an accidental
    dependency on the single-run reservation; cancellation then keeps it fast
    and a subsequent real run verifies that aggregation did not disturb ownership.
    """
    runner = BenchmarkRunner()
    values = (completed('single_core', 0.5), completed('multi_core', 2.0))
    aggregates: List[Optional[float]] = []

    def progress(value: Progress) -> None:
        if value.phase in ('warmup', 'measurement'):
            aggregates.append(SuiteResult.from_results(values).aggregated_scores.pi_equivalent)
            runner.cancel()

    result = runner.run('single_core', warmup_seconds=60, measurement_seconds=1, on_progress=progress)
    assert result.status == 'cancelled'
    assert aggregates == [1.0]
    assert runner.run('single_core', warmup_seconds=0, measurement_seconds=0.05).success


@pytest.mark.parametrize(('status', 'success', 'expected'), [
    ('completed', False, None), ('failed', True, 2.0), ('unknown', True, 2.0),
])
def test_aggregate_preserves_overridden_success(status: str, success: bool, expected: Optional[float]) -> None:
    """The public factory respects a typed result subclass's success property, as before."""
    class Result(BenchmarkResult):
        @property
        def success(self) -> bool:
            return success

    results = (Result('single_core', status=status, pi_equivalent=1),
               Result('multi_core', status=status, pi_equivalent=4))
    suite = SuiteResult.from_results(results)
    assert suite.results is results
    assert suite.aggregated_scores.pi_equivalent == expected


@pytest.mark.parametrize('error_type', [ValueError, RuntimeError, OverflowError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_aggregate_preserves_success_property_exceptions(error_type: Type[BaseException]) -> None:
    """A property error crosses back from Rust with its original Python type and message."""
    class Result(BenchmarkResult):
        @property
        def success(self) -> bool:
            raise error_type('custom success failed')

    results = (Result('single_core', status='completed', pi_equivalent=1), completed('multi_core', 4))
    with pytest.raises(error_type, match='custom success failed'):
        SuiteResult.from_results(results)


@pytest.mark.parametrize('first', [None, math.nan, -1.0])
def test_aggregate_does_not_read_success_after_an_invalid_ratio(first: Optional[float]) -> None:
    """An earlier unavailable coefficient hides later properties, preserving short-circuit order."""
    class Result(BenchmarkResult):
        @property
        def success(self) -> bool:
            raise AssertionError('later success must not be evaluated')

    results = (replace(completed('single_core', 1), pi_equivalent=first),
               Result('multi_core', status='completed', pi_equivalent=4))
    assert SuiteResult.from_results(results).aggregated_scores.pi_equivalent is None


@pytest.mark.parametrize(('case', 'expect_error'), [
    ('incomplete', False), ('extra', False), ('reordered', False), ('unknown', False),
    ('failed-first', False), ('cancelled-second', False), ('missing-first', False),
    ('nan-first', False), ('negative-first', False), ('huge-before-missing', True),
    ('huge-before-failed', True), ('negative-huge-before-zero', True),
])
def test_aggregate_preserves_short_circuit_before_numeric_conversion(case: str, expect_error: bool) -> None:
    """Preserve which coefficients the public factory converts before rejecting a suite.

    Integers satisfy the float annotation, but an enormous integer cannot become
    a C double. Structural rejection and earlier failed/missing/invalid values
    must hide unused conversions, while an earlier enormous value must still
    raise even if a later value would make the aggregate unavailable.
    """
    huge = completed('single_core', 10**1000)
    multi = completed('multi_core', 2)
    cases: Dict[str, Tuple[BenchmarkResult, ...]] = {
        'incomplete': (huge,),
        'extra': (huge, multi, multi),
        'reordered': (replace(huge, benchmark='multi_core'), completed('single_core', 1)),
        'unknown': (huge, replace(multi, benchmark='unknown')),
        'failed-first': (replace(huge, status='failed'), multi),
        'cancelled-second': (completed('single_core', .5), replace(multi, status='cancelled', pi_equivalent=10**1000)),
        'missing-first': (replace(huge, pi_equivalent=None), replace(multi, pi_equivalent=10**1000)),
        'nan-first': (replace(huge, pi_equivalent=math.nan), replace(multi, pi_equivalent=10**1000)),
        'negative-first': (replace(huge, pi_equivalent=-1), replace(multi, pi_equivalent=10**1000)),
        'huge-before-missing': (huge, replace(multi, pi_equivalent=None)),
        'huge-before-failed': (huge, replace(multi, status='failed')),
        'negative-huge-before-zero': (replace(huge, pi_equivalent=-(10**1000)), replace(multi, pi_equivalent=0)),
    }
    results = cases[case]
    if expect_error:
        with pytest.raises(OverflowError, match='int too large to convert to float'):
            SuiteResult.from_results(results)
    else:
        suite = SuiteResult.from_results(results)
        assert suite.results is results
        assert suite.aggregated_scores.pi_equivalent is None


@pytest.mark.parametrize(('single', 'multi', 'expected'), [
    (sys.float_info.max, sys.float_info.max, sys.float_info.max),
    (float.fromhex('0x0.0000000000001p-1022'), float.fromhex('0x0.0000000000001p-1022'),
     float.fromhex('0x0.0000000000001p-1022')),
    (sys.float_info.max, float.fromhex('0x0.0000000000001p-1022'),
     math.sqrt(sys.float_info.max) * math.sqrt(float.fromhex('0x0.0000000000001p-1022'))),
])
def test_aggregate_preserves_actual_finite_float_endpoints(single: float, multi: float, expected: float) -> None:
    """True maximum and minimum-subnormal doubles must survive finite log-space aggregation.

    Taking square roots separately supplies an independent oracle without the
    overflow/underflow that a direct product would introduce at these endpoints.
    """
    value = SuiteResult.from_results((completed('single_core', single), completed('multi_core', multi)))
    assert value.aggregated_scores.pi_equivalent is not None
    assert math.isclose(value.aggregated_scores.pi_equivalent, expected, rel_tol=1e-12, abs_tol=0)


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'unknown'])
@pytest.mark.parametrize('position', [0, 1])
def test_aggregate_rejects_unsuccessful_results_with_retained_ratios(status: str, position: int) -> None:
    """Reject status independently of ratio presence, at either suite position.

    Manually assembled snapshots can retain valid coefficients after a status
    change; those values must never turn a failed or cancelled suite into success.
    """
    results = [completed('single_core', 0.5), completed('multi_core', 2.0)]
    results[position] = replace(results[position], status=status)
    assert SuiteResult.from_results(tuple(results)).aggregated_scores.pi_equivalent is None


@pytest.mark.parametrize(('single', 'multi', 'expected', 'tolerance'), [
    (0.5, 2.0, 1.0, 1e-15), (0.0, 0.0, 0.0, 1e-15),
    (1.0, 1.0, 1.0, 1e-15), (1.0, 4.0, 2.0, 1e-15), (4.0, 1.0, 2.0, 1e-15),
    (0.0, 2.0, 0.0, 1e-15), (2.0, 0.0, 0.0, 1e-15),
    (1 / 3, 2 / 3, math.sqrt(2) / 3, 1e-15), (1e-300, 1e300, 1.0, 1e-12),
    (1e300, 1e300, 1e300, 1e-12), (1e-300, 1e-300, 1e-300, 1e-12),
])
def test_suite_aggregates_ratios_and_preserves_results(
    single: float, multi: float, expected: float, tolerance: float,
) -> None:
    """Preserve immutable results and public JSON keys while using unrounded ratios.

    The ordinary-ratio oracle catches premature rounding; extreme exponents
    allow log/exp roundoff while still rejecting direct-product overflow.
    Unit coefficients keep the calibration baseline at one, never at zero.
    """
    results = (replace(completed('single_core', single), unstable=True), completed('multi_core', multi))
    suite = SuiteResult.from_results(results)
    assert suite.results is results
    assert isinstance(suite.results, tuple)
    equivalent = suite.aggregated_scores.pi_equivalent
    assert equivalent is not None
    assert math.isclose(equivalent, expected, rel_tol=tolerance, abs_tol=0)
    if expected in (0.0, 1.0):
        assert equivalent == expected
    serialized = json.loads(json.dumps(asdict(suite), allow_nan=False))
    expected_results = json.loads(json.dumps([asdict(result) for result in results], allow_nan=False))
    assert serialized == {'results': expected_results, 'aggregated_scores': {'pi_equivalent': equivalent}}
    for obj, field in [(suite, 'results'), (suite.aggregated_scores, 'pi_equivalent')]:
        with pytest.raises(FrozenInstanceError):
            setattr(obj, field, None)
    assert AggregatedScores().pi_equivalent is None


@pytest.mark.parametrize('invalid', [-1.0, math.inf, -math.inf, math.nan])
@pytest.mark.parametrize('other', [0.0, 1.0])
@pytest.mark.parametrize('invalid_index', [0, 1])
def test_suite_rejects_invalid_ratios(invalid: float, other: float, invalid_index: int) -> None:
    """Validate every coefficient before a zero can hide a malformed suite.

    Both positions prevent first-only validation from accepting a bad second ratio.
    """
    single, multi = (invalid, other) if invalid_index == 0 else (other, invalid)
    suite = SuiteResult.from_results((completed('single_core', single), completed('multi_core', multi)))
    assert suite.aggregated_scores.pi_equivalent is None


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'completed'])
def test_no_full_score_for_partial_missing_or_invalid_suite(status: str) -> None:
    """Exclude incomplete, reordered, duplicated and missing-ratio result sets."""
    single, multi = completed('single_core', 0.5), completed('multi_core', 2)
    for results in ((), (single,), (multi, single), (single, single), (single, multi, multi),
                    (single, replace(multi, status=status, pi_equivalent=None))):
        assert SuiteResult.from_results(results).aggregated_scores.pi_equivalent is None


@pytest.mark.parametrize('stop', [0, 1])
@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled'])
def test_run_all_returns_suite_and_stops_on_failure(monkeypatch: pytest.MonkeyPatch, stop: int, status: str) -> None:
    """Keep full snapshots, including completed unstable results, and stop at either failed position.

    Distinct samples and diagnostics catch lossy reconstruction while packing
    the public suite result, including after failure or cancellation.
    """
    calls: List[str] = []
    produced: List[BenchmarkResult] = []
    callback = lambda _progress: None

    def execute(name: str, *, warmup_seconds: float, measurement_seconds: float,
                on_progress: Optional[Callable[[Progress], None]]) -> BenchmarkResult:
        assert (warmup_seconds, measurement_seconds, on_progress) == (0.25, 0.75, callback)
        current_status = status if len(calls) == stop else 'completed'
        calls.append(name)
        ordinal = len(calls)
        result = replace(
            completed(name, 0.5 if name == 'single_core' else 2), status=current_status,
            reason=None if current_status == 'completed' else name + ': stopped',
            workers=ordinal, warmup_seconds=warmup_seconds, measurement_seconds=measurement_seconds,
            elapsed_seconds=0.5, iterations=ordinal * 16, checksum=2**64 - ordinal,
            samples=(Sample(.25, .25, ordinal * 8, ordinal * 32),
                     Sample(.5, .25, ordinal * 8, ordinal * 32)),
            variation_percent=12.5 if current_status == 'completed' else None,
            unstable=current_status == 'completed',
        )
        produced.append(result)
        return result

    monkeypatch.setattr(BenchmarkRunner, 'run', lambda _self, name, **options: execute(name, **options))
    mock_suite(monkeypatch)
    suite = run_all(warmup_seconds=0.25, measurement_seconds=0.75, on_progress=callback)
    assert isinstance(suite, SuiteResult)
    assert calls == list(BENCHMARKS[:2 if status == 'completed' else stop + 1])
    assert tuple(result.benchmark for result in suite.results) == tuple(calls)
    assert suite.results == tuple(produced)
    assert suite.aggregated_scores.pi_equivalent == (1.0 if status == 'completed' else None)


def test_actual_suite_aggregate() -> None:
    """Check the public native-backed path against an independent geometric mean."""
    suite = run_all(warmup_seconds=0, measurement_seconds=0.08)
    if all(result.success for result in suite.results):
        single, multi = suite.results
        assert single.pi_equivalent is not None
        assert multi.pi_equivalent is not None
        equivalent = suite.aggregated_scores.pi_equivalent
        assert equivalent is not None
        assert math.isclose(equivalent, math.sqrt(single.pi_equivalent * multi.pi_equivalent), rel_tol=1e-12)
    else:
        assert suite.aggregated_scores.pi_equivalent is None


@pytest.mark.parametrize('encoding', ['ascii', 'utf-8'])
@pytest.mark.parametrize(('single', 'expected'), [(None, '—'), (0.0, '0 \u00d7 pi'), (0.5, '1 \u00d7 pi')])
@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_cli_summary_encoding(encoding: str, single: Optional[float], expected: str, newline: str) -> None:
    """Render missing, zero and positive aggregates with both stream encodings.

    A valid zero must not disappear through a truthiness-based absence check.
    Explicit LF and CRLF translation keeps byte checks portable without losing
    the required final newline or concealing duplicate carriage returns.
    """
    results = (completed('single_core', single), completed('multi_core', 2)) if single is not None else ()
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding=encoding, newline=newline)
    cli.render_suite(SuiteResult.from_results(results), stream)
    stream.flush()
    text = buffer.getvalue().decode(encoding)
    stream.detach()
    if encoding == 'ascii':
        expected = expected.replace('—', '-').replace('\u00d7', 'x')
    assert text == 'Full Pi equivalent : ' + expected + newline


def test_interrupted_completed_cli_suite_has_no_aggregate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A late interrupt may keep completed diagnostics but cannot publish a full score."""
    def execute(_runner: BenchmarkRunner, name: str, **_options: object) -> BenchmarkResult:
        if name == 'multi_core':
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(signal.SIGINT, None)
        return completed(name, 0.5 if name == 'single_core' else 2.0)

    monkeypatch.setattr(BenchmarkRunner, 'run', execute)
    mock_suite(monkeypatch)
    assert cli.main(['--bench=all', '--warmup=0', '--duration=.01']) == 130
    output = capsys.readouterr()
    assert 'single_core' in output.out
    assert 'multi_core' in output.err
    assert 'completed' in output.err
    assert 'Full Pi equivalent : —' in output.err
