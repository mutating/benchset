"""Private ctypes boundary. All measurement logic lives in Rust."""

import ctypes
import sys
from pathlib import Path
from typing import Callable, List, Optional

from benchset.models import BenchmarkResult, Progress, Sample, __version__

ABI_VERSION = 1


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


class Native:
    def __init__(self) -> None:
        filename = (
            'benchset.dll'
            if sys.platform == 'win32'
            else ('libbenchset.dylib' if sys.platform == 'darwin' else 'libbenchset.so')
        )
        path = Path(__file__).parent / '_native' / filename
        self.lib = ctypes.CDLL(str(path))
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
        }
        for name, (arguments, result) in signatures.items():
            try:
                function = getattr(self.lib, name)
            except AttributeError as error:
                raise RuntimeError('native library is missing required symbol: ' + name) from error
            function.argtypes = arguments
            function.restype = result

    def create(self) -> int:
        handle = self.lib.benchset_create()
        if not handle:
            raise RuntimeError('native library could not allocate a benchmark handle')
        return int(handle)

    def cancel(self, handle: int) -> None:
        self.lib.benchset_cancel(handle)

    def free(self, handle: int) -> None:
        self.lib.benchset_free(handle)

    def execute(
        self,
        handle: int,
        benchmark: str,
        warmup_seconds: float,
        measurement_seconds: float,
        on_progress: Optional[Callable[[Progress], None]],
    ) -> BenchmarkResult:
        config = Config(
            ABI_VERSION,
            ctypes.sizeof(Config),
            1 if benchmark == 'single_core' else 2,
            0,
            warmup_seconds,
            measurement_seconds,
        )
        callback_errors: List[BaseException] = []

        def callback(pointer: ctypes.POINTER(CProgress), _user: object) -> None:  # type: ignore[valid-type]
            if on_progress is None or callback_errors:
                return
            value = pointer.contents
            progress = Progress(
                benchmark,
                {
                    1: 'warmup',
                    2: 'measurement',
                    3: 'completed',
                    4: 'cancelled',
                    5: 'failed',
                }[value.phase],
                value.workers,
                value.fraction,
                value.phase_elapsed_seconds,
                value.phase_duration_seconds,
                value.iterations,
            )
            try:
                on_progress(progress)
            except BaseException as error:  # noqa: BLE001 — Never unwind a Python exception through a C callback.
                callback_errors.append(error)
                self.cancel(handle)

        c_callback = Callback(callback)
        try:
            self.lib.benchset_run(handle, ctypes.byref(config), c_callback, None)
            value = CResultV2()
            if not self.lib.benchset_result_v2(handle, ctypes.byref(value), ctypes.sizeof(value)):
                raise RuntimeError('native library did not return a result')
            samples = []
            for index in range(value.sample_count):
                sample = CSample()
                if not self.lib.benchset_sample(handle, index, ctypes.byref(sample)):
                    raise RuntimeError('native sample is unavailable')
                samples.append(
                    Sample(
                        sample.elapsed_seconds,
                        sample.duration_seconds,
                        sample.iterations,
                        sample.iterations_per_second,
                    ),
                )
            reason = self.lib.benchset_reason(handle)
            status = {0: 'completed', 1: 'cancelled', 2: 'failed'}[value.status]
            message = reason.decode('utf-8') if reason else None
            if callback_errors:
                status = 'failed'
                message = f'progress callback failed: {type(callback_errors[0]).__name__}: {callback_errors[0]}'
            return BenchmarkResult(
                benchmark=benchmark,
                status=status,
                reason=message,
                score=value.score if status == 'completed' else None,
                workers=value.workers,
                warmup_seconds=value.warmup_seconds,
                measurement_seconds=value.measurement_seconds,
                elapsed_seconds=value.elapsed_seconds,
                iterations=value.iterations,
                checksum=value.checksum,
                samples=tuple(samples),
                variation_percent=value.variation_percent if value.has_variation else None,
                unstable=bool(value.unstable),
                pi_equivalent=(
                    value.pi_equivalent
                    if status == 'completed' and value.has_pi_equivalent else None
                ),
            )
        finally:
            # Tracebacks retain the callback frame and its captured user data.
            # Break that cycle even if running or decoding the result fails.
            callback_errors.clear()
