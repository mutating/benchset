"""Marshal caller-built result snapshots to the Rust aggregate calculator."""

import ctypes
import math
from typing import List, Optional, Tuple, cast

from benchset._metadata import canonical_name
from benchset.models import BenchmarkResult


class _Input(ctypes.Structure):
    _fields_ = [  # noqa: RUF012 — ctypes structure declaration.
        ('benchmark', ctypes.c_uint32), ('status', ctypes.c_uint32),
        ('pi_equivalent', ctypes.c_double), ('has_pi_equivalent', ctypes.c_uint32),
        ('name', ctypes.c_void_p), ('name_size', ctypes.c_uint64),
    ]


class _Scores(ctypes.Structure):
    _fields_ = [('pi_equivalent', ctypes.c_double), ('has_pi_equivalent', ctypes.c_uint32)]  # noqa: RUF012


_Reader = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_uint64, ctypes.c_uint32,
                          ctypes.POINTER(_Input), ctypes.c_void_p)
_Names = ctypes.c_uint32 * 1


def aggregate(results: Tuple[BenchmarkResult, ...]) -> Optional[float]:
    if isinstance(results, tuple):
        # Snapshot actual tuple storage without invoking subclass protocols.
        results = tuple.__getitem__(results, slice(None))
    benchmarks = tuple(result.benchmark for result in results)
    # Keep model imports independent of native-library availability.
    from benchset._native import Native, _raise_original  # noqa: PLC0415

    native = Native()
    # Host mode reads names through the callback; only a non-null pointer is needed.
    names = _Names()
    errors: List[BaseException] = []

    def read(index: int, field: int, output: ctypes.POINTER(_Input), _user: object) -> int:  # type: ignore[valid-type]
        try:
            result = results[index]
            if field == 0:
                output.contents.status = 0 if result.success else 2
            elif field == 1:
                output.contents.has_pi_equivalent = result.pi_equivalent is not None
            elif field == 2:
                output.contents.has_pi_equivalent = bool(cast(float, result.pi_equivalent) >= output.contents.status)
            elif field == 3:
                output.contents.has_pi_equivalent = bool(result.pi_equivalent == output.contents.status)
            elif field == 4:
                # Rust requests this primitive only to restore Python's log-domain exception.
                output.contents.pi_equivalent = math.log(output.contents.pi_equivalent)
            elif field == 6:
                native.double(result.pi_equivalent, ctypes.byref(output.contents, _Input.pi_equivalent.offset))
            elif field == 7:
                # Preserve Python's exception only when Rust detected finite exp overflow.
                output.contents.pi_equivalent = math.exp(output.contents.pi_equivalent)
            else:
                expected = canonical_name(ctypes.string_at(output.contents.name, output.contents.name_size))
                output.contents.has_pi_equivalent = bool(benchmarks[index] == expected)
            return 1
        except BaseException as error:  # noqa: BLE001 — Host exceptions cannot unwind through C.
            errors.append(error)
            return 0

    function = native.lib.benchset_aggregate_read
    function.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint64, _Reader,
                         ctypes.c_void_p, ctypes.POINTER(_Scores), ctypes.c_uint32]
    function.restype = ctypes.c_int32
    scores = _Scores()
    try:
        available = function(names, len(results), _Reader(read), None, ctypes.byref(scores), 1)
        if errors:
            _raise_original(errors[0])
        if not available:
            raise RuntimeError('native aggregate is unavailable')
        return scores.pi_equivalent if scores.has_pi_equivalent else None
    finally:
        errors.clear()
