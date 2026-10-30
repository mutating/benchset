"""Full-suite result shape, aggregation, early stopping and console summary."""

import io
import json
import math
import signal
from dataclasses import FrozenInstanceError, asdict, replace
from typing import Callable, List, Optional

import pytest

from benchset import (
    BENCHMARKS,
    AggregatedScores,
    BenchmarkResult,
    BenchmarkRunner,
    Progress,
    Sample,
    SuiteResult,
    api,
    cli,
    run_all,
)


def completed(name: str, equivalent: float) -> BenchmarkResult:
    return BenchmarkResult(name, status='completed', score=999, pi_equivalent=equivalent)


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

    monkeypatch.setattr(api, 'run', execute)
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
    assert cli.main(['--bench=all', '--warmup=0', '--duration=.01']) == 130
    output = capsys.readouterr()
    assert 'single_core' in output.out
    assert 'multi_core' in output.err
    assert 'completed' in output.err
    assert 'Full Pi equivalent : —' in output.err
