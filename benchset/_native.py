"""Private ctypes boundary. All measurement logic lives in Rust."""

import _ctypes
import ctypes
import signal
import sys
from _ctypes import CFuncPtr
from functools import lru_cache
from os.path import abspath
from pathlib import Path
from threading import current_thread, main_thread
from types import FrameType
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    List,
    Optional,
    Tuple,
    cast,
)

from benchset._metadata import DATA, canonical_name
from benchset._signal_state import (
    _direct_error,
    _get_signal,
    _native_sigaction,
    _raise_original as _raise_original,  # noqa: PLC0414 — Shared with the host adapters.
    _restore_sigint,
    _set_signal,
    _SignalHandler,
)
from benchset.models import (
    AggregatedScores,
    BenchmarkResult,
    Progress,
    Sample,
    SuiteResult,
    __version__,
)

ABI_VERSION = 1


if TYPE_CHECKING:  # pragma: no cover — Only the type checker uses this alias.
    _CDLL = ctypes.CDLL
else:
    _CDLL = ctypes.cdll._dlltype


class _Library(_CDLL):
    """Adopt the opened handle when modern ctypes delegates to its platform hook."""

    def _load_library(self, name: str, mode: int, handle: int, winmode: Optional[int]) -> int:  # noqa: ARG002 — ctypes hook signature.
        self._name = name
        return handle


@lru_cache(maxsize=1)
def _open_library(path: str) -> _CDLL:
    """Open before constructing ctypes types, rolling back handles that cannot be adopted."""
    if _AsDouble is None:
        return cast(_CDLL, cast(Any, ctypes.cdll)._dlltype(path))
    mode = ctypes.DEFAULT_MODE
    if sys.platform == 'win32':
        path = abspath(path)  # noqa: PTH100 — Match CDLL without resolving symlinks.
        mode = 0x1100  # CDLL's LOAD_LIBRARY_SEARCH_DEFAULT_DIRS | LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR.
    opener = getattr(ctypes, '_dlopen', None)
    if opener is None:
        opener = getattr(_ctypes, 'LoadLibrary' if sys.platform == 'win32' else 'dlopen')
    handle = opener(path, mode)
    try:
        factory = cast(Any, ctypes.cdll)._dlltype
        return cast(_CDLL, (_Library if factory is _CDLL else factory)(path, handle=handle))
    except BaseException:
        _close_library(handle)
        raise


def _close_library(handle: int) -> None:
    if sys.platform == 'win32':
        _ctypes.FreeLibrary(handle)
    else:
        _ctypes.dlclose(handle)


def _shared_library(path: str) -> _CDLL:
    """Discard surplus opens from concurrent/reentrant misses for the fixed native path."""
    library = _open_library(path)
    shared = _open_library(path)
    if library is not shared and _AsDouble is not None:
        _close_library(library._handle)
    return shared


def _load_library(path: str) -> _CDLL:
    """Defer Python SIGINT delivery until the opened image has a cached owner.

    A try/finally cannot protect a C call's return before Python assigns its
    handle, or a return before lru_cache publishes its value. Keep the original
    policy and originating frames, delivering signals after either publication
    or rollback. Python dispatches signal handlers only on the main thread.
    """
    handler = signal.getsignal(signal.SIGINT)
    if current_thread() is not main_thread() or not callable(handler):
        return _shared_library(path)
    native = _native_sigaction()
    # Resolving the native symbol can run a caller's audit hook.
    handler = _get_signal(signal.SIGINT)
    if not callable(handler):
        return _shared_library(path)
    pending: List[Tuple[int, Optional[FrameType]]] = []
    deferring = True

    def defer(signum: int, frame: Optional[FrameType]) -> None:
        if deferring:
            pending.append((signum, frame))
        elif callable(handler):
            handler(signum, frame)

    try:
        try:
            previous = _set_signal(signal.SIGINT, defer, native)
            if previous is not None:
                handler = previous
        except ValueError as error:
            if (not _direct_error(error, _set_signal) or _get_signal(signal.SIGINT) is defer or error.args not in (
                ('signal only works in main thread of the main interpreter',),
                ('signal only works in main thread or with __pypy__.thread.enable_signals()',),
            )):
                raise
        return _shared_library(path)
    finally:
        try:
            _restore_sigint(defer, handler, native, _set_signal)
            callback: Optional[_SignalHandler] = handler
            while pending:
                arguments = pending.pop(0)
                try:
                    if callable(callback):
                        callback(*arguments)
                    elif callback != signal.SIG_IGN:
                        signal.raise_signal(signal.SIGINT)
                    callback = _get_signal(signal.SIGINT)
                finally:
                    del arguments
        finally:
            deferring = False
            pending.clear()  # Frames can refer back to this queue, even with cyclic GC disabled.


class _Functions:
    """Own independent address-bound functions without per-call ctypes cycles."""

    def __init__(self, library: Any) -> None:
        self._library = library

    def __getattr__(self, name: str) -> Any:
        function = getattr(self._library, name)
        if isinstance(function, CFuncPtr) and (
            (function._objects and function._objects.get('0') is self._library)
            or getattr(function, 'dll', None) is self._library
        ):
            function = type(function)(cast(int, ctypes.cast(function, ctypes.c_void_p).value))
            cast(Any, function).__name__ = name
            cast(Any, function)._library = self._library
        setattr(self, name, function)
        return function


class Config(ctypes.Structure):
    _fields_ = [  # noqa: RUF012 — ctypes requires this mutable class attribute.
        ('abi_version', ctypes.c_uint32),
        ('struct_size', ctypes.c_uint32),
        ('benchmark', ctypes.c_uint32),
        ('reserved', ctypes.c_uint32),
        ('warmup_seconds', ctypes.c_double),
        ('measurement_seconds', ctypes.c_double),
    ]


class CProgress(ctypes.Structure):
    _fields_ = [  # noqa: RUF012 — ctypes requires this mutable class attribute.
        ('phase', ctypes.c_uint32),
        ('workers', ctypes.c_uint32),
        ('fraction', ctypes.c_double),
        ('phase_elapsed_seconds', ctypes.c_double),
        ('phase_duration_seconds', ctypes.c_double),
        ('iterations', ctypes.c_uint64),
    ]


class CResult(ctypes.Structure):
    _fields_ = [  # noqa: RUF012 — ctypes requires this mutable class attribute.
        ('abi_version', ctypes.c_uint32),
        ('status', ctypes.c_uint32),
        ('benchmark', ctypes.c_uint32),
        ('workers', ctypes.c_uint32),
        ('score', ctypes.c_double),
        ('warmup_seconds', ctypes.c_double),
        ('measurement_seconds', ctypes.c_double),
        ('elapsed_seconds', ctypes.c_double),
        ('variation_percent', ctypes.c_double),
        ('iterations', ctypes.c_uint64),
        ('checksum', ctypes.c_uint64),
        ('sample_count', ctypes.c_uint64),
        ('unstable', ctypes.c_uint32),
        ('has_variation', ctypes.c_uint32),
    ]


class CSample(ctypes.Structure):
    _fields_ = [  # noqa: RUF012 — ctypes requires this mutable class attribute.
        ('elapsed_seconds', ctypes.c_double),
        ('duration_seconds', ctypes.c_double),
        ('iterations', ctypes.c_uint64),
        ('iterations_per_second', ctypes.c_double),
    ]


class CResultV2(ctypes.Structure):
    _fields_ = [  # noqa: RUF012 — ctypes layout, legacy prefix preserved.
        *CResult._fields_,
        ('pi_equivalent', ctypes.c_double),
        ('has_pi_equivalent', ctypes.c_uint32),
    ]


class CReferenceMachine(ctypes.Structure):
    _fields_ = [  # noqa: RUF012
        ('name', ctypes.c_char_p),
        ('short_name', ctypes.c_char_p),
        ('os_name', ctypes.c_char_p),
        ('os_version', ctypes.c_char_p),
        ('cpu_architecture', ctypes.c_char_p),
        ('memory_bytes', ctypes.c_uint64),
        ('cpu_cores', ctypes.c_uint32),
    ]


class CReferenceRun(ctypes.Structure):
    _fields_ = [  # noqa: RUF012
        ('started_at_unix_seconds', ctypes.c_double),
        ('start_temperature_c', ctypes.c_double),
        ('end_temperature_c', ctypes.c_double),
        ('warmup_seconds', ctypes.c_double),
        ('result_count', ctypes.c_uint64),
    ]


class CReferenceBenchmarkResult(ctypes.Structure):
    _fields_ = [  # noqa: RUF012
        ('benchmark', ctypes.c_char_p),
        ('score', ctypes.c_double),
    ]


Callback = ctypes.CFUNCTYPE(None, ctypes.POINTER(CProgress), ctypes.c_void_p)


class CSuiteProgress(ctypes.Structure):
    _fields_ = [('benchmark', ctypes.c_uint32), ('progress', CProgress)]  # noqa: RUF012


class CAggregatedScores(ctypes.Structure):
    _fields_ = [('pi_equivalent', ctypes.c_double), ('has_pi_equivalent', ctypes.c_uint32)]  # noqa: RUF012


class CSuiteResult(ctypes.Structure):
    _fields_ = [  # noqa: RUF012
        ('results', ctypes.c_void_p), ('result_count', ctypes.c_uint64), ('aggregated_scores', CAggregatedScores),
    ]


SuiteCallback = ctypes.CFUNCTYPE(None, ctypes.POINTER(CSuiteProgress), ctypes.c_void_p)
ResultCallback = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p)
ConfigCallback = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(Config), ctypes.c_void_p)
NameMatcher = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p)
_DoubleCopy = ctypes.PYFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.py_object, ctypes.c_void_p)
_api = getattr(ctypes, 'pythonapi', None)
_AsDouble = ctypes.cast(_api.PyFloat_AsDouble, ctypes.c_void_p) if _api is not None else None


class _HostComparisonError(BaseException):
    def __init__(self, error: BaseException) -> None:
        self.error = error


class _SuiteConfigurationError(BaseException):
    """Carry a caller conversion exception past backend failure handling."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error


class Native:
    def __init__(self) -> None:
        filename = (
            'benchset.dll'
            if sys.platform == 'win32'
            else ('libbenchset.dylib' if sys.platform == 'darwin' else 'libbenchset.so')
        )
        path = Path(__file__).parent / '_native' / filename
        # Share the loaded image, retaining fresh signatures and injected-loader behavior.
        factory = ctypes.CDLL
        loader = _load_library if factory is cast(Any, ctypes.cdll)._dlltype else factory
        self.lib = _Functions(loader(str(path)))
        # Check the fixed ABI/version entry points before resolving V2 symbols.
        self.lib.benchset_abi_version.argtypes = []
        self.lib.benchset_abi_version.restype = ctypes.c_uint32
        self.lib.benchset_version.argtypes = []
        self.lib.benchset_version.restype = ctypes.c_char_p
        version = self.lib.benchset_version().decode('utf-8')
        if self.lib.benchset_abi_version() != ABI_VERSION or version != __version__:
            raise RuntimeError(
                'native library version/ABI does not match the Python package',
            )
        signatures = {
            'benchset_create': ([], ctypes.c_void_p),
            'benchset_run': (
                [ctypes.c_void_p, ctypes.POINTER(Config), Callback, ctypes.c_void_p],
                ctypes.c_uint32,
            ),
            'benchset_cancel': ([ctypes.c_void_p], None),
            'benchset_result': (
                [ctypes.c_void_p, ctypes.POINTER(CResult)],
                ctypes.c_int32,
            ),
            'benchset_result_v2': (
                [ctypes.c_void_p, ctypes.POINTER(CResultV2), ctypes.c_uint32],
                ctypes.c_int32,
            ),
            'benchset_reference_machine': (
                [ctypes.POINTER(CReferenceMachine), ctypes.c_uint32], ctypes.c_int32,
            ),
            'benchset_reference_run': (
                [ctypes.POINTER(CReferenceRun), ctypes.c_uint32], ctypes.c_int32,
            ),
            'benchset_reference_result': (
                [ctypes.c_uint64, ctypes.POINTER(CReferenceBenchmarkResult), ctypes.c_uint32], ctypes.c_int32,
            ),
            'benchset_sample': (
                [ctypes.c_void_p, ctypes.c_uint64, ctypes.POINTER(CSample)],
                ctypes.c_int32,
            ),
            'benchset_reason': ([ctypes.c_void_p], ctypes.c_char_p),
            'benchset_free': ([ctypes.c_void_p], None),
            'benchset_benchmark_id': ([ctypes.c_char_p, ctypes.c_uint64], ctypes.c_uint32),
            'benchset_benchmark_match': ([NameMatcher, ctypes.c_void_p, ctypes.c_uint32], ctypes.c_uint32),
            'benchset_duration_error': ([ctypes.c_double, ctypes.c_double], ctypes.c_uint32),
            'benchset_validation_error': ([ctypes.c_uint32, ctypes.c_double, ctypes.c_double], ctypes.c_uint32),
            'benchset_validation_message': ([ctypes.c_uint32, ctypes.c_uint32], ctypes.c_char_p),
            'benchset_aggregate': ([ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p], ctypes.c_int32),
            'benchset_aggregate_read': ([ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p,
                                        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int32),
            'benchset_suite_create': ([], ctypes.c_void_p),
            'benchset_suite_cancel': ([ctypes.c_void_p], None),
            'benchset_suite_free': ([ctypes.c_void_p], None),
            'benchset_run_all_observed': (
                [ctypes.c_void_p, ctypes.POINTER(Config), SuiteCallback, ResultCallback, ctypes.c_void_p],
                ctypes.c_uint32,
            ),
            'benchset_run_all_configured': (
                [ctypes.c_void_p, ctypes.POINTER(Config), SuiteCallback, ResultCallback, ConfigCallback, ctypes.c_void_p],
                ctypes.c_uint32,
            ),
            'benchset_suite_result': ([ctypes.c_void_p, ctypes.POINTER(CSuiteResult), ctypes.c_uint32], ctypes.c_int32),
            'benchset_copy_double': ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int32),
        }
        for name, (arguments, result) in signatures.items():
            try:
                function = getattr(self.lib, name)
            except AttributeError as error:
                raise RuntimeError('native library is missing required symbol: ' + name) from error
            function.argtypes = arguments
            function.restype = result

    def double(self, value: Any, output: Any = None) -> ctypes.c_double:
        """Copy with the interpreter's numeric primitive without signaling NaNs.

        PYFUNCTYPE keeps the Python thread state/GIL and propagates conversion
        errors. The local function and self retain the library during the call.
        """
        if _AsDouble is None:  # Preserve math's conversions when ctypes reads subclass storage directly.
            if issubclass(type(value), (float, int)):
                value = float(value)
            number = ctypes.c_double(value)
            if output is not None:
                ctypes.memmove(output, ctypes.byref(number), ctypes.sizeof(number))
            return number
        number = ctypes.c_double()
        function = self.lib.benchset_copy_double
        copy = _DoubleCopy(cast(int, ctypes.c_void_p.from_buffer(function).value))
        if not copy(_AsDouble, value, ctypes.byref(number) if output is None else output):
            raise RuntimeError('native numeric conversion is unavailable')
        return number

    def config(self, benchmark: int, warmup: float, measurement: float) -> Config:
        config = Config(ABI_VERSION, ctypes.sizeof(Config), benchmark)
        self.double(warmup, ctypes.byref(config, Config.warmup_seconds.offset))
        self.double(measurement, ctypes.byref(config, Config.measurement_seconds.offset))
        return config

    def benchmark_id(self, name: str, *, execution: bool = False, wrap_host: bool = False) -> int:
        if type(name) is str:
            encoded = str.encode(name, 'utf-8', errors='surrogatepass')
            return int(self.lib.benchset_benchmark_id(encoded, len(encoded)))
        errors: List[BaseException] = []

        def matches(pointer: int, size: int, _user: object) -> int:
            try:
                expected = canonical_name(ctypes.string_at(pointer, size))
                return int(bool(name == expected if execution else expected == name))
            except BaseException as error:  # noqa: BLE001 — Host operators cannot unwind through C.
                errors.append(error)
                return -1

        try:
            value = self.lib.benchset_benchmark_match(NameMatcher(matches), None, execution)
            if errors:
                if wrap_host:
                    raise _HostComparisonError(errors[0])
                _raise_original(errors[0])
            return int(value)
        finally:
            errors.clear()

    def validation_error(
        self, benchmark: str, warmup: float, measurement: float, *, wrap_host: bool = False,
    ) -> Optional[str]:
        code = self.lib.benchset_validation_error(self.benchmark_id(benchmark, wrap_host=wrap_host),
                                                self.double(warmup), self.double(measurement))
        if not code:
            return None
        reason = str(self.lib.benchset_validation_message(code, 0).decode('utf-8'))
        if code != 1:
            return reason
        try:
            reason = reason + ': ' + benchmark
            if not issubclass(type(reason), str):
                raise TypeError('benchmark diagnostic must be a string')
            return reason
        except BaseException as error:
            if wrap_host:
                raise _HostComparisonError(error) from None
            raise

    def duration_error(self, warmup: float, measurement: float) -> Optional[str]:
        code = self.lib.benchset_duration_error(self.double(warmup), self.double(measurement))
        return str(self.lib.benchset_validation_message(code, 1).decode('utf-8')) if code else None

    def create(self, suite: bool = False) -> int:
        handle = self.lib.benchset_suite_create() if suite else self.lib.benchset_create()
        if not handle:
            raise RuntimeError('native library could not allocate a benchmark handle')
        return int(handle)

    def cancel(self, handle: int, suite: bool = False) -> None:
        if suite:
            self.lib.benchset_suite_cancel(handle)
        else:
            self.lib.benchset_cancel(handle)

    def free(self, handle: int, suite: bool = False) -> None:
        if suite:
            self.lib.benchset_suite_free(handle)
        else:
            self.lib.benchset_free(handle)

    @staticmethod
    def progress(benchmark: str, value: CProgress) -> Progress:
        return Progress(
            benchmark, {1: 'warmup', 2: 'measurement', 3: 'completed', 4: 'cancelled', 5: 'failed'}[value.phase],
            value.workers, value.fraction, value.phase_elapsed_seconds, value.phase_duration_seconds, value.iterations,
        )

    def result(self, handle: int, benchmark: str, errors: List[BaseException]) -> BenchmarkResult:
        value = CResultV2()
        if not self.lib.benchset_result_v2(handle, ctypes.byref(value), ctypes.sizeof(value)):
            raise RuntimeError('native library did not return a result')
        samples = []
        for index in range(value.sample_count):
            sample = CSample()
            if not self.lib.benchset_sample(handle, index, ctypes.byref(sample)):
                raise RuntimeError('native sample is unavailable')
            samples.append(Sample(sample.elapsed_seconds, sample.duration_seconds,
                                  sample.iterations, sample.iterations_per_second))
        reason = self.lib.benchset_reason(handle)
        status = {0: 'completed', 1: 'cancelled', 2: 'failed'}[value.status]
        message = reason.decode('utf-8') if reason else None
        if errors:
            status = 'failed'
            try:
                message = f'progress callback failed: {type(errors[0]).__name__}: {errors[0]}'
            except BaseException:  # noqa: BLE001 — User exception formatters can fail too.
                message = f'progress callback failed: {type(errors[0]).__name__}'
        return BenchmarkResult(
            benchmark=benchmark, status=status, reason=message,
            score=value.score if status == 'completed' else None, workers=value.workers,
            warmup_seconds=value.warmup_seconds, measurement_seconds=value.measurement_seconds,
            elapsed_seconds=value.elapsed_seconds, iterations=value.iterations, checksum=value.checksum,
            samples=tuple(samples), variation_percent=value.variation_percent if value.has_variation else None,
            unstable=bool(value.unstable),
            pi_equivalent=value.pi_equivalent if status == 'completed' and value.has_pi_equivalent else None,
        )

    def execute(
        self, handle: int, benchmark: str, warmup_seconds: float, measurement_seconds: float,
        on_progress: Optional[Callable[[Progress], None]],
    ) -> BenchmarkResult:
        config = self.config(self.benchmark_id(benchmark, execution=True), warmup_seconds, measurement_seconds)
        errors: List[BaseException] = []

        def callback(pointer: ctypes.POINTER(CProgress), _user: object) -> None:  # type: ignore[valid-type]
            if on_progress is not None and not errors:
                try:
                    on_progress(self.progress(benchmark, pointer.contents))
                except BaseException as error:  # noqa: BLE001 — Never unwind through a C callback.
                    errors.append(error)
                    self.cancel(handle)

        c_callback = Callback(callback)
        try:
            self.lib.benchset_run(handle, ctypes.byref(config), c_callback, None)
            return self.result(handle, benchmark, errors)
        finally:
            # User tracebacks retain the callback frame and its captured data.
            errors.clear()

    def execute_suite(  # noqa: PLR0913, PLR0915 — Callbacks share one native lifetime and error boundary.
        self, handle: int, warmup_seconds: float, measurement_seconds: float,
        on_progress: Optional[Callable[[Progress], None]],
        on_result: Optional[Callable[[BenchmarkResult], bool]] = None,
        convert_windows: Optional[Callable[[], Tuple[float, float]]] = None,
    ) -> SuiteResult:
        config = self.config(0, warmup_seconds, measurement_seconds)
        names = {item['id']: item['name'] for item in DATA['benchmarks']}
        results: List[BenchmarkResult] = []
        errors: List[BaseException] = []
        failures: List[BaseException] = []
        configuration_errors: List[BaseException] = []
        unavailable = False
        configured = False
        current_warmup, current_measurement = warmup_seconds, measurement_seconds

        def configure(_benchmark: int, pointer: ctypes.POINTER(Config), _user: object) -> int:  # type: ignore[valid-type]
            nonlocal configured, current_warmup, current_measurement
            try:
                # The public runner already converted the first pair before loading.
                if configured and convert_windows is not None:
                    current_warmup, current_measurement = convert_windows()
                self.double(current_warmup, ctypes.byref(pointer.contents, Config.warmup_seconds.offset))
                self.double(current_measurement, ctypes.byref(pointer.contents, Config.measurement_seconds.offset))
                configured = True
                return 1
            except BaseException as error:  # noqa: BLE001 — Never unwind caller conversions through C.
                configuration_errors.append(error)
                self.cancel(handle, True)
                return 0

        def progress(pointer: ctypes.POINTER(CSuiteProgress), _user: object) -> None:  # type: ignore[valid-type]
            if on_progress is not None and not errors and not failures:
                try:
                    value = pointer.contents
                    on_progress(self.progress(names[value.benchmark], value.progress))
                except BaseException as error:  # noqa: BLE001 — Python exceptions cannot cross C.
                    errors.append(error)
                    self.cancel(handle, True)

        def completed(benchmark: int, child: int, _user: object) -> int:
            nonlocal unavailable
            try:
                try:
                    reason = self.validation_error(names[benchmark], current_warmup, current_measurement)
                    if reason is not None and not errors:
                        result = BenchmarkResult(names[benchmark], reason=reason, warmup_seconds=current_warmup,
                                                 measurement_seconds=current_measurement)
                    else:
                        result = self.result(child, names[benchmark], errors)
                except (OSError, RuntimeError, OverflowError) as error:
                    unavailable = True
                    result = BenchmarkResult(names[benchmark], reason=str(error), warmup_seconds=current_warmup,
                                             measurement_seconds=current_measurement)
                results.append(result)
                stop = on_result(result) if on_result is not None else False
                return int(stop or bool(errors) or unavailable)
            except BaseException as error:  # noqa: BLE001 — Rethrow only after native cleanup.
                failures.append(error)
                self.cancel(handle, True)
                return 1

        c_progress, c_result, c_config = SuiteCallback(progress), ResultCallback(completed), ConfigCallback(configure)
        try:
            self.lib.benchset_run_all_configured(handle, ctypes.byref(config), c_progress, c_result, c_config, None)
            if configuration_errors:
                raise _SuiteConfigurationError(configuration_errors[0])
            if failures:
                raise failures[0]
            value = CSuiteResult()
            if not self.lib.benchset_suite_result(handle, ctypes.byref(value), ctypes.sizeof(value)):
                raise RuntimeError('native library did not return a suite result')
            if value.result_count != len(results):
                raise RuntimeError('native suite results are unavailable')
            scores = value.aggregated_scores
            equivalent = scores.pi_equivalent if scores.has_pi_equivalent and not errors and not unavailable else None
            return SuiteResult(tuple(results), AggregatedScores(equivalent))
        finally:
            errors.clear()
            failures.clear()
            configuration_errors.clear()
