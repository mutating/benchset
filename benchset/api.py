"""Python API with cancellation and a synchronized native-handle lifetime."""

import math
import signal
from dataclasses import replace
from threading import RLock, current_thread, main_thread
from types import FrameType
from typing import Callable, Optional

from benchset._native import Native
from benchset.models import (
    BENCHMARKS,
    DEFAULT_MEASUREMENT_SECONDS,
    DEFAULT_WARMUP_SECONDS,
    BenchmarkResult,
    Progress,
    SuiteResult,
)


class BenchmarkRunner:
    """Reusable runner. Run in a background thread and cancel from any thread.

    A runner permits one active call. Progress callbacks run on that call's
    thread and should return promptly. There is no automatic GUI dispatch.
    """

    def __init__(self) -> None:
        # User signal handlers can reenter cancel() on the calling thread.
        self._lock = RLock()
        self._active: Optional[object] = None
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

    def _release(self, owner: object, native: Optional[Native], handle: Optional[int]) -> None:
        with self._lock:
            if self._active is owner:
                self._handle = None
                self._native = None
                if native is not None and handle is not None:
                    native.free(handle)
                self._active = None

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
            return replace(failure, reason='unknown benchmark: ' + benchmark)
        if not math.isfinite(warmup_seconds) or warmup_seconds < 0:
            return replace(
                failure, reason='warmup_seconds must be finite and nonnegative',
            )
        if not math.isfinite(measurement_seconds) or measurement_seconds <= 0:
            return replace(
                failure, reason='measurement_seconds must be finite and positive',
            )
        native: Optional[Native] = None
        handle: Optional[int] = None
        interrupted = False
        owner = object()

        def interrupt(_signum: int, _frame: Optional[FrameType]) -> None:
            nonlocal interrupted
            interrupted = True
            # The runner lock may already be held. Native cancellation is an
            # atomic operation; the local handle is valid until cleanup starts.
            if native is not None and handle is not None:
                native.cancel(handle)

        handles_signals = (
            current_thread() is main_thread()
            and signal.getsignal(signal.SIGINT) is signal.default_int_handler
        )
        try:
            if handles_signals:
                signal.signal(signal.SIGINT, interrupt)
            try:
                with self._lock:
                    if self._active:
                        return replace(failure, reason='this runner is already running')
                    self._cancelled = False
                    # One assignment publishes both activity and ownership,
                    # even if a user signal handler raises immediately after.
                    self._active = owner
                native = Native()
                handle = native.create()
                with self._lock:
                    self._native = native
                    self._handle = handle
                    if self._cancelled or interrupted:
                        native.cancel(handle)
                return native.execute(
                    handle, benchmark, warmup_seconds, measurement_seconds, on_progress,
                )
            except (OSError, RuntimeError, OverflowError) as error:
                return replace(failure, reason=str(error))
            finally:
                # Stop signal-driven cancellation before freeing this call's
                # handle. _release preserves a concurrent call's ownership.
                current, handle = handle, None
                self._release(owner, native, current)
        finally:
            if handles_signals and signal.getsignal(signal.SIGINT) is interrupt:
                signal.signal(signal.SIGINT, signal.default_int_handler)
            if interrupted:
                # Raising inside a ctypes callback would lose the exception.
                # Wait until native workers are joined and the handle is freed.
                raise KeyboardInterrupt


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
) -> SuiteResult:
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
    return SuiteResult.from_results(tuple(results))
