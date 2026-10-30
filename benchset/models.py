"""Public, immutable measurement objects."""

import math
from dataclasses import dataclass
from importlib.metadata import version
from typing import Optional, Tuple

from benchset.reference import (
    PI_REFERENCE_MACHINE,
    PI_REFERENCE_RUN,
    ReferenceMachine,
    ReferenceRun,
)

__version__ = version('benchset')
BENCHMARKS = ('single_core', 'multi_core')
DEFAULT_WARMUP_SECONDS = 120.0
DEFAULT_MEASUREMENT_SECONDS = 60.0


@dataclass(frozen=True)
class Progress:
    benchmark: str
    phase: str
    workers: int
    fraction: float
    phase_elapsed_seconds: float
    phase_duration_seconds: float
    iterations: int


@dataclass(frozen=True)
class Sample:
    elapsed_seconds: float
    duration_seconds: float
    iterations: int
    iterations_per_second: float


@dataclass(frozen=True)
class BenchmarkResult:
    """Measurement with an optional double-precision Pi multiplier.

    pi_equivalent is score / the same benchmark's reference score. Completed
    results retain it even when unstable; failed/cancelled results use None.
    reference_machine and reference_run share the immutable global calibration
    for every status, including failed and cancelled results.
    """

    benchmark: str
    version: str = __version__
    status: str = 'failed'
    reason: Optional[str] = None
    score: Optional[float] = None
    unit: str = 'iterations/s'
    workers: int = 0
    warmup_seconds: float = DEFAULT_WARMUP_SECONDS
    measurement_seconds: float = DEFAULT_MEASUREMENT_SECONDS
    elapsed_seconds: float = 0.0
    iterations: int = 0
    checksum: int = 0
    samples: Tuple[Sample, ...] = ()
    variation_percent: Optional[float] = None
    unstable: bool = False
    pi_equivalent: Optional[float] = None
    reference_machine: ReferenceMachine = PI_REFERENCE_MACHINE
    reference_run: ReferenceRun = PI_REFERENCE_RUN

    @property
    def success(self) -> bool:
        return self.status == 'completed'


@dataclass(frozen=True)
class AggregatedScores:
    """Full-suite scores; Pi equivalent is the geometric mean of all ratios."""

    pi_equivalent: Optional[float] = None


@dataclass(frozen=True)
class SuiteResult:
    results: Tuple[BenchmarkResult, ...]
    aggregated_scores: AggregatedScores

    @classmethod
    def from_results(cls, results: Tuple[BenchmarkResult, ...]) -> 'SuiteResult':
        complete = (
            tuple(result.benchmark for result in results) == BENCHMARKS
            and all(
                result.success and result.pi_equivalent is not None
                and math.isfinite(result.pi_equivalent) and result.pi_equivalent >= 0
                for result in results
            )
        )
        equivalent: Optional[float] = None
        if complete:
            equivalent = (
                0.0 if any(result.pi_equivalent == 0 for result in results)
                else math.exp(sum(math.log(result.pi_equivalent) for result in results
                                  if result.pi_equivalent is not None) / len(results))
            )
        return cls(results, AggregatedScores(pi_equivalent=equivalent))
