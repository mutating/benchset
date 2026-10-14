"""Sustained CPU throughput, backed by the same Rust core as the C API."""

from benchset.api import (
    BenchmarkRunner as BenchmarkRunner,
    run as run,
    run_all as run_all,
)
from benchset.models import (
    BENCHMARKS as BENCHMARKS,
    DEFAULT_MEASUREMENT_SECONDS as DEFAULT_MEASUREMENT_SECONDS,
    DEFAULT_WARMUP_SECONDS as DEFAULT_WARMUP_SECONDS,
    BenchmarkResult as BenchmarkResult,
    Progress as Progress,
    Sample as Sample,
    __version__ as __version__,
)
