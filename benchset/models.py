"""Public, immutable measurement objects."""

from dataclasses import dataclass
from importlib.metadata import version
from typing import Optional, Tuple, cast

from benchset._metadata import DATA
from benchset.reference import (
    PI_REFERENCE_MACHINE,
    PI_REFERENCE_RUN,
    ReferenceMachine,
    ReferenceRun,
)

__version__ = version('benchset')
_benchmarks = tuple(benchmark['name'] for benchmark in DATA['benchmarks'])
BENCHMARKS = cast(Tuple[str, str], _benchmarks)
DEFAULT_WARMUP_SECONDS = DATA['default_warmup_seconds']
DEFAULT_MEASUREMENT_SECONDS = DATA['default_measurement_seconds']


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
        # Load Rust only when computing, preserving the native-free constructor.
        from benchset._aggregate import aggregate  # noqa: PLC0415

        return cls(results, AggregatedScores(pi_equivalent=aggregate(results)))
