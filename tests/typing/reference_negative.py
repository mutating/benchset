"""Intentional consumer errors: required metadata types and frozen fields."""

from benchset import (
    PI_REFERENCE_MACHINE,
    BenchmarkResult,
    ReferenceMachine,
    ReferenceRun,
)

result = BenchmarkResult('single_core')
machine: ReferenceMachine = result.reference_run
reference: ReferenceRun = result.reference_machine
BenchmarkResult('single_core', reference_machine=None)
BenchmarkResult('single_core', reference_run=None)
result.reference_machine = PI_REFERENCE_MACHINE
