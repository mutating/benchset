"""Bound native-loader memory while keeping each Python binding independently configured."""

import ctypes
import gc
import weakref
from typing import Any, Callable, List, Optional, Tuple

import pytest

from benchset import BenchmarkResult, SuiteResult, __version__, _native, api, cli
from benchset._native import ABI_VERSION, Native
from tests.test_resources import (
    without_cyclic_gc,  # noqa: F401 — Reuse the GC-disabled fixture.
)

_Reference = Callable[[], Optional[object]]


def _live_count(references: List[_Reference]) -> int:
    return len({id(value) for reference in references if (value := reference()) is not None})


@pytest.mark.usefixtures('without_cyclic_gc')
@pytest.mark.parametrize('operation', ['aggregate', 'cli', 'invalid_run'])
def test_new_native_consumers_keep_loader_objects_bounded_without_gc(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], operation: str,
) -> None:
    """Repeated scoring and validation must not add a CDLL/function-type cycle per call.

    Observe actual function pointers and their dynamic ctypes classes while
    leaving the standard CDLL factory intact. A single process-lived library
    is permitted; per-operation growth is not. Weak references distinguish
    retained objects from allocator high-water marks and tracing overhead.
    """
    pointers: List[_Reference] = []
    pointer_types: List[_Reference] = []

    class ObservedNative(Native):
        def __init__(self) -> None:
            super().__init__()
            function = self.lib.benchset_create
            pointers.append(weakref.ref(function))
            pointer_types.append(weakref.ref(type(function)))

    for module in (_native, api, cli):
        monkeypatch.setattr(module, 'Native', ObservedNative, raising=False)
    values = (BenchmarkResult('single_core', status='completed', pi_equivalent=1),
              BenchmarkResult('multi_core', status='completed', pi_equivalent=4))

    def invoke() -> None:
        if operation == 'aggregate':
            assert SuiteResult.from_results(values).aggregated_scores.pi_equivalent == 2
        elif operation == 'cli':
            with pytest.raises(SystemExit) as caught:
                cli.settings(['--warmup=-1', '--duration=.1'])
            assert caught.value.code == 2
            capsys.readouterr()
        else:
            result = api.run('unknown', warmup_seconds=0, measurement_seconds=.1)
            assert result.status == 'failed'
            assert result.reason == 'unknown benchmark: unknown'

    ObservedNative()
    invoke()
    # Exclude import, FFI-type and parser warmup before starting the no-GC batches.
    gc.collect()
    pointers.clear()
    pointer_types.clear()
    for _batch in range(3):
        for _attempt in range(20):
            invoke()
        assert _live_count(pointers) <= 1, 'native function-pointer ownership grows with each call'
        assert _live_count(pointer_types) <= 1, 'CDLL construction leaves a new cyclic function type per call'


def test_native_function_signatures_are_independent_between_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shared raw library must not share mutable argtypes/restype or function monkeypatches."""
    first, second = Native(), Native()
    original = second.lib.benchset_abi_version
    assert first.lib.benchset_abi_version is not original
    expected_arguments, expected_result = list(original.argtypes), original.restype
    first.lib.benchset_abi_version.argtypes = [ctypes.c_int]
    first.lib.benchset_abi_version.restype = None
    assert original.argtypes == expected_arguments
    assert original.restype is expected_result
    assert original() == ABI_VERSION
    monkeypatch.setattr(first.lib, 'benchset_abi_version', lambda: 999)
    assert second.lib.benchset_abi_version() == ABI_VERSION
    assert Native().lib.benchset_abi_version() == ABI_VERSION


@pytest.mark.usefixtures('without_cyclic_gc')
@pytest.mark.parametrize('operation', ['aggregate', 'invalid_run'])
def test_quiet_numeric_copies_release_their_function_pointers_without_gc(
    monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    """Public calls must not leave a quiet-copy function cycle for each Native.

    Casting a ctypes function pointer to void* mutates its ownership dictionary
    into a self-cycle. Weakly observe the actual per-call helper and its type,
    with collection disabled, to distinguish this leak from a loaded library's
    permitted process lifetime and to keep repeated scoring/validation bounded.
    """
    pointers: List[_Reference] = []
    pointer_types: List[_Reference] = []

    class ObservedNative(Native):
        def __init__(self) -> None:
            super().__init__()
            function = self.lib.benchset_copy_double
            pointers.append(weakref.ref(function))
            pointer_types.append(weakref.ref(type(function)))

    monkeypatch.setattr(_native, 'Native', ObservedNative)
    monkeypatch.setattr(api, 'Native', ObservedNative)
    values = (BenchmarkResult('single_core', status='completed', pi_equivalent=1),
              BenchmarkResult('multi_core', status='completed', pi_equivalent=4))

    def invoke() -> None:
        if operation == 'aggregate':
            assert SuiteResult.from_results(values).aggregated_scores.pi_equivalent == 2
        else:
            result = api.run('unknown', warmup_seconds=0, measurement_seconds=.1)
            assert result.status == 'failed'
            assert result.reason == 'unknown benchmark: unknown'

    def counts() -> Tuple[int, int]:
        objects = gc.get_objects()
        return (sum(issubclass(type(value), _native.CFuncPtr) for value in objects),
                sum(issubclass(type(value), type) and issubclass(value, _native.CFuncPtr) for value in objects))

    invoke()
    gc.collect()
    pointers.clear()
    pointer_types.clear()
    baseline = counts()
    for _batch in range(3):
        for _attempt in range(16):
            invoke()
        assert _live_count(pointers) <= 1, 'quiet numeric copying retains one cyclic function pointer per call'
        assert _live_count(pointer_types) <= 1, 'quiet numeric copying retains per-call ctypes types'
        actual = counts()
        assert actual[0] <= baseline[0] + 1, 'quiet numeric copying retains temporary ctypes callables'
        assert actual[1] <= baseline[1] + 1, 'quiet numeric copying creates unbounded ctypes callable types'


@pytest.mark.parametrize('failure', ['abi', 'version', 'symbol'])
def test_native_revalidates_cached_library_and_preserves_error_priority(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    """Every binding rechecks ABI, package version and symbols, even after a successful warm load.

    Patch the standard loader's symbol resolver rather than replacing its
    factory: this also exercises a cached raw library. ABI/version rejection
    must precede required-symbol resolution and failed validation must recover.
    """
    state = {'abi': ABI_VERSION, 'version': __version__.encode('utf-8'), 'missing': False}
    lookups: List[str] = []
    resolve = ctypes.CDLL.__getattribute__
    fallback = ctypes.CDLL.__getattr__

    def lookup(library: ctypes.CDLL, name: str) -> Any:
        if name.startswith('benchset_'):
            lookups.append(name)
        if name == 'benchset_abi_version':
            return lambda: state['abi']
        if name == 'benchset_version':
            return lambda: state['version']
        if name == 'benchset_sample' and state['missing']:
            raise AttributeError(name)
        return resolve(library, name)

    def missing(library: ctypes.CDLL, name: str) -> Any:
        # AttributeError from __getattribute__ must not resolve the hidden
        # symbol through CDLL's normal __getattr__ fallback.
        if name == 'benchset_sample' and state['missing']:
            raise AttributeError(name)
        return fallback(library, name)

    monkeypatch.setattr(ctypes.CDLL, '__getattribute__', lookup)
    monkeypatch.setattr(ctypes.CDLL, '__getattr__', missing)
    Native()
    lookups.clear()
    state['missing'] = True
    if failure == 'abi':
        state['abi'] = ABI_VERSION + 1
    elif failure == 'version':
        state['version'] = b'not-the-package-version'
    message = 'missing required symbol: benchset_sample' if failure == 'symbol' else 'version/ABI'
    with pytest.raises(RuntimeError, match=message):
        Native()
    if failure != 'symbol':
        assert set(lookups) <= {'benchset_abi_version', 'benchset_version'}
    state.update(abi=ABI_VERSION, version=__version__.encode('utf-8'), missing=False)
    assert Native().lib.benchset_abi_version() == ABI_VERSION


def test_custom_loader_is_called_again_after_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caching the standard library cannot cache an injected loader's objects or suppress its errors."""
    library = Native().lib
    calls: List[str] = []
    original = OSError('fresh injected loader failure')

    def load(path: str) -> Any:
        calls.append(path)
        if len(calls) in (2, 3):
            raise original
        return library

    monkeypatch.setattr(ctypes, 'CDLL', load)
    assert Native().lib.benchset_abi_version() == ABI_VERSION
    for _attempt in range(2):
        with pytest.raises(OSError, match='fresh injected loader failure') as caught:
            Native()
        assert caught.value is original
    assert Native().lib.benchset_abi_version() == ABI_VERSION
    assert len(calls) == 4
    assert len(set(calls)) == 1


@pytest.mark.usefixtures('without_cyclic_gc')
def test_custom_loader_closure_is_not_retained_after_restoration(monkeypatch: pytest.MonkeyPatch) -> None:
    """An injected factory and its captured data must not become process-lived cache keys."""
    library = Native().lib

    class Loader:
        def __init__(self) -> None:
            self.payload = bytearray(64 * 1024)

        def __call__(self, _path: str) -> Any:
            return library

    loader = Loader()
    observed = weakref.ref(loader)
    with monkeypatch.context() as patch:
        patch.setattr(ctypes, 'CDLL', loader)
        assert Native().lib.benchset_abi_version() == ABI_VERSION
    del loader
    assert observed() is None


def test_duck_library_is_not_probed_before_fixed_abi_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Function-table construction must not invoke arbitrary duck-library lookups before rejecting its ABI."""
    lookups: List[str] = []

    class Library:
        def __getattr__(self, name: str) -> Any:
            lookups.append(name)
            if name == 'benchset_abi_version':
                return lambda: ABI_VERSION + 1
            if name == 'benchset_version':
                return lambda: __version__.encode('utf-8')
            raise AssertionError('unexpected lookup before ABI rejection: ' + name)

    monkeypatch.setattr(ctypes, 'CDLL', lambda _path: Library())
    with pytest.raises(RuntimeError, match='version/ABI'):
        Native()
    assert set(lookups) == {'benchset_abi_version', 'benchset_version'}


@pytest.mark.usefixtures('without_cyclic_gc')
def test_address_bound_function_keeps_its_original_owner_alive() -> None:
    """A copied C function must keep a temporary callback's executable owner alive.

    This checks the new function table directly: HEAD had no address cloning.
    Assert the original still exists before calling its address, so a broken
    owner chain produces an assertion rather than invoking freed executable memory.
    Releasing the function must also release its owner without another GC pass.
    """
    functions = getattr(_native, '_Functions', None)
    if functions is None:
        pytest.skip('HEAD has no address-bound function table')
    originals: List[_Reference] = []

    class Library:
        def __getattr__(self, _name: str) -> Any:
            callback = ctypes.CFUNCTYPE(ctypes.c_int)(lambda: 71)
            originals.append(weakref.ref(callback))
            return callback

    function = functions(Library()).example
    gc.collect()
    assert originals[0]() is not None, 'the copied address outlived its executable callback owner'
    assert function() == 71
    del function
    assert originals[0]() is None, 'resolving a callback introduced a cyclic owner reference'
