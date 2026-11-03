"""SIGINT must never abandon an opened native image between ownership transfers."""

import _ctypes
import ctypes
import os
import shutil
import signal
import subprocess
import sys
import weakref
from pathlib import Path
from threading import Barrier, Lock, Thread, current_thread, main_thread
from types import FrameType, SimpleNamespace
from typing import Any, List, Optional, cast

import pytest

from benchset import _native

_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('boundary', ['open_return', 'opened', 'adopt_return', 'cache_return'])
@pytest.mark.parametrize('policy', ['default', 'raising', 'returning'])
def test_sigint_during_loading_keeps_exactly_one_owned_handle(  # noqa: PLR0915 — Trace all ownership boundaries with one handle ledger.
    monkeypatch: pytest.MonkeyPatch, boundary: str, policy: str,
) -> None:
    """Deliver real SIGINT before assignment, adoption and cache publication.

    A fake OS handle ledger avoids unloading the test runner's Rust library.
    Interrupting any boundary must leave one usable cached owner, preserve
    the original signal policy/exception, and never reopen or close that owner.
    The returning handler also checks that delivery sees completed adoption.
    """
    cached: Any = getattr(_native, '_open_library', _native._load_library)
    cached.cache_clear()
    raw_code = cached.__wrapped__.__code__
    opened: List[int] = []
    closed: List[int] = []
    deliveries: List[object] = []
    result = SimpleNamespace(_handle=701)
    adopted = False
    fired = False
    error = KeyboardInterrupt('original handler exception')
    cause = ValueError('original cause')
    error.__cause__ = cause
    previous = signal.getsignal(signal.SIGINT)
    previous_trace = sys.gettrace()

    def handler(signum: int, _frame: Optional[FrameType]) -> None:
        deliveries.append((signum, adopted, signal.getsignal(signal.SIGINT) is handler))
        if policy == 'raising':
            raise error

    selected = signal.default_int_handler if policy == 'default' else handler

    def open_image(_path: str, _mode: int) -> int:
        opened.append(701)
        return 701

    def construct(_path: str, *, handle: int) -> object:
        nonlocal adopted
        assert handle == 701
        adopted = True
        return result

    def trace(frame: FrameType, event: str, _value: object) -> Any:
        nonlocal fired
        hit = (
            (boundary == 'open_return' and event == 'return' and frame.f_code is open_image.__code__)
            or (boundary == 'opened' and event == 'line' and frame.f_code is raw_code
                and 'handle' in frame.f_locals)
            or (boundary == 'adopt_return' and event == 'return' and frame.f_code is construct.__code__)
            or (boundary == 'cache_return' and event == 'return' and frame.f_code is raw_code)
        )
        if hit and not fired:
            fired = True
            signal.raise_signal(signal.SIGINT)
        return trace

    monkeypatch.setattr(ctypes, '_dlopen', open_image, raising=False)
    monkeypatch.setattr(ctypes.cdll, '_dlltype', construct)
    monkeypatch.setattr(_ctypes, 'dlclose', closed.append, raising=False)
    monkeypatch.setattr(_ctypes, 'FreeLibrary', closed.append, raising=False)
    signal.signal(signal.SIGINT, selected)
    try:
        sys.settrace(trace)
        try:
            if policy == 'returning':
                assert _native._load_library('signal-probe') is result
            else:
                with pytest.raises(KeyboardInterrupt) as caught:
                    _native._load_library('signal-probe')
                if policy == 'raising':
                    assert caught.value is error
                    assert caught.value.__cause__ is cause
        finally:
            sys.settrace(previous_trace)
        assert fired, 'the intended ownership boundary was not exercised'
        assert signal.getsignal(signal.SIGINT) is selected
        assert cached.cache_info().currsize == 1, 'SIGINT abandoned an opened image outside the cache'
        assert _native._load_library('signal-probe') is result
        assert opened == [701], 'retry reopened an image whose first reference was abandoned'
        assert closed == [], 'the cached image must remain usable'
        if policy != 'default':
            assert deliveries == [(signal.SIGINT, True, True)]
    finally:
        signal.signal(signal.SIGINT, previous)
        cached.cache_clear()


def test_concurrent_loading_shares_one_owned_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent cold misses must release the redundant open without serializing dlopen.

    A barrier inside the opener models an audit hook waiting for another
    loading thread. Holding a loader lock across this hook would deadlock.
    Worker threads must never attempt to install Python signal handlers.
    """
    cached: Any = getattr(_native, '_open_library', _native._load_library)
    cached.cache_clear()
    barrier = Barrier(2)
    ledger_lock = Lock()
    opened: List[int] = []
    closed: List[int] = []
    results: List[Any] = []
    errors: List[BaseException] = []
    installations: List[Thread] = []
    install = signal.signal

    def install_on_main(signum: int, handler: Any) -> Any:
        installations.append(current_thread())
        return install(signum, handler)

    def open_image(_path: str, _mode: int) -> int:
        with ledger_lock:
            handle = 701 + len(opened)
            opened.append(handle)
        barrier.wait(timeout=5)
        return handle

    def construct(_path: str, *, handle: int) -> object:
        return SimpleNamespace(_handle=handle)

    def load() -> None:
        try:
            results.append(_native._load_library('concurrent-probe'))
        except BaseException as error:  # noqa: BLE001 — Propagate worker interruptions through the main assertion.
            errors.append(error)

    monkeypatch.setattr(ctypes, '_dlopen', open_image, raising=False)
    monkeypatch.setattr(ctypes.cdll, '_dlltype', construct)
    monkeypatch.setattr(_ctypes, 'dlclose', closed.append, raising=False)
    monkeypatch.setattr(_ctypes, 'FreeLibrary', closed.append, raising=False)
    monkeypatch.setattr(signal, 'signal', install_on_main)
    worker = Thread(target=load, daemon=True)
    try:
        worker.start()
        load()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert errors == []
        assert installations
        assert all(thread is main_thread() for thread in installations)
        assert len(results) == 2
        assert results[0] is results[1], 'concurrent callers retained separate raw references'
        assert len(opened) == 2
        assert closed == [handle for handle in opened if handle != results[0]._handle]
    finally:
        cached.cache_clear()


@pytest.mark.parametrize('raising', [False, True])
def test_deferred_signals_keep_frames_policies_and_release_temporaries(  # noqa: PLR0915 — Verify delivery and frame release without an intervening GC.
    monkeypatch: pytest.MonkeyPatch, raising: bool,
) -> None:
    """Deferred delivery keeps each originating frame and honors handler replacements.

    With cyclic GC disabled, frame-local payloads must disappear immediately
    after delivery, including unprocessed signals when a handler raises;
    storing deferred frames in their own frame would leak.
    """
    import gc  # noqa: PLC0415 — Keep GC manipulation local to its regression.

    cached: Any = getattr(_native, '_open_library', _native._load_library)
    cached.cache_clear()
    delivered: List[object] = []
    payloads: List[Any] = []
    adopted = False

    class Payload:
        pass

    def second(signum: int, frame: Optional[FrameType]) -> None:
        assert frame is not None
        delivered.append(('second', signum, frame.f_code is open_image.__code__, adopted))

    def first(signum: int, frame: Optional[FrameType]) -> None:
        assert frame is not None
        delivered.append(('first', signum, frame.f_code is open_image.__code__, adopted))
        if raising:
            raise KeyboardInterrupt
        signal.signal(signal.SIGINT, second)

    def open_image(_path: str, _mode: int) -> int:
        payload = Payload()
        payloads.append(weakref.ref(payload))
        signal.raise_signal(signal.SIGINT)
        signal.raise_signal(signal.SIGINT)
        return 701

    def construct(_path: str, *, handle: int) -> object:
        nonlocal adopted
        adopted = True
        return SimpleNamespace(_handle=handle)

    monkeypatch.setattr(ctypes, '_dlopen', open_image, raising=False)
    monkeypatch.setattr(ctypes.cdll, '_dlltype', construct)
    previous = signal.signal(signal.SIGINT, first)
    collecting = gc.isenabled()
    gc.disable()
    try:
        if raising:
            try:
                _native._load_library('frames-probe')
            except KeyboardInterrupt as error:
                error.__traceback__ = None  # Release the host-owned traceback before checking loader retention.
            else:
                pytest.fail('the handler exception was swallowed')
        else:
            _native._load_library('frames-probe')
        expected = [('first', signal.SIGINT, True, True)]
        if not raising:
            expected.append(('second', signal.SIGINT, True, True))
        assert delivered == expected
        assert signal.getsignal(signal.SIGINT) is (first if raising else second)
        assert all(reference() is None for reference in payloads), 'deferred frames formed a reference cycle'
    finally:
        signal.signal(signal.SIGINT, previous)
        cached.cache_clear()
        if collecting:
            gc.enable()


def test_shared_loading_leaves_non_cpython_lifetime_to_its_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """PyPy's CDLL owns its rawffi handle; deduplication must not manually close it a second time."""
    private, shared = SimpleNamespace(_handle=701), SimpleNamespace(_handle=702)
    owners = iter((private, shared))
    closed: List[int] = []
    monkeypatch.setattr(_native, '_AsDouble', None)
    monkeypatch.setattr(_native, '_open_library', lambda _path: next(owners))
    monkeypatch.setattr(_ctypes, 'dlclose', closed.append, raising=False)
    monkeypatch.setattr(_ctypes, 'FreeLibrary', closed.append, raising=False)
    assert _native._shared_library('pypy-probe') is shared
    assert closed == []


@pytest.mark.parametrize('stage', ['open', 'adopt'])
@pytest.mark.parametrize('policy', ['default', 'custom', 'ignore', 'native'])
def test_loading_failure_restores_the_original_signal_policy(
    monkeypatch: pytest.MonkeyPatch, stage: str, policy: str,
) -> None:
    """Rollback preserves the exact exception and leaves callable, ignored and native policies intact."""
    cached: Any = getattr(_native, '_open_library', _native._load_library)
    cached.cache_clear()
    error = OSError('original loading failure')
    closed: List[int] = []
    previous = signal.getsignal(signal.SIGINT)
    selected: Any = {'default': signal.default_int_handler, 'custom': lambda *_args: None,
                'ignore': signal.SIG_IGN, 'native': None}[policy]

    def open_image(_path: str, _mode: int) -> int:
        if stage == 'open':
            raise error
        return 701

    def construct(_path: str, *, handle: int) -> object:
        assert handle == 701
        raise error

    monkeypatch.setattr(ctypes, '_dlopen', open_image, raising=False)
    monkeypatch.setattr(ctypes.cdll, '_dlltype', construct)
    monkeypatch.setattr(_ctypes, 'dlclose', closed.append, raising=False)
    monkeypatch.setattr(_ctypes, 'FreeLibrary', closed.append, raising=False)
    if policy == 'native':
        monkeypatch.setattr(signal, 'getsignal', lambda _signum: None)
    else:
        signal.signal(signal.SIGINT, selected)
    try:
        with pytest.raises(OSError, match='original loading failure') as caught:
            _native._load_library('failure-probe')
        assert caught.value is error
        assert signal.getsignal(signal.SIGINT) is selected
        assert closed == ([701] if stage == 'adopt' else [])
        assert cached.cache_info().currsize == 0
    finally:
        signal.signal(signal.SIGINT, previous)
        cached.cache_clear()


def test_subinterpreter_loading_does_not_require_signal_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    """A subinterpreter's apparent main thread cannot install handlers but must still load normally."""
    result = object()
    calls: List[str] = []

    def unsupported(_signum: int, _handler: object) -> None:
        raise ValueError('signal only works in main thread of the main interpreter')

    def shared(path: str) -> object:
        calls.append(path)
        return result

    monkeypatch.setattr(signal, 'signal', unsupported)
    monkeypatch.setattr(_native, '_shared_library', shared)
    assert _native._load_library('subinterpreter-probe') is result
    assert calls == ['subinterpreter-probe']


@pytest.mark.parametrize('replacement', [signal.SIG_IGN, signal.SIG_DFL])
def test_deferred_delivery_honors_a_noncallable_replacement_policy(
    monkeypatch: pytest.MonkeyPatch, replacement: int,
) -> None:
    """A returning handler may ignore later signals or restore OS delivery, including deferred ones.

    Intercept OS redelivery so testing SIG_DFL cannot terminate the test runner.
    """
    cached: Any = getattr(_native, '_open_library', _native._load_library)
    cached.cache_clear()
    redelivered: List[int] = []
    raise_signal = signal.raise_signal

    def handler(_signum: int, _frame: Optional[FrameType]) -> None:
        signal.signal(signal.SIGINT, replacement)

    def open_image(_path: str, _mode: int) -> int:
        raise_signal(signal.SIGINT)
        raise_signal(signal.SIGINT)
        monkeypatch.setattr(signal, 'raise_signal', redelivered.append)
        return 701

    def construct(_path: str, *, handle: int) -> object:
        return SimpleNamespace(_handle=handle)

    monkeypatch.setattr(ctypes, '_dlopen', open_image, raising=False)
    monkeypatch.setattr(ctypes.cdll, '_dlltype', construct)
    previous = signal.signal(signal.SIGINT, handler)
    try:
        _native._load_library('replacement-probe')
        assert signal.getsignal(signal.SIGINT) is replacement
        assert redelivered == ([] if replacement == signal.SIG_IGN else [signal.SIGINT])
    finally:
        signal.signal(signal.SIGINT, previous)
        cached.cache_clear()


_REAL_LOADING = r'''
import _ctypes, ctypes, gc, os, signal, sys
from pathlib import Path
from benchset import _native
path, log, boundary = sys.argv[1:]
cached = getattr(_native, '_open_library', _native._load_library)
cached.cache_clear()
raw_code = cached.__wrapped__.__code__
original_open = ctypes._dlopen
fired = False
def open_image(path, mode):
    return original_open(path, mode)
ctypes._dlopen = open_image
def trace(frame, event, value):
    global fired
    hit = ((boundary == 'open_return' and event == 'return' and frame.f_code is open_image.__code__)
           or (boundary == 'opened' and event == 'line' and frame.f_code is raw_code
               and 'handle' in frame.f_locals)
           or (boundary == 'cache_return' and event == 'return' and frame.f_code is raw_code))
    if hit and not fired:
        fired = True
        signal.raise_signal(signal.SIGINT)
    return trace
os.environ['BENCHSET_LOADER_LOG'] = log
os.environ['BENCHSET_LOADER_INTERRUPT'] = '1' if boundary == 'constructor' else '0'
signal.signal(signal.SIGINT, signal.default_int_handler)
sys.settrace(trace)
try:
    _native._load_library(path)
except KeyboardInterrupt:
    pass
else:
    raise AssertionError('SIGINT was swallowed')
finally:
    sys.settrace(None)
os.environ['BENCHSET_LOADER_INTERRUPT'] = '0'
library = _native._load_library(path)
function = library.loader_probe
function.restype = ctypes.c_int
assert function() == 7, 'the surviving cache owner is unusable'
handle = library._handle
cached.cache_clear()
del function, library
gc.collect()
assert Path(log).read_text() == 'L', 'a live owner was unloaded'
_ctypes.dlclose(handle)  # Release the one intentional process-lived cache reference.
assert Path(log).read_text() == 'LU', 'an interrupted load leaked an additional OS reference'
'''


@pytest.fixture(scope='module')
def unload_probe(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a separate image whose constructor/destructor expose actual loader reference counts."""
    if os.name == 'nt':
        pytest.skip('POSIX constructor/destructor probe; Windows ownership uses the intercepted OS ledger')
    compiler = shutil.which('cc') or shutil.which('clang') or shutil.which('gcc')
    if compiler is None:
        pytest.skip('No C compiler for the real unloading probe')
    directory = tmp_path_factory.mktemp('loader-signals')
    source = directory / 'probe.c'
    source.write_text('''#include <stdio.h>
#include <stdlib.h>
#include <signal.h>
static void event(char value) {
    FILE *log = fopen(getenv("BENCHSET_LOADER_LOG"), "a");
    if (!log) abort();
    fputc(value, log); fclose(log);
}
__attribute__((constructor)) static void loaded(void) {
    event('L');
    if (getenv("BENCHSET_LOADER_INTERRUPT")[0] == '1') raise(SIGINT);
}
__attribute__((destructor)) static void unloaded(void) { event('U'); }
int loader_probe(void) { return 7; }
''')
    image = directory / 'probe.so'
    result = subprocess.run([compiler, '-shared', '-fPIC', str(source), '-o', str(image)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return image


@pytest.mark.parametrize('boundary', ['constructor', 'open_return', 'opened', 'cache_return'])
def test_real_sigint_does_not_leak_an_os_loader_reference(
    unload_probe: Path, tmp_path: Path, boundary: str,
) -> None:
    """After interruption and retry, exactly one dlclose must unload the independent image.

    Include SIGINT raised inside the actual C constructor, before dlopen can
    return its handle. A subprocess safely isolates real signals and unloading.
    """
    result = subprocess.run([sys.executable, '-c', _REAL_LOADING, str(unload_probe),
                             str(tmp_path / 'events'), boundary], cwd=_ROOT,
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('platform', ['darwin', 'linux', 'win32'])
def test_reentrant_loading_releases_the_surplus_reference(
    monkeypatch: pytest.MonkeyPatch, platform: str,
) -> None:
    """A loader audit hook may reenter loading; only the shared cache owner may survive.

    Both misses open the same image. A subsequent SIGINT must not strand the
    outer, uncached OS reference, and the nested owner must not be closed.
    Use the absolute path supplied by Native on every platform: Windows
    normalizes relative names before entering the opener's audit hook.
    """
    cached: Any = getattr(_native, '_open_library', _native._load_library)
    cached.cache_clear()
    opened: List[int] = []
    closed: List[int] = []
    nested: List[object] = []
    requested = os.path.abspath('reentrant-probe')  # noqa: PTH100 — Match ctypes on legacy Windows too.
    monkeypatch.setattr(_native, 'sys', SimpleNamespace(platform=platform))

    def open_image(path: str, _mode: int) -> int:
        assert path == requested
        handle = 701 + len(opened)
        opened.append(handle)
        if len(opened) == 1:
            nested.append(_native._load_library(path))
            signal.raise_signal(signal.SIGINT)
        return handle

    def construct(_path: str, *, handle: int) -> object:
        return SimpleNamespace(_handle=handle)

    monkeypatch.setattr(ctypes, '_dlopen', open_image, raising=False)
    monkeypatch.setattr(ctypes.cdll, '_dlltype', construct)
    monkeypatch.setattr(_ctypes, 'dlclose', closed.append, raising=False)
    monkeypatch.setattr(_ctypes, 'FreeLibrary', closed.append, raising=False)
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            _native._load_library(requested)
        assert opened == [701, 702]
        assert closed == [701], 'only the redundant outer reference should be released'
        assert _native._load_library(requested) is nested[0]
        assert cast(Any, nested[0])._handle == 702
    finally:
        signal.signal(signal.SIGINT, previous)
        cached.cache_clear()
