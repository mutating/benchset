"""Shared checks for result metadata and reusable native runners."""

from typing import Callable, List, Optional

import pytest

from benchset import (
    BENCHMARKS,
    PI_REFERENCE_MACHINE,
    PI_REFERENCE_RUN,
    BenchmarkResult,
    BenchmarkRunner,
    Progress,
    ReferenceMachine,
    ReferenceRun,
    SuiteResult,
)


def mock_suite(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adapt single-result test fakes to the native suite callback boundary.

    These fakes exercise host forwarding/rendering; Rust's real suite ordering
    and stopping are covered independently by core and integration tests.
    """
    def execute(
        runner: BenchmarkRunner, warmup_seconds: float, measurement_seconds: float,
        on_progress: Optional[Callable[[Progress], None]],
        on_result: Optional[Callable[[BenchmarkResult], bool]] = None,
    ) -> SuiteResult:
        results: List[BenchmarkResult] = []
        for name in BENCHMARKS:
            try:
                result = runner.run(name, warmup_seconds=warmup_seconds,
                                    measurement_seconds=measurement_seconds, on_progress=on_progress)
            except Exception as error:  # noqa: BLE001 — Emulate the public runner failure boundary.
                result = BenchmarkResult(name, reason=str(error))
            results.append(result)
            stop = on_result(result) if on_result is not None else False
            if not result.success or stop:
                break
        return SuiteResult.from_results(tuple(results))

    monkeypatch.setattr(BenchmarkRunner, '_run_all', execute)


def assert_result_references(result: BenchmarkResult) -> None:
    assert isinstance(result.reference_machine, ReferenceMachine)
    assert isinstance(result.reference_run, ReferenceRun)
    assert result.reference_machine is PI_REFERENCE_MACHINE
    assert result.reference_run is PI_REFERENCE_RUN


def assert_runner_reusable(runner: BenchmarkRunner) -> None:
    started = False

    def cancel(progress: Progress) -> None:
        nonlocal started
        if progress.phase in ('warmup', 'measurement'):
            started = True
            runner.cancel()

    result = runner.run('single_core', warmup_seconds=10, measurement_seconds=10, on_progress=cancel)
    assert_result_references(result)
    assert started, result.reason
    assert result.status == 'cancelled', result.reason
    assert result.score is None
    assert result.pi_equivalent is None
