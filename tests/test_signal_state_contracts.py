"""Preserve error lifetimes and optional native lookup boundaries during refactoring."""

from __future__ import annotations

import builtins
import ctypes
import gc
import signal
import subprocess
import sys
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Tuple, Type

import pytest

from benchset import _signal_state


def _restore_after_interruption(
    monkeypatch: pytest.MonkeyPatch, *, refuse: bool,
) -> Tuple[List[Any], List[BaseException], int]:
    """Model one caller interruption followed by success or a direct OS refusal."""
    owner = signal.default_int_handler
    references: List[Any] = []
    captured: List[BaseException] = []
    attempts = 0

    class HostError(RuntimeError):
        pass

    def create_error() -> HostError:
        error = HostError('caller interrupted restoration')
        references.append(weakref.ref(error))
        return error

    def interrupt() -> None:
        raise create_error()

    def setter(_signum: int, _handler: Any, _native: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            interrupt()
        if refuse:
            raise OSError('native restoration refused')
        return owner

    monkeypatch.setattr(_signal_state, '_get_signal', lambda _signum: owner)
    try:
        _signal_state._restore_sigint(owner, owner, None, setter)
    except (HostError, OSError) as error:
        captured.append(error)
    return references, captured, attempts


@pytest.mark.skipif(sys.implementation.name != 'cpython', reason='Immediate reference release is a CPython contract')
def test_successful_restoration_does_not_retain_its_interruption(monkeypatch: pytest.MonkeyPatch) -> None:
    """A restored caller error must disappear when its owner releases it, even without cyclic GC.

    The factory avoids storing the error in the raising frame. Keeping a
    mutable error queue populated during rethrow would create a new cycle.
    """
    collecting = gc.isenabled()
    gc.disable()
    captured: List[BaseException] = []
    try:
        references, captured, attempts = _restore_after_interruption(monkeypatch, refuse=False)
        assert attempts == 2
        assert len(captured) == 1
        same_error = references[0]() is captured[0]
        assert same_error
        captured.clear()
        released = references[0]() is None
        assert released
    finally:
        captured.clear()
        if collecting:
            gc.enable()


@pytest.mark.skipif(sys.implementation.name != 'cpython', reason='Immediate reference release is a CPython contract')
def test_native_restoration_refusal_releases_an_earlier_interruption(monkeypatch: pytest.MonkeyPatch) -> None:
    """A direct OS failure supersedes a caller interruption without retaining the earlier error.

    Keep the OS error and its traceback alive while checking the first error.
    This detects stale saved errors in the restoration frame independently of GC.
    """
    collecting = gc.isenabled()
    gc.disable()
    captured: List[BaseException] = []
    try:
        references, captured, attempts = _restore_after_interruption(monkeypatch, refuse=True)
        assert attempts == 2
        assert len(captured) == 1
        assert type(captured[0]) is OSError
        assert str(captured[0]) == 'native restoration refused'
        released_while_refusal_held = references[0]() is None
        assert released_while_refusal_held
    finally:
        captured.clear()
        if collecting:
            gc.enable()


@pytest.mark.parametrize('error_type', [ImportError, ModuleNotFoundError])
def test_optional_import_failure_and_symbol_lookup_failure_remain_distinct(
    monkeypatch: pytest.MonkeyPatch, error_type: Type[ImportError],
) -> None:
    """Missing ctypes permits a snapshot fallback, while a host ImportError during lookup propagates.

    The error class and module name are identical at both boundaries, so
    merging the import and symbol-lookup exception handlers loses this contract.
    """
    unavailable = error_type('optional extension is unavailable', name='_ctypes')
    veto = error_type('host rejected symbol lookup', name='_ctypes')
    original_import = builtins.__import__

    def import_module(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == 'ctypes':
            raise unavailable
        return original_import(name, *args, **kwargs)

    class Process:
        def __getitem__(self, _name: str) -> object:
            raise veto

    monkeypatch.setattr(_signal_state, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(ctypes, 'pythonapi', Process(), raising=False)
    _signal_state._sigaction.cache_clear()
    try:
        with monkeypatch.context() as imports:
            imports.setattr(builtins, '__import__', import_module)
            assert _signal_state._native_sigaction() is None
        with pytest.raises(error_type, match='host rejected symbol lookup') as caught:
            _signal_state._native_sigaction()
        assert caught.value is veto
    finally:
        _signal_state._sigaction.cache_clear()


def test_optional_snapshot_retries_a_refusal_and_caches_only_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A temporary symbol refusal must not disable future snapshots, while a resolved symbol is reused."""
    allowed = False
    events: List[str] = []
    action = SimpleNamespace()

    class Process:
        def __getitem__(self, name: str) -> object:
            events.append(name)
            if not allowed:
                raise PermissionError('temporary symbol refusal')
            return action

    monkeypatch.setattr(_signal_state, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(ctypes, 'pythonapi', Process(), raising=False)
    _signal_state._sigaction.cache_clear()
    try:
        assert _signal_state._native_sigaction() is None
        allowed = True
        assert _signal_state._native_sigaction() is action
        assert _signal_state._native_sigaction() is action
        assert events == ['sigaction', 'sigaction']
    finally:
        _signal_state._sigaction.cache_clear()


@pytest.mark.parametrize('identifiable', [False, True])
def test_snapshot_import_fallback_keeps_permission_error_origin_policy(
    monkeypatch: pytest.MonkeyPatch, identifiable: bool,
) -> None:
    """Preserve callback classification while handling an unavailable ctypes import.

    Deliver a real SIGINT on the first line after ImportError. The tracer
    clears its invocation so only the signal handler can identify the error.
    An identifiable handler's error propagates; erasing its arguments retains
    the documented PermissionError fallback. This guards the outer handler's
    coverage of the import-error handler itself, without fixing line numbers.
    """
    rejected = False
    delivered: List[bool] = []
    failure = PermissionError('caller interrupted optional import fallback')
    original_import = builtins.__import__

    def import_module(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == 'ctypes':
            raise ImportError('optional ctypes is unavailable')
        return original_import(name, *args, **kwargs)

    def handler(*invocation: Any) -> None:
        if not identifiable:
            del invocation
        raise failure

    def trace(*invocation: Any) -> Any:
        nonlocal rejected
        if invocation[0].f_code.co_name == '_sigaction':
            if invocation[1] == 'exception' and issubclass(invocation[2][0], ImportError):
                rejected = True
            elif rejected and invocation[1] == 'line' and not delivered:
                del invocation
                delivered.append(True)
                signal.raise_signal(signal.SIGINT)
        return trace

    monkeypatch.setattr(_signal_state, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(builtins, '__import__', import_module)
    previous = signal.signal(signal.SIGINT, handler)
    tracing = sys.gettrace()
    _signal_state._sigaction.cache_clear()
    try:
        sys.settrace(trace)
        if identifiable:
            with pytest.raises(PermissionError) as caught:
                _signal_state._native_sigaction()
            assert caught.value is failure
        else:
            assert _signal_state._native_sigaction() is None
        assert delivered == [True]
    finally:
        sys.settrace(tracing)
        signal.signal(signal.SIGINT, previous)
        _signal_state._sigaction.cache_clear()


def test_traceback_extraction_preserves_a_caller_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SIGINT during native traceback extraction must retain its exact error.

    Trace the descriptor operation wherever the classifier owns it, rather
    than a fixed line number. Its caller's PermissionError is distinct from
    an audit refusal while inspecting frames and must escape that fallback.
    """
    source = Path(_signal_state.__file__).read_text().splitlines()
    extraction_lines = {
        number for number, line in enumerate(source, 1)
        if "BaseException.__dict__['__traceback__'].__get__(error)" in line
    }
    assert extraction_lines
    failure = PermissionError('caller interrupted native traceback extraction')
    delivered: List[bool] = []

    class Process:
        def __getitem__(self, _name: str) -> object:
            raise PermissionError('optional sigaction lookup was refused')

    def handler(_signum: int, _frame: Any) -> None:
        raise failure

    def trace(frame: Any, event: str, _argument: Any) -> Any:
        if (not delivered and event == 'line' and frame.f_code.co_filename == _signal_state.__file__
                and frame.f_lineno in extraction_lines and frame.f_code.co_name in ('_sigaction', '_from_callback')):
            delivered.append(True)
            signal.raise_signal(signal.SIGINT)
        return trace

    monkeypatch.setattr(_signal_state, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(ctypes, 'pythonapi', Process(), raising=False)
    previous = signal.signal(signal.SIGINT, handler)
    tracing = sys.gettrace()
    _signal_state._sigaction.cache_clear()
    try:
        sys.settrace(trace)
        with pytest.raises(PermissionError) as caught:
            _signal_state._native_sigaction()
        assert caught.value is failure
        assert delivered == [True]
    finally:
        sys.settrace(tracing)
        signal.signal(signal.SIGINT, previous)
        _signal_state._sigaction.cache_clear()


@pytest.mark.skipif(sys.implementation.name != 'cpython', reason='Immediate reference release and f_code auditing are CPython contracts')
def test_retained_inspection_error_does_not_retain_the_snapshot_error() -> None:
    """A host may retain its frame-inspection refusal without retaining a discarded lookup error.

    The subprocess installs a real audit veto and disables cyclic GC. After
    clearing the host error's context, only its traceback remains: the
    classifier must not leave its input error in that retained frame.
    Factories keep the raised errors out of their own raising frames.
    """
    source = '''import ctypes, gc, sys, weakref
from benchset import _signal_state
references, retained = [], []
class LookupError(PermissionError):
    pass
def lookup_error():
    error = LookupError('optional snapshot refused')
    references.append(weakref.ref(error))
    return error
class Process:
    def __getitem__(self, _name):
        raise lookup_error()
def inspection_error():
    error = PermissionError('host forbids frame inspection')
    retained.append(error)
    return error
def audit(event, args):
    if event == 'object.__getattr__' and args[1] == 'f_code':
        raise inspection_error()
ctypes.pythonapi = Process()
_signal_state.sys = type('Platform', (), {'platform': 'linux'})
_signal_state._sigaction.cache_clear()
sys.addaudithook(audit)
gc.disable()
assert _signal_state._native_sigaction() is None
assert len(references) == len(retained) == 1
retained[0].__context__ = None
assert references[0]() is None, 'retained classifier frame still owns the discarded lookup error'
'''
    result = subprocess.run([sys.executable, '-c', source], cwd=Path(_signal_state.__file__).resolve().parents[1],
                            capture_output=True, text=True, check=False, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(
    sys.implementation.name != 'cpython' or sys.version_info < (3, 11),
    reason='Immediate release at tracing entry requires CPython 3.11 or later',
)
def test_retained_entry_error_does_not_retain_the_snapshot_error() -> None:
    """A tracing refusal at classifier entry must not retain the discarded lookup error.

    The call event precedes the classifier's first instruction, so clearing
    an error argument in its body cannot protect this boundary. Retain the
    host exception's traceback, clear its context, and check the lookup error
    is released with cyclic GC disabled. CPython 3.8 through 3.10 also retains handled
    exception state in these traceback frames in the original implementation,
    so immediate release at this boundary is asserted only from 3.11 onward.
    """
    source = '''import ctypes, gc, sys, weakref
from benchset import _signal_state
references, retained = [], []
class LookupError(PermissionError):
    pass
def lookup_error():
    error = LookupError('optional snapshot refused')
    references.append(weakref.ref(error))
    return error
class Process:
    def __getitem__(self, _name):
        raise lookup_error()
def entry_error():
    error = RuntimeError('host refuses classifier entry')
    retained.append(error)
    return error
def trace(frame, event, _argument):
    if event == 'call' and frame.f_code is _signal_state._from_callback.__code__:
        raise entry_error()
    return trace
ctypes.pythonapi = Process()
_signal_state.sys = type('Platform', (), {'platform': 'linux'})
_signal_state._sigaction.cache_clear()
gc.disable()
sys.settrace(trace)
try:
    _signal_state._native_sigaction()
except RuntimeError as error:
    assert error is retained[0]
else:
    raise AssertionError('host tracing refusal was swallowed')
finally:
    sys.settrace(None)
assert len(references) == len(retained) == 1
retained[0].__context__ = None
assert references[0]() is None, 'classifier entry still owns the discarded lookup error'
'''
    result = subprocess.run([sys.executable, '-c', source], cwd=Path(_signal_state.__file__).resolve().parents[1],
                            capture_output=True, text=True, check=False, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
