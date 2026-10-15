"""Public, immutable measurement objects."""

from dataclasses import dataclass
from typing import Optional, Tuple

__version__ = '0.0.2'
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

    @property
    def success(self) -> bool:
        return self.status == 'completed'
