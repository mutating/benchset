"""Immutable Pi calibration, mirrored from Rust for native-free inspection.

Integration tests compare every value with the Rust C ABI and calibration
fixture. The reference covers the six final runs, excluding preparatory
profiles and final cooling. Scores pool three repeats of each workload.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Tuple


@dataclass(frozen=True)
class ReferenceMachine:
    """Reference Pi identity; memory_bytes is nominal installed RAM capacity."""

    name: str
    short_name: str
    os_name: str
    os_version: str
    cpu_architecture: str
    memory_bytes: int
    cpu_cores: int


@dataclass(frozen=True)
class ReferenceBenchmarkResult:
    """Sustained iterations/s for one benchmark, pooled across three repeats."""

    benchmark: str
    score: float


@dataclass(frozen=True)
class ReferenceRun:
    """The final series, from its first warmup to its last measurement.

    The start is UTC Unix seconds; temperatures are Celsius at series boundaries.
    warmup_seconds applies before each reference measurement, not to runtime
    defaults. results contains one pooled score per benchmark, not per repeat.
    """

    started_at_unix_seconds: float
    start_temperature_c: float
    end_temperature_c: float
    warmup_seconds: float
    results: Tuple[ReferenceBenchmarkResult, ...]

    @property
    def started_at(self) -> datetime:
        """Timezone-aware UTC start; asdict retains JSON-compatible epoch seconds."""
        return datetime.fromtimestamp(self.started_at_unix_seconds, timezone.utc)


PI_REFERENCE_MACHINE = ReferenceMachine(
    name='Raspberry Pi 5 Model B Rev 1.0',
    short_name='pi',
    os_name='Ubuntu Server',
    os_version='26.04 LTS',
    cpu_architecture='aarch64',
    memory_bytes=8 * 1024 * 1024 * 1024,
    cpu_cores=4,
)

PI_REFERENCE_RUN = ReferenceRun(
    started_at_unix_seconds=1792767590.517391,
    start_temperature_c=41.35,
    end_temperature_c=66.65,
    warmup_seconds=1248.0,
    results=(
        ReferenceBenchmarkResult('single_core', 47231.227029007096),
        ReferenceBenchmarkResult('multi_core', 181953.17419139884),
    ),
)
