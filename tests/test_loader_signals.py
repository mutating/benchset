"""SIGINT must never abandon an opened native image between ownership transfers."""

import _ctypes
import builtins
import ctypes
import os
import shutil
import signal
import subprocess
import sys
import weakref
from importlib.util import find_spec
from pathlib import Path
from threading import Barrier, Lock, Thread, current_thread, main_thread
from types import FrameType, SimpleNamespace
from typing import Any, List, Optional, Type, cast

import pytest

from benchset import _native, _signal_state

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

    def install_on_main(signum: int, handler: Any, _native_action: Any) -> Any:
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
    monkeypatch.setattr(_native, '_set_signal', install_on_main)
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


@pytest.mark.parametrize('replacement', [signal.SIG_IGN, signal.SIG_DFL])
def test_snapshot_lookup_preserves_a_new_noncallable_policy(
    monkeypatch: pytest.MonkeyPatch, replacement: signal.Handlers,
) -> None:
    """A host policy selected during snapshot lookup bypasses temporary signal ownership.

    Model the audit hook's choice without installing process-wide handlers so
    both ignore and default policies are checked on every platform.
    """
    selected: object = signal.default_int_handler
    events: List[str] = []
    result = object()

    def current(_signum: int) -> object:
        return selected

    def resolve() -> None:
        nonlocal selected
        events.append('lookup')
        selected = replacement

    def install(*_arguments: object) -> None:
        pytest.fail('the new noncallable policy must not be temporarily replaced')

    def load(path: str) -> object:
        events.append(path)
        return result

    monkeypatch.setattr(_native, 'signal', SimpleNamespace(SIGINT=signal.SIGINT, getsignal=current))
    monkeypatch.setattr(_native, '_get_signal', current)
    monkeypatch.setattr(_native, '_native_sigaction', resolve)
    monkeypatch.setattr(_native, '_set_signal', install)
    monkeypatch.setattr(_native, '_shared_library', load)
    assert _native._load_library('policy-selected-during-lookup') is result
    assert events == ['lookup', 'policy-selected-during-lookup']
    assert selected is replacement


@pytest.mark.parametrize('message', [
    'signal only works in main thread',
    'signal only works in main thread of the main interpreter',
    'signal only works in main thread or with __pypy__.thread.enable_signals()',
])
def test_subinterpreter_loading_does_not_require_signal_registration(
    monkeypatch: pytest.MonkeyPatch, message: str,
) -> None:
    """A subinterpreter's apparent main thread cannot install handlers but must still load normally."""
    result = object()
    calls: List[str] = []

    def unsupported(_signum: int, _handler: object, _native_action: Any) -> None:
        raise ValueError(message)

    def shared(path: str) -> object:
        calls.append(path)
        return result

    monkeypatch.setattr(_native, '_set_signal', unsupported)
    monkeypatch.setattr(_native, '_shared_library', shared)
    assert _native._load_library('subinterpreter-probe') is result
    assert calls == ['subinterpreter-probe']


@pytest.mark.parametrize('context', ['simulated', 'subinterpreter'])
def test_subinterpreter_signal_refusal_survives_an_introspection_denial(context: str) -> None:
    """Forbidden traceback inspection must not turn a normal signal refusal into a loader failure.

    Exercise a shared-GIL subinterpreter that permits ctypes when available,
    and a synthetic setter on every platform so coverage does not depend on
    experimental interpreter APIs. Test Native directly because public runs
    can independently require signal registration on older interpreters.
    """
    if context == 'subinterpreter' and find_spec('_xxsubinterpreters') is None:
        pytest.skip('The experimental CPython subinterpreter harness is unavailable')
    setup = '''
from benchset import _native
def unsupported(_signum, _handler, _action):
    raise ValueError('signal only works in main thread of the main interpreter')
_native._set_signal = unsupported
expected = object()
_native._shared_library = lambda _path: expected
'''
    check = ("assert _native._load_library('restricted-interpreter') is expected" if context == 'simulated' else
             "assert _native.Native().lib.benchset_abi_version() == _native.ABI_VERSION")
    inner = f'''
import sys
sys.path.insert(0, {str(_ROOT)!r})
from benchset import _native
{setup if context == 'simulated' else ''}
def audit(event, args):
    if event == 'object.__getattr__' and args[1] == 'f_code':
        raise PermissionError('frame introspection forbidden')
sys.addaudithook(audit)
{check}
'''
    source = inner
    environment = os.environ.copy()
    if context == 'subinterpreter':
        source = f'''
import _xxsubinterpreters as interpreters
try:
    interpreter = interpreters.create(isolated=False)
except TypeError:
    interpreter = interpreters.create()
try:
    interpreters.run_string(interpreter, {inner!r})
finally:
    interpreters.destroy(interpreter)
'''
        # Coverage's startup hook can deadlock CPython 3.10 interpreter teardown.
        environment.pop('COVERAGE_PROCESS_START', None)
    result = subprocess.run([sys.executable, '-c', source], cwd=_ROOT, env=environment,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(('error', 'installed', 'message'), [
    (KeyboardInterrupt, True, 'interrupted installation'),
    (ValueError, True, 'signal only works in main thread of the main interpreter'),
    (ValueError, False, 'interrupted installation'),
])
def test_interrupted_signal_installation_restores_policy(
    monkeypatch: pytest.MonkeyPatch, error: Type[BaseException], installed: bool, message: str,
) -> None:
    """An exception during registration must restore SIGINT and abort the load.

    Interrupt before or after the real handler change. A caller's ValueError
    at either boundary must propagate instead of enabling subinterpreter fallback.
    """
    install = signal.signal
    original = signal.default_int_handler
    previous = install(signal.SIGINT, original)
    interruption = error(message)

    def interrupt_install(signum: int, handler: Any, _native_action: Any) -> None:
        if handler is original or installed:
            install(signum, handler)
        if handler is not original:
            raise interruption

    def unexpected_load(_path: str) -> None:
        pytest.fail('loading must stop after an interrupted signal installation')

    monkeypatch.setattr(_native, '_set_signal', interrupt_install)
    monkeypatch.setattr(_native, '_shared_library', unexpected_load)
    try:
        with pytest.raises(error) as caught:
            _native._load_library('interrupted-install-probe')
        assert caught.value is interruption
        assert signal.getsignal(signal.SIGINT) is original
    finally:
        install(signal.SIGINT, previous)


@pytest.mark.parametrize(('error', 'interruptions', 'opaque_property'), [
    (RuntimeError, 1, None), (RuntimeError, 2, None), (OSError, 2, None),
    (OSError, 2, '__traceback__'), (Exception, 2, '__class__'),
])
def test_interrupted_signal_restoration_releases_policy(
    monkeypatch: pytest.MonkeyPatch, error: Type[Exception], interruptions: int, opaque_property: Optional[str],
) -> None:
    """Interrupted restoration attempts must leave the caller's policy active.

    Each attempt models another signal raising before the handler changes.
    Restore the policy and propagate the first error even when delivery repeats.
    Classify the error without evaluating the caller's traceback or class override.
    """
    install = signal.signal
    original = signal.default_int_handler
    previous = install(signal.SIGINT, original)
    if opaque_property is not None:
        def inaccessible(_error: BaseException) -> None:
            raise RuntimeError('caller exception property must not run')

        error = type('OpaqueError', (error,), {opaque_property: property(inaccessible)})
    errors = [error(f'interrupted restoration {i}') for i in range(interruptions)]
    attempts = 0

    def caller_signal(index: int) -> None:
        raise errors[index]

    def interrupt_restore(signum: int, handler: Any, _native_action: Any) -> Any:
        nonlocal attempts
        if handler is original and attempts < interruptions:
            attempts += 1
            caller_signal(attempts - 1)
        return install(signum, handler)

    monkeypatch.setattr(_native, '_set_signal', interrupt_restore)
    monkeypatch.setattr(_native, '_shared_library', lambda _path: object())
    try:
        captured: List[BaseException] = []
        try:
            _native._load_library('interrupted-restore-probe')
        except BaseException as caught:  # noqa: BLE001 — Do not let pytest read the test's traceback override.
            captured.append(caught)
        assert len(captured) == 1
        same_error = captured[0] is errors[0]
        assert same_error
        assert signal.getsignal(signal.SIGINT) is original
    finally:
        install(signal.SIGINT, previous)


def test_signal_restoration_surfaces_registration_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A direct refusal to restore SIGINT must terminate and keep delivery usable.

    A second attempted restoration releases the fake guard so a mutant that
    retries an OS failure cannot hang the test process.
    """
    install = signal.signal
    delivered: List[int] = []

    def original(signum: int, _frame: Optional[FrameType]) -> None:
        delivered.append(signum)

    previous = install(signal.SIGINT, original)
    failure = OSError(1, 'signal registration refused')
    attempts = 0

    def fail_restore(signum: int, handler: Any, _native_action: Any) -> Any:
        nonlocal attempts
        if handler is original:
            attempts += 1
            if attempts == 2:
                install(signum, handler)
            raise failure
        return install(signum, handler)

    monkeypatch.setattr(_native, '_set_signal', fail_restore)
    monkeypatch.setattr(_native, '_shared_library', lambda _path: object())
    try:
        with pytest.raises(OSError, match='signal registration refused') as caught:
            _native._load_library('failed-restore-probe')
        assert caught.value is failure
        assert attempts == 1
        signal.raise_signal(signal.SIGINT)
        assert delivered == [signal.SIGINT]
    finally:
        install(signal.SIGINT, previous)


@pytest.mark.parametrize('phase', ['install', 'restore', 'no-displaced-handler'])
def test_signal_change_during_registration_is_preserved(monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    """Use the policy actually displaced by registration or cleanup.

    Another signal can choose a replacement after getsignal but before the
    real signal.signal call. The earlier observation must not override it.
    """
    install = signal.signal
    original = signal.default_int_handler
    replacement = signal.SIG_IGN
    previous = install(signal.SIGINT, original)
    changed = False
    temporary: List[Any] = []

    def choose_replacement(signum: int, handler: Any, _native_action: Any) -> Any:
        nonlocal changed
        if not changed and handler is not original:
            temporary.append(handler)
        if phase == 'no-displaced-handler':
            changed = True
            install(signum, handler)
            return None
        if not changed and (handler is original) == (phase == 'restore'):
            install(signum, replacement)
            changed = True
        return install(signum, handler)

    monkeypatch.setattr(_native, '_set_signal', choose_replacement)
    monkeypatch.setattr(_native, '_shared_library', lambda _path: object())
    try:
        _native._load_library('concurrent-policy-probe')
        assert changed
        assert signal.getsignal(signal.SIGINT) is (original if phase == 'no-displaced-handler' else replacement)
        if phase == 'install':
            assert len(temporary) == 1
            temporary[0](signal.SIGINT, None)
    finally:
        install(signal.SIGINT, previous)


@pytest.mark.skipif(sys.implementation.name != 'cpython', reason='CPython ctypes.dlopen audit event')
@pytest.mark.parametrize(('pending', 'selection'), [
    (0, 'keep'), (1, 'keep'), (1, 'new'), (2, 'keep'), (2, 'reinstall'), (2, 'new'),
])
def test_loader_preserves_sigint_handler_selected_by_audit_hook(pending: int, selection: str) -> None:
    """A cold public run must keep a caller's SIGINT replacement from ctypes.dlopen.

    Use a subprocess so the audit hook and its first load cannot affect other tests.
    The first queued signal belongs to the original handler. Later ones use
    the active handler, including when it has been installed again.
    """
    source = '''
import os
import signal
import sys
from pathlib import Path
from benchset import run

pending = int(sys.argv[1])
selection = sys.argv[2]
events = []
def original(_signum, _frame):
    events.append('original')
    if selection == 'new':
        signal.signal(signal.SIGINT, during_delivery)
    elif selection == 'reinstall':
        signal.signal(signal.SIGINT, replacement)
def replacement(_signum, _frame):
    events.append('replacement')
def during_delivery(_signum, _frame):
    events.append('during delivery')
def audit(event, args):
    if (event == 'ctypes.dlopen' and args[0] is not None
            and Path(os.fsdecode(args[0])).stem in ('benchset', 'libbenchset')):
        events.append('audit')
        for _ in range(pending):
            signal.raise_signal(signal.SIGINT)
        signal.signal(signal.SIGINT, replacement)

signal.signal(signal.SIGINT, original)
sys.addaudithook(audit)
assert run('single_core', warmup_seconds=-1).status == 'failed'
assert signal.getsignal(signal.SIGINT) is (during_delivery if selection == 'new' else replacement)
signal.raise_signal(signal.SIGINT)
assert events == (['audit'] + (['original'] if pending else [])
                  + ['during delivery' if selection == 'new' else 'replacement'] * max(pending, 1)), events
'''
    result = subprocess.run([sys.executable, '-c', source, str(pending), selection], cwd=_ROOT,
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(sys.implementation.name != 'cpython', reason='CPython ctypes.dlopen audit event')
@pytest.mark.parametrize('conversion', ['raises', 'ignore', 'no-longer-callable'])
def test_loader_restores_a_callable_without_converting_it_again(conversion: str) -> None:
    """A registered callable remains the handler if its later int conversion changes.

    The cold load triggers the change in an audit hook. The timeout catches
    retries of a deterministic conversion error without hanging the test run.
    If the object loses its call method, surface the registration refusal.
    """
    source = '''
import signal
import sys
from benchset import run

changed = False
received = []
class Handler:
    def __int__(self):
        if not changed:
            raise TypeError('register this as a callable')
        if sys.argv[1] == 'ignore':
            return 1
        raise RuntimeError('do not convert a registered handler again')
    def __call__(self, signum, _frame):
        received.append(signum)

def audit(event, _args):
    global changed
    if event == 'ctypes.dlopen':
        changed = True
        if sys.argv[1] == 'no-longer-callable':
            del Handler.__call__

handler = Handler()
signal.signal(signal.SIGINT, handler)
sys.addaudithook(audit)
if sys.argv[1] == 'no-longer-callable':
    try:
        run('single_core', warmup_seconds=-1)
    except TypeError:
        assert changed
        sys.exit(0)
    raise AssertionError('invalid handler registration was not reported')
assert run('single_core', warmup_seconds=-1).status == 'failed'
assert changed and signal.getsignal(signal.SIGINT) is handler
signal.raise_signal(signal.SIGINT)
assert received == [signal.SIGINT]
'''
    result = subprocess.run([sys.executable, '-c', source, conversion], cwd=_ROOT,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


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
    source.write_text('''#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
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
int sigint_restart(void) {
    struct sigaction action;
    if (sigaction(SIGINT, NULL, &action) != 0) return -1;
    return (action.sa_flags & SA_RESTART) != 0;
}
size_t sigaction_size(void) { return sizeof(struct sigaction); }
size_t sigaction_alignment(void) { return _Alignof(struct sigaction); }
''')
    image = directory / 'probe.so'
    result = subprocess.run([compiler, '-shared', '-fPIC', str(source), '-o', str(image)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return image


@pytest.mark.skipif(sys.implementation.name != 'cpython' or sys.platform not in ('darwin', 'linux'),
                    reason='CPython POSIX SIGINT disposition and ctypes.dlopen audit event')
@pytest.mark.parametrize(('initial', 'final', 'lookup_policy'), [
    (0, 0, 'none'), (1, 1, 'none'), (0, 1, 'none'), (1, 0, 'none'),
    (0, 1, 'callable'), (0, 1, 'ignore'), (1, 0, 'deny'), (1, 1, 'reenter'),
])
def test_loader_preserves_sigint_restart_policy(
    unload_probe: Path, tmp_path: Path, initial: int, final: int, lookup_policy: str,
) -> None:
    """Observe the OS restart flag before, during and after a cold native load.

    An audit hook can also change the flag without changing the Python handler.
    It can replace both during symbol resolution. Honor the mode attached to
    the current caller policy at the time of installing the temporary handler.
    A denied lookup may lose the flags, while reentrant lookup must terminate
    locally without blocking the outer loader.
    """
    source = '''
import ctypes
import os
import signal
import sys
from benchset import _native, _signal_state, run

probe, log, initial, final, lookup = sys.argv[1:]
initial, final = int(initial), int(final)
os.environ.update(BENCHSET_LOADER_LOG=log, BENCHSET_LOADER_INTERRUPT='0')
library = ctypes.CDLL(probe)
restart = library.sigint_restart
library.sigaction_size.restype = ctypes.c_size_t
library.sigaction_alignment.restype = ctypes.c_size_t
resolve = _signal_state._native_sigaction
def checked_snapshot():
    native = resolve()
    if native is None:
        return None
    def checked(signum, action, previous):
        pointer = previous if action is None else action
        assert ctypes.sizeof(pointer._obj) >= library.sigaction_size()
        assert ctypes.alignment(pointer._obj) >= library.sigaction_alignment()
        return native(signum, action, previous)
    return checked
_native._native_sigaction = checked_snapshot
seen = []
requests = []

def original(_signum, _frame):
    pass

def replacement(_signum, _frame):
    pass

policy = signal.SIG_IGN if lookup == 'ignore' else replacement

def audit(event, args):
    if lookup != 'none' and event == 'ctypes.dlsym' and args[1] == 'sigaction':
        if lookup == 'deny':
            requests.append('denied')
            raise PermissionError('sigaction not allowed by host')
        if lookup == 'reenter':
            requests.append(run('single_core', warmup_seconds=-1).reason)
        else:
            signal.signal(signal.SIGINT, policy)
            signal.siginterrupt(signal.SIGINT, False)
    if event == 'ctypes.dlopen':
        seen.append(restart())
        signal.siginterrupt(signal.SIGINT, not final)

signal.signal(signal.SIGINT, original)
signal.siginterrupt(signal.SIGINT, not initial)
assert restart() == initial
sys.setrecursionlimit(100)
sys.addaudithook(audit)
assert run('single_core', warmup_seconds=-1).status == 'failed'
expected_requests = {
    'deny': ['denied'], 'reenter': ['reentrant native SIGINT disposition lookup'],
}
assert requests == expected_requests.get(lookup, []), requests
expected_during = {'deny': 0, 'callable': 1, 'ignore': 1}.get(lookup, initial)
assert seen == [expected_during], seen
assert restart() == final
assert signal.getsignal(signal.SIGINT) is (policy if lookup in ('callable', 'ignore') else original)
'''
    result = subprocess.run([sys.executable, '-c', source, str(unload_probe), str(tmp_path / 'events'),
                             str(initial), str(final), lookup_policy], cwd=_ROOT, capture_output=True, text=True,
                            timeout=15, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(sys.platform not in ('darwin', 'linux'), reason='POSIX SA_RESTART')
def test_publication_preserves_sigint_restart_policy(unload_probe: Path, tmp_path: Path) -> None:
    """The build publisher must preserve the caller's native restart mode too."""
    source = '''
import ctypes
import os
import signal
import sys
from pathlib import Path
from scripts.cargo import publish

probe, temporary = sys.argv[1:]
directory = Path(temporary)
os.environ.update(BENCHSET_LOADER_LOG=str(directory / 'events'), BENCHSET_LOADER_INTERRUPT='0')
library = ctypes.CDLL(probe)
image = directory / 'benchset.so'
image.write_bytes(b'compiled image')
signal.signal(signal.SIGINT, signal.default_int_handler)
signal.siginterrupt(signal.SIGINT, False)
assert library.sigint_restart() == 1
publish(directory / 'package', image, b'metadata')
assert library.sigint_restart() == 1
assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
'''
    result = subprocess.run([sys.executable, '-c', source, str(unload_probe), str(tmp_path)], cwd=_ROOT,
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_sigaction_configuration_permission_error_aborts_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a permission denial during symbol lookup permits loading without flags.

    Interrupt configuration after lookup succeeded. This models a caller
    signal raising PermissionError at that point and must preserve its error.
    """
    failure = PermissionError('caller interrupted symbol configuration')

    class ResolvedAction:
        @property
        def argtypes(self) -> object:
            return None

        @argtypes.setter
        def argtypes(self, _value: object) -> None:
            raise failure

    def unexpected_load(_path: str) -> None:
        pytest.fail('loading must stop after caller interruption')

    _signal_state._sigaction.cache_clear()
    monkeypatch.setattr(_signal_state, 'sys', SimpleNamespace(platform='linux', modules=sys.modules))
    monkeypatch.setattr(ctypes, 'pythonapi', {'sigaction': ResolvedAction()}, raising=False)
    monkeypatch.setattr(_native, '_shared_library', unexpected_load)
    with pytest.raises(PermissionError, match='caller interrupted symbol configuration') as caught:
        _native._load_library('interrupted-resolution-probe')
    assert caught.value is failure


@pytest.mark.parametrize('mode', [
    'available', 'process-image', 'partial-ctypes', 'denied', 'missing-ctypes', 'missing-extension', 'broken-extension', 'import-signal',
    'recursive', 'other-platform',
])
def test_optional_native_snapshot_initialization(  # noqa: PLR0915 — Keep the optional import and symbol owner in one fixture.
    monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    """Snapshot setup is optional, cached and reentrancy-safe on every host platform.

    Synthetic symbols exercise portability and audit refusals without requiring
    the host libc to export POSIX functions.
    """
    action = SimpleNamespace()
    events: List[object] = []
    recursive = mode == 'recursive'
    failure = ImportError('application signal interrupted the optional import')

    class Process:
        def __getitem__(self, name: str) -> object:
            nonlocal recursive
            assert name == 'sigaction'
            events.append(name)
            if mode == 'denied':
                raise PermissionError('audit denial')
            if recursive:
                recursive = False
                return _signal_state._sigaction()
            return action

    def process(handle: object) -> Process:
        events.append(handle)
        return Process()

    original_import = builtins.__import__

    def import_module(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == 'ctypes':
            if mode == 'import-signal':
                signal.raise_signal(signal.SIGINT)
            if mode == 'broken-extension':
                raise ImportError('libffi is unavailable', name='_ctypes')
            raise ModuleNotFoundError('optional module is unavailable',
                                      name='ctypes' if mode == 'missing-ctypes' else '_ctypes')
        return original_import(name, *args, **kwargs)

    def interrupt(_signum: int, _frame: Optional[FrameType]) -> None:
        raise failure

    if mode in ('missing-ctypes', 'missing-extension', 'broken-extension', 'import-signal'):
        monkeypatch.setattr(builtins, '__import__', import_module)
    monkeypatch.setattr(_signal_state, 'sys', SimpleNamespace(platform='win32' if mode == 'other-platform' else 'linux'))
    monkeypatch.setattr(ctypes, 'pythonapi', None if mode in ('process-image', 'partial-ctypes') else Process(), raising=False)
    monkeypatch.setattr(ctypes, 'CDLL', process)
    if mode == 'partial-ctypes':
        monkeypatch.delattr(ctypes, 'CDLL')
    _signal_state._sigaction.cache_clear()
    previous = signal.signal(signal.SIGINT, interrupt if mode == 'import-signal' else signal.default_int_handler)
    try:
        if mode == 'import-signal':
            with pytest.raises(ImportError) as caught:
                _signal_state._native_sigaction()
            assert caught.value is failure
            assert not events
        elif mode == 'recursive':
            with pytest.raises(RuntimeError, match='reentrant native SIGINT disposition lookup'):
                _signal_state._native_sigaction()
            assert _signal_state._native_sigaction() is action
            assert events == ['sigaction', 'sigaction']
        elif mode in ('available', 'process-image'):
            assert _signal_state._native_sigaction() is action
            assert _signal_state._native_sigaction() is action
            assert action.argtypes == [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
            assert action.restype is ctypes.c_int
            assert events == ([None] if mode == 'process-image' else []) + ['sigaction']
        else:
            assert _signal_state._native_sigaction() is None
            assert events == (['sigaction'] if mode == 'denied' else [])
    finally:
        signal.signal(signal.SIGINT, previous)
        _signal_state._sigaction.cache_clear()


def test_snapshot_lookup_propagates_a_recognizable_callback_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal-shaped caller PermissionError must escape optional native lookup on every OS.

    Supply the real handler argument pair through a fake process symbol table.
    This exercises lookup's callback classification without requiring sigaction
    or delivering an OS signal, including on Windows.
    """
    failure = PermissionError('caller interrupted snapshot lookup')
    requests: List[str] = []

    def interrupt(_signum: int, _frame: FrameType) -> None:
        raise failure

    class Process:
        def __getitem__(self, name: str) -> object:
            requests.append(name)
            interrupt(int(signal.SIGINT), sys._getframe())
            pytest.fail('the caller interruption must escape the symbol table')

    monkeypatch.setattr(_signal_state, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(ctypes, 'pythonapi', Process(), raising=False)
    _signal_state._sigaction.cache_clear()
    try:
        with pytest.raises(PermissionError, match='caller interrupted snapshot lookup') as caught:
            _signal_state._native_sigaction()
        assert caught.value is failure
        assert requests == ['sigaction']
        assert _signal_state._sigaction.cache_info().currsize == 0
    finally:
        _signal_state._sigaction.cache_clear()


@pytest.mark.parametrize('signature', ['fixed', 'variadic', 'not-signal'])
def test_native_lookup_error_origin_is_checked_without_requiring_posix(
    signature: str,
) -> None:
    """Recognize the real signal argument pair and tolerate missing or forbidden frame data.

    Live SIGINT exercises both argument layouts. An ordinary function with
    modified locals supplies no trustworthy signal origin.
    """
    failure = PermissionError('host policy')

    def fixed(_signum: int, _frame: Optional[FrameType]) -> None:
        raise failure

    def variadic(*arguments: object, key: object = None) -> None:  # noqa: ARG001 — Signature layout is under test.
        if signature == 'not-signal':
            arguments = None  # type: ignore[assignment]  # noqa: F841 — Model changed frame locals.
        raise failure

    assert not _signal_state._from_callback(None)
    previous = signal.signal(signal.SIGINT, fixed if signature == 'fixed' else variadic)

    def invoke() -> None:
        if signature == 'not-signal':
            variadic()
        else:
            signal.raise_signal(signal.SIGINT)

    try:
        with pytest.raises(PermissionError) as caught:
            invoke()
        assert caught.value is failure
        assert _signal_state._from_callback(caught.value.__traceback__) == (signature in ('fixed', 'variadic'))
    finally:
        signal.signal(signal.SIGINT, previous)


@pytest.mark.skipif(sys.implementation.name != 'cpython', reason='f_code access auditing is CPython-specific')
def test_callback_origin_tolerates_a_real_frame_inspection_veto() -> None:
    """An audit refusal of actual frame inspection leaves optional origin detection unavailable."""
    source = '''
import sys
from benchset import _signal_state

def host(*args):
    raise PermissionError('host callback')

try:
    host('open', ())
except PermissionError as error:
    failure = error

def audit(event, args):
    if event == 'object.__getattr__' and args[1] == 'f_code':
        raise PermissionError('frame inspection forbidden')

sys.addaudithook(audit)
assert not _signal_state._from_callback(failure.__traceback__, include_audit=True)
'''
    result = subprocess.run([sys.executable, '-c', source], cwd=_ROOT,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('callback', ['audit', 'trace'])
def test_callback_origin_keeps_optional_audit_refusals_distinct(callback: str) -> None:
    """Filesystem vetoes propagate, while an optional native lookup can still decline an audited symbol.

    Supply the real callback argument layouts without OS-specific native calls.
    The subprocess tests separately exercise registered audit and tracing hooks.
    """
    failure = PermissionError('host callback')

    def host(*_arguments: object) -> None:
        raise failure

    def invoke() -> None:
        if callback == 'audit':
            host('open', ())
        else:
            host(sys._getframe(), 'line', None)

    with pytest.raises(PermissionError) as caught:
        invoke()
    assert caught.value is failure
    assert _signal_state._from_callback(caught.value.__traceback__, include_audit=True)
    assert _signal_state._from_callback(caught.value.__traceback__) == (callback != 'audit')


@pytest.mark.parametrize('failure', ['read', 'restore'])
def test_native_snapshot_errors_preserve_signal_ownership(failure: str) -> None:
    """A failed native snapshot never silently loses flags or leaks a temporary handler."""
    calls: List[str] = []

    def native(signum: int, action: object, _previous: object) -> int:
        assert signum == signal.SIGINT
        operation = 'read' if action is None else 'restore'
        calls.append(operation)
        return -1 if operation == failure else 0

    def handler(_signum: int, _frame: Optional[FrameType]) -> None:
        pass

    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(OSError, match=r'cannot .* the native SIGINT disposition'):
            _signal_state._set_signal(signal.SIGINT, handler, native)
        assert calls == (['read'] if failure == 'read' else ['read', 'restore'])
        assert signal.getsignal(signal.SIGINT) is (signal.default_int_handler if failure == 'read' else handler)
    finally:
        signal.signal(signal.SIGINT, previous)


@pytest.mark.skipif(sys.platform not in ('darwin', 'linux') or not hasattr(signal, 'SIGUSR1'),
                    reason='POSIX native snapshot and a real competing signal')
@pytest.mark.parametrize('replacement', ['default', 'ignore', 'callable'])
def test_native_snapshot_reconciles_a_policy_changed_during_restore(replacement: str) -> None:
    """A competing handler must not leave Python's registry and the OS disposition inconsistent.

    A trace places SIGUSR1 after the last ownership check, immediately before
    libc restores the snapshot. A later SIGINT must use the selected policy.
    """
    source = '''
import inspect
import signal
import sys
from benchset import _native, _signal_state

mode = sys.argv[1]
events = []
def original(signum, frame):
    raise AssertionError('the old policy was restored')
def selected(signum, frame):
    events.append(signum)
replacement = {'default': signal.SIG_DFL, 'ignore': signal.SIG_IGN, 'callable': selected}[mode]
signal.signal(signal.SIGINT, original)
signal.signal(signal.SIGUSR1, lambda *_: signal.signal(signal.SIGINT, replacement))
_native._shared_library = lambda _path: object()
code = _signal_state._set_signal.__code__
lines, start = inspect.getsourcelines(_signal_state._set_signal)
boundary = start + next(index for index, line in enumerate(lines) if 'if native(signum, action, None)' in line)
fired = False
def trace(frame, event, value):
    global fired
    if not fired and frame.f_code is code and event == 'line' and frame.f_lineno == boundary:
        fired = True
        signal.raise_signal(signal.SIGUSR1)
    return trace

sys.settrace(trace)
_native._load_library('native-policy-race')
sys.settrace(None)
assert fired
assert signal.getsignal(signal.SIGINT) is replacement
print('ready', flush=True)
signal.raise_signal(signal.SIGINT)
assert mode != 'default', 'SIG_DFL did not terminate the process'
assert events == ([signal.SIGINT] if mode == 'callable' else []), events
'''
    result = subprocess.run([sys.executable, '-c', source, replacement], cwd=_ROOT,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == (-signal.SIGINT if replacement == 'default' else 0), result.stdout + result.stderr
    assert result.stdout == 'ready\n'
    assert not result.stderr


@pytest.mark.parametrize('policy', ['unknown', 'ignore', 'callable'])
def test_snapshot_reconciliation_respects_a_replacement_without_a_second_snapshot(
    monkeypatch: pytest.MonkeyPatch, policy: str,
) -> None:
    """Reinstall a known replacement once, preserving the displaced handler and avoiding recursive snapshots.

    An unknown C-owned policy is represented by None and must never be passed
    to Python's signal installer. Synthetic OS calls exercise this on every platform.
    """
    def temporary(_signum: int, _frame: Optional[FrameType]) -> None:
        pass

    def custom(_signum: int, _frame: Optional[FrameType]) -> None:
        pass

    original = signal.default_int_handler
    registered: Any = original
    replacement: Any = {'unknown': None, 'ignore': int(signal.SIG_IGN), 'callable': custom}[policy]
    installations: List[Any] = []
    snapshots: List[str] = []

    def install(signum: int, handler: Any) -> Any:
        nonlocal registered
        assert signum == signal.SIGINT
        if handler is None:
            registered = original  # Terminate a faulty cleanup retry after the invalid installation.
            raise TypeError('None is not an installable Python signal policy')
        installations.append(handler)
        previous, registered = registered, handler
        return previous

    def native(_signum: int, action: object, _out: object) -> int:
        nonlocal registered
        snapshots.append('read' if action is None else 'restore')
        if action is not None:
            registered = replacement
        return 0

    monkeypatch.setattr(_signal_state, '_signal', SimpleNamespace(getsignal=lambda _signum: registered, signal=install))
    assert _signal_state._set_signal(signal.SIGINT, temporary, native) is original
    assert installations == [temporary] + ([] if policy == 'unknown' else [replacement])
    assert registered is replacement
    assert snapshots == ['read', 'restore']


@pytest.mark.parametrize(('entry', 'read_budget'), [('run', 4), ('load', 1)])
def test_callable_introspection_cannot_trap_signal_restoration(entry: str, read_budget: int) -> None:
    """A valid callable's failing __class__ property must not cause an unbounded cleanup retry."""
    source = '''
import signal
import sys
from benchset import _native, run

reads = 0
budget = int(sys.argv[1])
class Handler:
    @property
    def __class__(self):
        global reads
        reads += 1
        if reads > budget:
            raise RuntimeError('caller disallows further class inspection')
        return type(self)

    def __call__(self, signum, frame):
        pass

handler = Handler()
signal.signal(signal.SIGINT, handler)
reads = 0
if sys.argv[2] == 'load':
    expected = object()
    _native._shared_library = lambda _path: expected
    assert _native._load_library('class-probe') is expected
else:
    result = run('single_core', warmup_seconds=-1)
    assert result.reason == 'warmup_seconds must be finite and nonnegative', result
assert reads <= budget, reads
'''
    result = subprocess.run([sys.executable, '-c', source, str(read_budget), entry], cwd=_ROOT,
                            capture_output=True, text=True, timeout=5, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(sys.implementation.name != 'cpython' or sys.platform not in ('darwin', 'linux'),
                    reason='CPython POSIX symbol audit and SIGINT')
@pytest.mark.parametrize('error_kind', [
    'permission', 'permission-replaced', 'permission-bound', 'permission-callable', 'permission-partial', 'permission-cleared',
    'audit-shared', 'audit-forwarded-frame', 'audit-frame-denied', 'unrelated-handler', 'module',
])
def test_caller_signal_error_during_symbol_lookup_is_not_a_snapshot_refusal(error_kind: str) -> None:
    """Recognizable signal errors propagate while audit refusals can disable the optional snapshot."""
    source = '''
import signal
import sys
from functools import partial
from benchset import run

kind = sys.argv[1]
failure = (ModuleNotFoundError('application signal error', name='_ctypes') if kind == 'module'
           else PermissionError('SIGINT host policy exception'))
events = []

def interrupt(_signum, _frame):
    events.append('handler')
    if kind == 'permission-cleared':
        _signum = _frame = None
    if kind == 'permission-replaced':
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise failure

class Host:
    def interrupt(self, signum, frame):
        if type(signum) is int:
            interrupt(signum, frame)
        elif signum == 'ctypes.dlsym' and frame[1] == 'sigaction':
            events.append('lookup')
            raise failure

    __call__ = interrupt

class Unrelated:
    @property
    def __class__(self):
        raise RuntimeError('unrelated handler must not be inspected')

    def __call__(self, _signum, _frame):
        pass

def prefixed(_prefix, *args, key=None):
    interrupt(*args)

handler = {'permission-bound': Host().interrupt, 'permission-callable': Host(),
           'permission-partial': partial(prefixed, 'prefix'),
           'audit-shared': Host().interrupt}.get(kind, interrupt)

def audit(event, args):
    if kind == 'audit-frame-denied' and event == 'object.__getattr__' and args[1] == 'f_code':
        raise PermissionError('frame introspection forbidden')
    if event == 'ctypes.dlsym' and args[1] == 'sigaction':
        events.append('lookup')
        if kind == 'audit-frame-denied':
            raise failure
        signal.raise_signal(signal.SIGINT)
    if event == 'ctypes.dlopen':
        events.append('library')

def policy(frame, event, args):
    events.append('lookup')
    raise failure

def forwarded_audit(event, args):
    if event == 'ctypes.dlsym' and args[1] == 'sigaction':
        policy(sys._getframe(), event, args)
    elif event == 'ctypes.dlopen':
        events.append('library')

signal.signal(signal.SIGINT, handler)
if kind == 'unrelated-handler':
    signal.signal(signal.SIGUSR1, Unrelated())
hook = Host().interrupt if kind == 'audit-shared' else forwarded_audit if kind == 'audit-forwarded-frame' else audit
sys.addaudithook(hook)
try:
    result = run('single_core', warmup_seconds=-1)
except ModuleNotFoundError as error:
    assert error is failure
else:
    refusal = kind in ('audit-shared', 'audit-forwarded-frame', 'audit-frame-denied', 'permission-cleared')
    expected = 'warmup_seconds must be finite and nonnegative' if refusal else str(failure)
    assert kind != 'module' and result.reason == expected, result
expected_events = {'audit-shared': ['lookup'], 'audit-frame-denied': ['lookup', 'library'],
                   'audit-forwarded-frame': ['lookup', 'library'], 'permission-cleared': ['lookup', 'handler', 'library']}
assert events == expected_events.get(kind, ['lookup', 'handler']), events
assert signal.getsignal(signal.SIGINT) is (signal.SIG_IGN if kind == 'permission-replaced' else handler)
'''
    result = subprocess.run([sys.executable, '-c', source, error_kind], cwd=_ROOT,
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(sys.implementation.name != 'cpython' or sys.platform not in ('darwin', 'linux'),
                    reason='CPython POSIX symbol audit')
def test_profiler_error_during_native_lookup_is_preserved() -> None:
    """A recognizable profiler failure during optional lookup is not an audit refusal."""
    source = '''
import os
import sys
from benchset import run

looking = False
events = []
failure = PermissionError('host profiler interrupted native lookup')
def audit(event, args):
    global looking
    if event == 'ctypes.dlsym' and args[1] == 'sigaction':
        looking = True
        os.stat('.')
def profile(frame, event, value):
    if looking and event == 'c_call' and value is os.stat:
        events.append('profile')
        raise failure
audit.__cantrace__ = True
sys.addaudithook(audit)
sys.setprofile(profile)
result = run('single_core', warmup_seconds=-1)
assert events == ['profile'], events
assert result.reason == str(failure), result
'''
    result = subprocess.run([sys.executable, '-c', source], cwd=_ROOT,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(sys.implementation.name != 'cpython' or sys.platform not in ('darwin', 'linux'),
                    reason='CPython POSIX lookup audit and immediate reference release')
@pytest.mark.parametrize('origin', ['signal', 'audit'])
def test_snapshot_permission_error_does_not_create_a_traceback_cycle(origin: str) -> None:
    """Inspecting a lookup failure must not capture the catcher's live error in frame locals."""
    source = '''
import gc
import signal
import sys
import weakref
from benchset import run

references = []
origin = sys.argv[1]
class Failure(PermissionError):
    pass

def create_failure():
    error = Failure('transient host denial')
    references.append(weakref.ref(error))
    return error

def interrupt(signum, frame):
    raise create_failure()

def audit(event, args):
    if event == 'ctypes.dlsym' and args[1] == 'sigaction':
        if origin == 'audit':
            raise create_failure()
        signal.raise_signal(signal.SIGINT)

signal.signal(signal.SIGINT, interrupt)
sys.addaudithook(audit)
gc.disable()
for _ in range(3):
    result = run('single_core', warmup_seconds=-1)
    expected = 'transient host denial' if origin == 'signal' else 'warmup_seconds must be finite and nonnegative'
    assert result.reason == expected, result
    assert references and all(reference() is None for reference in references), 'lookup retained its failure'
'''
    result = subprocess.run([sys.executable, '-c', source, origin], cwd=_ROOT,
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(sys.implementation.name != 'cpython' or sys.platform not in ('darwin', 'linux'),
                    reason='POSIX fork during CPython ctypes symbol audit')
@pytest.mark.parametrize('forking_thread', ['lookup', 'other'])
def test_fork_does_not_inherit_sigaction_recursion(forking_thread: str) -> None:
    """A fork during lookup leaves the child free to load its own native image.

    Fork from the auditing thread itself or hold it in the hook while a worker
    forks. The child's surviving thread must be able to load on both calls.
    """
    source = '''
import os
import signal
import sys
from threading import Event, Thread
from benchset import run

entered, complete = Event(), Event()
statuses = []
reason = 'warmup_seconds must be finite and nonnegative'
fork_in_lookup = sys.argv[1] == 'lookup'

def audit(event, args):
    if event == 'ctypes.dlsym' and args[1] == 'sigaction' and not entered.is_set():
        entered.set()
        if fork_in_lookup:
            worker()
        assert complete.wait(5), 'fork did not finish'

def worker():
    assert entered.wait(5), 'symbol lookup did not start'
    child = os.fork()
    if child == 0:
        try:
            reasons = [run('single_core', warmup_seconds=-1).reason for _ in range(2)]
            assert reasons == [reason, reason], reasons
        except BaseException:
            import traceback
            traceback.print_exc()
            os._exit(1)
        os._exit(0)
    statuses.append(os.waitpid(child, 0)[1])
    complete.set()

signal.signal(signal.SIGINT, lambda *_: None)
sys.addaudithook(audit)
thread = Thread(target=worker, daemon=True)
if not fork_in_lookup:
    thread.start()
assert run('single_core', warmup_seconds=-1).reason == reason
if not fork_in_lookup:
    thread.join(6)
    assert not thread.is_alive()
assert statuses == [0], statuses
'''
    result = subprocess.run([sys.executable, '-c', source, forking_thread], cwd=_ROOT,
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


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
