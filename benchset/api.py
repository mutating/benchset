"""Python API with cancellation and a synchronized native-handle lifetime."""

import math
from threading import Lock
from typing import Callable, Optional, Tuple

from benchset._native import Native
from benchset.models import (
    BENCHMARKS,
    DEFAULT_MEASUREMENT_SECONDS,
    DEFAULT_WARMUP_SECONDS,
    BenchmarkResult,
    Progress,
)


class BenchmarkRunner:
    """Reusable runner. Run in a background thread and cancel from any thread.

    A runner permits one active call. Progress callbacks run on that call's
    thread and should return promptly. There is no automatic GUI dispatch.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._active = False
        self._cancelled = False
        self._native: Optional[Native] = None
        self._handle: Optional[int] = None

    def cancel(self) -> None:
        """Cancel the active call, including native initialization or warmup."""
        with self._lock:
            if self._active:
                self._cancelled = True
            if self._native is not None and self._handle is not None:
                self._native.cancel(self._handle)

    def run(
        self,
        benchmark: str,
        *,
        warmup_seconds: float = DEFAULT_WARMUP_SECONDS,
        measurement_seconds: float = DEFAULT_MEASUREMENT_SECONDS,
        on_progress: Optional[Callable[[Progress], None]] = None,
    ) -> BenchmarkResult:
        warmup_seconds = float(warmup_seconds)
        measurement_seconds = float(measurement_seconds)
        failure = BenchmarkResult(
            benchmark=benchmark,
            warmup_seconds=warmup_seconds,
            measurement_seconds=measurement_seconds,
        )
        if benchmark not in BENCHMARKS:
            return self._failed(failure, 'unknown benchmark: ' + benchmark)
        if not math.isfinite(warmup_seconds) or warmup_seconds < 0:
            return self._failed(
                failure, 'warmup_seconds must be finite and nonnegative',
            )
        if not math.isfinite(measurement_seconds) or measurement_seconds <= 0:
            return self._failed(
                failure, 'measurement_seconds must be finite and positive',
            )
        with self._lock:
            if self._active:
                return self._failed(failure, 'this runner is already running')
            self._active = True
            self._cancelled = False
        try:
            native = Native()
            handle = native.create()
            with self._lock:
                self._native = native
                self._handle = handle
                if self._cancelled:
                    native.cancel(handle)
            return native.execute(
                handle, benchmark, warmup_seconds, measurement_seconds, on_progress,
            )
        except (OSError, RuntimeError, OverflowError) as error:
            return self._failed(failure, str(error))
        finally:
            with self._lock:
                if self._native is not None and self._handle is not None:
                    self._native.free(self._handle)
                self._handle = None
                self._native = None
                self._active = False

    @staticmethod
    def _failed(result: BenchmarkResult, reason: str) -> BenchmarkResult:
        from dataclasses import replace  # noqa: PLC0415

        return replace(result, reason=reason)


def run(
    benchmark: str,
    *,
    warmup_seconds: float = DEFAULT_WARMUP_SECONDS,
    measurement_seconds: float = DEFAULT_MEASUREMENT_SECONDS,
    on_progress: Optional[Callable[[Progress], None]] = None,
) -> BenchmarkResult:
    return BenchmarkRunner().run(
        benchmark,
        warmup_seconds=warmup_seconds,
        measurement_seconds=measurement_seconds,
        on_progress=on_progress,
    )


def run_all(
    *,
    warmup_seconds: float = DEFAULT_WARMUP_SECONDS,
    measurement_seconds: float = DEFAULT_MEASUREMENT_SECONDS,
    on_progress: Optional[Callable[[Progress], None]] = None,
) -> Tuple[BenchmarkResult, ...]:
    """Run single_core then multi_core; stop after failure or cancellation."""
    results = []
    for benchmark in BENCHMARKS:
        result = run(
            benchmark,
            warmup_seconds=warmup_seconds,
            measurement_seconds=measurement_seconds,
            on_progress=on_progress,
        )
        results.append(result)
        if not result.success:
            break
    return tuple(results)
