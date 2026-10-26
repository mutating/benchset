"""Reference metadata is available without Optional narrowing or native code."""

from dataclasses import replace
from typing import Tuple

from benchset import (
    PI_REFERENCE_MACHINE,
    PI_REFERENCE_RUN,
    BenchmarkResult,
    BenchmarkRunner,
    ReferenceMachine,
    ReferenceRun,
    run,
    run_all,
)


def metadata(result: BenchmarkResult) -> Tuple[ReferenceMachine, ReferenceRun]:
    machine: ReferenceMachine = result.reference_machine
    reference: ReferenceRun = result.reference_run
    score: float = reference.results[0].score
    memory: int = machine.memory_bytes
    assert score > 0
    assert memory > 0
    return machine, reference


def main() -> None:
    results = (
        BenchmarkResult('single_core'),
        BenchmarkRunner().run('unknown'),
        run('multi_core', measurement_seconds=0),
        *run_all(warmup_seconds=-1),
    )
    for result in results:
        machine, reference = metadata(result)
        assert machine is PI_REFERENCE_MACHINE
        assert reference is PI_REFERENCE_RUN
    custom_machine = replace(PI_REFERENCE_MACHINE, name='consumer machine')
    custom_run = replace(PI_REFERENCE_RUN, warmup_seconds=1.0)
    custom = BenchmarkResult('single_core', reference_machine=custom_machine, reference_run=custom_run)
    assert metadata(custom) == (custom_machine, custom_run)


if __name__ == '__main__':
    main()
