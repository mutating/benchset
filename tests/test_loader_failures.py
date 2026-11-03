"""Loader failure and real-symbol ownership must not create new lifetime regressions."""

import _ctypes
import ctypes
import gc
import os
import sys
import weakref
from _ctypes import CFuncPtr
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Tuple, Type, cast

import pytest

from benchset import _native, run
from benchset._native import ABI_VERSION, Native
from tests.test_resources import (
    CallbackProbe,
    without_cyclic_gc,  # noqa: F401 — Shared pytest fixture.
)


def _clear_standard_loader() -> None:
    loader = getattr(_native, '_open_library', None)
    if loader is not None:
        loader.cache_clear()


def _function_type_count() -> int:
    return sum(isinstance(value, type) and issubclass(value, CFuncPtr) and value.__name__ == '_FuncPtr'
               for value in gc.get_objects())


def _library_path() -> str:
    filename = 'benchset.dll' if os.name == 'nt' else (
        'libbenchset.dylib' if sys.platform == 'darwin' else 'libbenchset.so'
    )
    return str(Path(_native.__file__).parent / '_native' / filename)


@pytest.mark.usefixtures('without_cyclic_gc')
def test_failed_native_loads_do_not_accumulate_function_types(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated invalid API calls must not retain one dynamic ctypes class per missing Rust image.

    Keep the standard CDLL factory and fail its low-level open. HEAD rejects
    the argument without loading; the new Rust validation path still has to
    return failed results without growing temporary constructor types.
    """
    _clear_standard_loader()

    def missing(_path: object, _mode: int) -> int:
        raise OSError('missing Rust image')

    monkeypatch.setattr(ctypes, '_dlopen', missing, raising=False)
    gc.collect()
    baseline = _function_type_count()
    for _batch in range(3):
        for _attempt in range(20):
            result = run('unknown', warmup_seconds=0, measurement_seconds=.1)
            assert result.status == 'failed'
        assert _function_type_count() - baseline <= 1, 'failed loading retains a cyclic function class per call'


def test_standard_loader_retries_failures_and_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failures are retried as fresh attempts so a subsequently available Rust image can load."""
    _clear_standard_loader()
    original_open = cast(Any, getattr(ctypes, '_dlopen', getattr(_ctypes, 'LoadLibrary', None)))
    attempts = 0

    def open_after_two_failures(path: Any, mode: int) -> int:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise OSError('transient load failure ' + str(attempts))
        return cast(int, original_open(path, mode))

    monkeypatch.setattr(ctypes, '_dlopen', open_after_two_failures, raising=False)
    for attempt in (1, 2):
        with pytest.raises(OSError, match='transient load failure ' + str(attempt)):
            Native()
    assert Native().lib.benchset_abi_version() == ABI_VERSION
    assert attempts == 3


def test_standard_loader_preflights_and_adopts_the_open_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed open must precede wrapper allocation, with the successful handle adopted once.

    Check the host's original ctypes search mode: POSIX DEFAULT_MODE or the
    secure Windows absolute-path defaults. This is a new private bridge check;
    HEAD had no cached loader helper.
    """
    loader = getattr(_native, '_open_library', None)
    if loader is None:
        pytest.skip('HEAD has no standard loader helper')
    loader.cache_clear()
    original_open = cast(Any, getattr(ctypes, '_dlopen', getattr(_ctypes, 'LoadLibrary', None)))
    original_initialize = ctypes.CDLL.__init__
    calls: List[Tuple[str, object, object]] = []

    def observe_open(path: Any, mode: int) -> int:
        handle = cast(int, original_open(path, mode))
        calls.append(('open', path, mode))
        return handle

    def initialize(library: ctypes.CDLL, name: Any, *args: Any, **kwargs: Any) -> None:
        calls.append(('wrapper', name, kwargs.get('handle')))
        original_initialize(library, name, *args, **kwargs)

    monkeypatch.setattr(ctypes, '_dlopen', observe_open, raising=False)
    monkeypatch.setattr(ctypes.CDLL, '__init__', initialize)
    path = _library_path()
    library = loader(path)
    expected_mode = 0x1100 if os.name == 'nt' else ctypes.DEFAULT_MODE
    assert calls == [('open', path, expected_mode), ('wrapper', path, library._handle)]


def test_cold_native_opens_the_real_image_once_and_reuses_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful cold binding owns one open, including with newer ctypes.

    Python 3.14's POSIX CDLL hook can reopen a path despite receiving a handle.
    Observe actual image opens, then check a warm binding still shares the
    image while configuring its own function objects. No fake handle is called.
    """
    _clear_standard_loader()
    original_open = cast(Any, getattr(ctypes, '_dlopen', getattr(_ctypes, 'LoadLibrary', None)))
    calls: List[Tuple[str, int]] = []

    def observe_open(path: str, mode: int) -> int:
        calls.append((path, mode))
        return cast(int, original_open(path, mode))

    monkeypatch.setattr(ctypes, '_dlopen', observe_open, raising=False)
    first, second = Native(), Native()
    assert first.lib.benchset_abi_version() == second.lib.benchset_abi_version() == ABI_VERSION
    assert first.lib._library is second.lib._library
    assert first.lib.benchset_abi_version is not second.lib.benchset_abi_version
    expected_mode = 0x1100 if os.name == 'nt' else ctypes.DEFAULT_MODE
    assert calls == [(_library_path(), expected_mode)], 'a supplied native handle must not reopen its path'


@pytest.mark.parametrize('platform', ['darwin', 'linux', 'win32'])
def test_modern_cdll_hook_adopts_the_preopened_handle_without_reopening(
    monkeypatch: pytest.MonkeyPatch, platform: str,
) -> None:
    """Exercise modern ctypes handle adoption on every supported Python version.

    A simulated canonical constructor dispatches through the modern hook. Its
    base hook deliberately ignores the supplied handle, as Python 3.14 POSIX
    does. The bridge must adopt the first open, preserve its normalized name,
    and keep platform search flags without relying on Python version checks.
    Fake handles stay inside this constructor; no native symbol is invoked.
    """
    canonical = cast(Any, ctypes.cdll)._dlltype
    calls: List[Tuple[Any, ...]] = []

    def open_image(path: str, mode: int) -> int:
        handle = 701 + len(calls)
        calls.append(('open', path, mode, handle))
        return handle

    def reopen(library: Any, name: str, mode: int, handle: int, winmode: object) -> int:
        library._name = name
        calls.append(('reopen', name, handle, winmode))
        return open_image(name, mode)

    def initialize(library: Any, name: str, *_args: Any, **kwargs: Any) -> None:
        handle = kwargs.get('handle')
        calls.append(('wrapper', name, handle))
        library._handle = library._load_library(name, ctypes.DEFAULT_MODE, handle, None)

    monkeypatch.setattr(_native, 'sys', SimpleNamespace(platform=platform))
    monkeypatch.setattr(ctypes, '_dlopen', open_image, raising=False)
    monkeypatch.setattr(canonical, '_load_library', reopen, raising=False)
    monkeypatch.setattr(canonical, '__init__', initialize)
    requested = 'subdir/../adoption-probe.dll'
    expected_path = os.path.abspath(requested) if platform == 'win32' else requested  # noqa: PTH100 — Match CDLL.
    expected_mode = 0x1100 if platform == 'win32' else ctypes.DEFAULT_MODE
    library = _native._open_library.__wrapped__(requested)
    assert calls == [('open', expected_path, expected_mode, 701), ('wrapper', expected_path, 701)]
    assert library._handle == 701
    assert library._name == expected_path


def test_address_bound_real_symbol_keeps_library_owner_alive() -> None:
    """A retained real symbol must retain its library's resources after the function table is gone."""
    functions = getattr(_native, '_Functions', None)
    if functions is None:
        pytest.skip('HEAD has no address-bound function table')

    class Library(ctypes.CDLL):
        payload: CallbackProbe

    library = Library(_library_path())
    library.payload = CallbackProbe(None)
    observed = weakref.ref(library)
    payload = weakref.ref(library.payload)
    table = functions(library)
    function = table.benchset_abi_version
    del table, library
    gc.collect()
    assert observed() is not None, 'the copied symbol lost its real library owner'
    assert payload() is not None
    # Only call after proving that its owner is still live.
    function.argtypes = []
    function.restype = ctypes.c_uint32
    assert function() == ABI_VERSION
    del function
    gc.collect()
    assert observed() is None
    assert payload() is None


@pytest.mark.parametrize('platform', ['darwin', 'linux', 'win32'])
@pytest.mark.parametrize('alias_present', [True, False])
def test_standard_loader_keeps_platform_modes_and_normalizes_windows_path(
    monkeypatch: pytest.MonkeyPatch, platform: str, alias_present: bool,
) -> None:
    """Exercise platform loading rules on every host without loading a foreign binary.

    Spies provide a handle and its wrapper; simulated Windows must normalize
    the relative path exactly as CDLL does and keep its secure search flags.
    Both POSIX variants keep the path and DEFAULT_MODE unchanged. Removing
    ctypes' legacy alias reproduces modern Windows: the raw platform opener
    must still work without changing ownership, paths or search flags.
    """
    loader = getattr(_native, '_open_library', None)
    if loader is None:
        pytest.skip('HEAD has no standard loader helper')
    calls: List[Tuple[str, str, object]] = []
    handle = 12345
    result = object()

    def open_image(path: str, mode: int) -> int:
        calls.append(('open', path, mode))
        return handle

    def construct(path: str, *, handle: int) -> object:
        calls.append(('wrapper', path, handle))
        return result

    monkeypatch.setattr(_native, 'sys', SimpleNamespace(platform=platform))
    if alias_present:
        monkeypatch.setattr(ctypes, '_dlopen', open_image, raising=False)
    else:
        monkeypatch.delattr(ctypes, '_dlopen', raising=False)
        monkeypatch.setattr(_ctypes, 'LoadLibrary' if platform == 'win32' else 'dlopen',
                            open_image, raising=False)
    monkeypatch.setattr(ctypes.cdll, '_dlltype', construct)
    requested = 'subdir/../benchset.dll'
    expected_path = os.path.abspath(requested) if platform == 'win32' else requested  # noqa: PTH100 — Match CDLL without resolving symlinks.
    expected_mode = 0x1100 if platform == 'win32' else ctypes.DEFAULT_MODE
    assert loader.__wrapped__(requested) is result
    assert calls == [('open', expected_path, expected_mode), ('wrapper', expected_path, handle)]


@pytest.mark.parametrize('platform', ['darwin', 'linux', 'win32'])
@pytest.mark.parametrize('error_type', [MemoryError, KeyboardInterrupt, SystemExit])
def test_failed_wrapper_adoption_closes_only_its_open_handle_and_retries(  # noqa: PLR0915 — Observe open/adopt/rollback/recovery in one lifetime.
    monkeypatch: pytest.MonkeyPatch, platform: str, error_type: Type[BaseException],
) -> None:
    """A successful raw open must be rolled back if ctypes cannot allocate its wrapper.

    HEAD constructs its dynamic function class before opening the image, so it
    cannot reach this new private-helper boundary. Simulated platforms use
    fake handles and intercepted OS closers; no foreign or fake handle reaches
    a real close function. Two failed adoptions must each close exactly their
    own handle, preserve the original error and remain uncached. Recovery then
    adopts and caches its handle without closing the successful image.
    """
    loader = getattr(_native, '_open_library', None)
    if loader is None:
        pytest.skip('HEAD has no pre-opened wrapper adoption helper')
    loader.cache_clear()
    calls: List[Tuple[Any, ...]] = []
    context, cause = LookupError('existing context'), ValueError('existing cause')
    errors = [error_type('first wrapper allocation failed'), error_type('second wrapper allocation failed')]
    for error in errors:
        error.__context__, error.__cause__ = context, cause
    result = object()
    opened: List[int] = []

    def open_image(path: str, mode: int) -> int:
        handle = 101 + len(opened)
        opened.append(handle)
        calls.append(('open', path, mode, handle))
        return handle

    def construct(path: str, *, handle: int) -> object:
        calls.append(('wrapper', path, handle))
        if len(opened) <= len(errors):
            raise errors[len(opened) - 1]
        return result

    def close_posix(handle: int) -> None:
        calls.append(('dlclose', handle))

    def close_windows(handle: int) -> None:
        calls.append(('FreeLibrary', handle))

    monkeypatch.setattr(_native, 'sys', SimpleNamespace(platform=platform))
    monkeypatch.setattr(ctypes, '_dlopen', open_image, raising=False)
    monkeypatch.setattr(ctypes.cdll, '_dlltype', construct)
    monkeypatch.setattr(_ctypes, 'dlclose', close_posix, raising=False)
    monkeypatch.setattr(_ctypes, 'FreeLibrary', close_windows, raising=False)
    requested = 'subdir/../adoption-probe.dll'
    expected_path = os.path.abspath(requested) if platform == 'win32' else requested  # noqa: PTH100
    expected_mode = 0x1100 if platform == 'win32' else ctypes.DEFAULT_MODE
    closer = 'FreeLibrary' if platform == 'win32' else 'dlclose'
    expected: List[Tuple[Any, ...]] = []
    try:
        for index, original in enumerate(errors):
            with pytest.raises(error_type) as caught:
                loader(requested)
            assert caught.value is original, 'rollback must preserve the constructor exception'
            assert caught.value.__context__ is context
            assert caught.value.__cause__ is cause
            assert caught.value.__suppress_context__ is True
            handle = 101 + index
            expected.extend([('open', expected_path, expected_mode, handle),
                             ('wrapper', expected_path, handle), (closer, handle)])
            assert calls == expected, 'each failed adoption must close its raw image exactly once'
            assert loader.cache_info().currsize == 0, 'a failed adoption must not enter the loader cache'
        assert loader(requested) is result
        expected.extend([('open', expected_path, expected_mode, 103), ('wrapper', expected_path, 103)])
        assert calls == expected, 'successful adoption must retain its opened image'
        assert loader(requested) is result
        assert calls == expected, 'a recovered standard image must be cached'
        assert loader.cache_info().currsize == 1
    finally:
        loader.cache_clear()


@pytest.mark.parametrize('platform', ['darwin', 'linux', 'win32'])
def test_failed_raw_open_never_constructs_or_closes_a_wrapper(
    monkeypatch: pytest.MonkeyPatch, platform: str,
) -> None:
    """An unsuccessful open owns no handle to close and must remain retryable.

    This complements failed-adoption coverage: rollback must begin only after
    _dlopen returned a real handle, while a recovered successful image remains
    open. Both close entry points are intercepted on every host.
    """
    loader = getattr(_native, '_open_library', None)
    if loader is None:
        pytest.skip('HEAD has no pre-opened wrapper adoption helper')
    loader.cache_clear()
    calls: List[Tuple[Any, ...]] = []
    original = OSError('raw open failed before owning a handle')
    attempts = 0
    result = object()

    def open_image(path: str, mode: int) -> int:
        nonlocal attempts
        attempts += 1
        calls.append(('open', path, mode))
        if attempts == 1:
            raise original
        return 701

    def construct(path: str, *, handle: int) -> object:
        calls.append(('wrapper', path, handle))
        return result

    def close_posix(handle: int) -> None:
        calls.append(('dlclose', handle))

    def close_windows(handle: int) -> None:
        calls.append(('FreeLibrary', handle))

    monkeypatch.setattr(_native, 'sys', SimpleNamespace(platform=platform))
    monkeypatch.setattr(ctypes, '_dlopen', open_image, raising=False)
    monkeypatch.setattr(ctypes.cdll, '_dlltype', construct)
    monkeypatch.setattr(_ctypes, 'dlclose', close_posix, raising=False)
    monkeypatch.setattr(_ctypes, 'FreeLibrary', close_windows, raising=False)
    requested = 'subdir/../adoption-probe.dll'
    expected_path = os.path.abspath(requested) if platform == 'win32' else requested  # noqa: PTH100
    expected_mode = 0x1100 if platform == 'win32' else ctypes.DEFAULT_MODE
    try:
        with pytest.raises(OSError, match='raw open failed before owning a handle') as caught:
            loader(requested)
        assert caught.value is original
        assert calls == [('open', expected_path, expected_mode)], 'a failed open must never be closed'
        assert loader.cache_info().currsize == 0
        assert loader(requested) is result
        assert calls == [('open', expected_path, expected_mode), ('open', expected_path, expected_mode),
                         ('wrapper', expected_path, 701)]
        assert loader(requested) is result
        assert len(calls) == 3
    finally:
        loader.cache_clear()
