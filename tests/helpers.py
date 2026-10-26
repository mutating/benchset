"""Shared checks for result metadata and reusable native runners."""

from benchset import (
    PI_REFERENCE_MACHINE,
    PI_REFERENCE_RUN,
    BenchmarkResult,
    BenchmarkRunner,
    Progress,
    ReferenceMachine,
    ReferenceRun,
)


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
