"""Python API with cancellation and a synchronized native-handle lifetime."""

import signal
from dataclasses import dataclass as _dataclass, replace
from threading import RLock, current_thread, main_thread
from types import FrameType
from typing import Callable, Optional, Tuple, TypeVar, Union, cast

from benchset._native import (
    Native,
    _HostComparisonError,
    _raise_original,
    _SuiteConfigurationError,
)
from benchset.models import (
    BENCHMARKS,
    DEFAULT_MEASUREMENT_SECONDS,
    DEFAULT_WARMUP_SECONDS,
    AggregatedScores,
    BenchmarkResult,
    Progress,
    SuiteResult,
)

_HostValue = TypeVar('_HostValue')


@_dataclass(eq=False)
class _RunState:
    """Keep cancellation with its call while host validation yields the runner."""

    cancelled: bool = False


class BenchmarkRunner:
    """Reusable runner. Run in a background thread and cancel from any thread.

    A runner permits one active call. Progress callbacks run on that call's
    thread and should return promptly. There is no automatic GUI dispatch.
    """

    def __init__(self) -> None:
        # User signal handlers can reenter cancel() on the calling thread.
        self._lock = RLock()
        self._active: Optional[_RunState] = None
        self._native: Optional[Native] = None
        self._handle: Optional[int] = None
        self._suite = False
        self._on_native: Optional[Callable[[Native], None]] = None

    def cancel(self) -> None:
        """Cancel the active call, including native initialization or warmup."""
        with self._lock:
            if self._active:
                self._active.cancelled = True
            if self._native is not None and self._handle is not None:
                self._native.cancel(self._handle, True) if self._suite else self._native.cancel(self._handle)

    def _release(self, owner: _RunState, native: Optional[Native], handle: Optional[int]) -> None:
        with self._lock:
            if self._active is owner:
                self._handle = None
                self._native = None
                if native is not None and handle is not None:
                    native.free(handle, True) if self._suite else native.free(handle)
                self._active = None

    def run(
        self,
        benchmark: str,
        *,
        warmup_seconds: float = DEFAULT_WARMUP_SECONDS,
        measurement_seconds: float = DEFAULT_MEASUREMENT_SECONDS,
        on_progress: Optional[Callable[[Progress], None]] = None,
    ) -> BenchmarkResult:
        try:
            return cast(BenchmarkResult, self._run(benchmark, warmup_seconds, measurement_seconds, on_progress))
        except _HostComparisonError as error:
            _raise_original(error.error)

    def _run_all(
        self, warmup_seconds: float, measurement_seconds: float,
        on_progress: Optional[Callable[[Progress], None]],
        on_result: Optional[Callable[[BenchmarkResult], bool]] = None,
    ) -> SuiteResult:
        try:
            return cast(SuiteResult, self._run(BENCHMARKS[0], warmup_seconds, measurement_seconds,
                                              on_progress, on_result, suite=True))
        except (_SuiteConfigurationError, _HostComparisonError) as error:
            # _run has released its native handle and restored signal handling.
            _raise_original(error.error)

    def _run(  # noqa: PLR0913, PLR0915 — One lifetime owner for both execution modes and signal cleanup.
        self, benchmark: str, warmup_seconds: float, measurement_seconds: float,
        on_progress: Optional[Callable[[Progress], None]],
        on_result: Optional[Callable[[BenchmarkResult], bool]] = None,
        *, suite: bool = False,
    ) -> Union[BenchmarkResult, SuiteResult]:
        raw_windows = (warmup_seconds, measurement_seconds)
        warmup_seconds = float(warmup_seconds)
        measurement_seconds = float(measurement_seconds)
        if not isinstance(benchmark, str):
            raise TypeError('benchmark must be a string')
        name = benchmark
        failure = BenchmarkResult(
            benchmark=name,
            warmup_seconds=warmup_seconds,
            measurement_seconds=measurement_seconds,
        )
        native: Optional[Native] = None
        handle: Optional[int] = None
        interrupted = False
        host_failed = False
        owner = _RunState()

        def failed(reason: str) -> Union[BenchmarkResult, SuiteResult]:
            result = replace(failure, reason=reason)
            if suite and on_result is not None:
                on_result(result)
            return SuiteResult((result,), AggregatedScores()) if suite else result

        def completed(result: BenchmarkResult) -> bool:
            return interrupted or (on_result(result) if on_result is not None else False)

        def interrupt(_signum: int, _frame: Optional[FrameType]) -> None:
            nonlocal interrupted
            interrupted = True
            # The runner lock may already be held. Native cancellation is an
            # atomic operation; the local handle is valid until cleanup starts.
            if native is not None and handle is not None:
                native.cancel(handle, True) if suite else native.cancel(handle)

        on_main_thread = current_thread() is main_thread()

        def acquire_signals() -> None:
            # Caller operators may select a new policy before each execution.
            if on_main_thread and signal.getsignal(signal.SIGINT) is signal.default_int_handler:
                signal.signal(signal.SIGINT, interrupt)

        def caller_operation(operation: Callable[[], _HostValue]) -> _HostValue:
            nonlocal host_failed
            owns_signals = on_main_thread and signal.getsignal(signal.SIGINT) is interrupt
            try:
                if owns_signals:
                    signal.signal(signal.SIGINT, signal.default_int_handler)
                return operation()
            except BaseException:
                # Retain the caller's exception through the native cleanup boundary.
                host_failed = True
                raise
            finally:
                if owns_signals and signal.getsignal(signal.SIGINT) is signal.default_int_handler:
                    signal.signal(signal.SIGINT, interrupt)

        def convert_windows() -> Tuple[float, float]:
            try:
                # Both conversions precede the next child's signal ownership.
                return caller_operation(lambda: (float(raw_windows[0]), float(raw_windows[1])))
            finally:
                # Also protect shared-handle cleanup after a caller error.
                acquire_signals()

        try:
            acquire_signals()
            try:
                with self._lock:
                    busy = self._active is not None
                    active_native = self._native
                    if not busy:
                        # Publish activity before loading so cancellation is sticky.
                        self._active = owner
                        # Reentrant signals before the claim may finish another mode.
                        self._suite = suite
                native = active_native if busy and active_native is not None else Native()
                if self._on_native is not None:
                    self._on_native(native)
                with self._lock:
                    if self._active is owner:
                        # Caller operators preceded runner ownership before native validation.
                        self._active = None
                reason = caller_operation(
                    lambda: (native.validation_error(name, warmup_seconds, measurement_seconds)
                             if type(name) is str else
                             native.validation_error(name, warmup_seconds, measurement_seconds, wrap_host=True)),
                )
                if reason is not None:
                    return failed(reason)
                acquire_signals()
                with self._lock:
                    busy = self._active is not None
                    if not busy:
                        self._active = owner
                        self._suite = suite
                if busy:
                    return failed('this runner is already running')
                handle = native.create(True) if suite else native.create()
                with self._lock:
                    self._native = native
                    self._handle = handle
                    if owner.cancelled or interrupted:
                        native.cancel(handle, True) if suite else native.cancel(handle)
                if suite:
                    return native.execute_suite(handle, warmup_seconds, measurement_seconds,
                                                on_progress, completed, convert_windows)
                return native.execute(
                    handle, name, warmup_seconds, measurement_seconds, on_progress,
                )
            except (OSError, RuntimeError, OverflowError) as error:
                host_failed = False
                return failed(str(error))
            finally:
                # Stop signal-driven cancellation before freeing this call's
                # handle. _release preserves a concurrent call's ownership.
                current, handle = handle, None
                self._release(owner, native, current)
        finally:
            if on_main_thread and signal.getsignal(signal.SIGINT) is interrupt:
                signal.signal(signal.SIGINT, signal.default_int_handler)
            if interrupted and not host_failed:
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
    return BenchmarkRunner()._run_all(warmup_seconds, measurement_seconds, on_progress)
