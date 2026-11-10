"""Process SIGINT policy shared by the Python loader and source publisher."""

import _signal  # type: ignore[import-not-found]  # The low-level stdlib signal setter.
import signal
import sys
from functools import lru_cache
from os import getpid
from threading import local
from types import FrameType, TracebackType
from typing import Any, Callable, List, NoReturn, Optional, Union, cast

_SignalHandler = Union[Callable[[int, Optional[FrameType]], None], int]
_NativeSigaction = Callable[[int, Optional[object], Optional[object]], int]


_sigaction_resolution = local()


class _SigactionUnavailableError(Exception):
    """The optional native snapshot could not be initialized."""


def _from_callback(trace: Optional[TracebackType], *, include_audit: bool = False) -> bool:
    # Signal and tracing callbacks receive their originating frame. Audit
    # callbacks receive (event, args). Inspect the actual invocation so
    # callbacks and ordinary wrappers can share the same Python code.
    # Skip our own except frame. Its cached locals would retain the error
    # after the exception variable is cleared.
    try:
        trace = trace.tb_next if trace is not None else None
        while trace is not None:
            frame = trace.tb_frame
            code, variables = frame.f_code, frame.f_locals
            arguments = tuple(variables.get(name) for name in code.co_varnames[:code.co_argcount])
            if code.co_flags & 0x04:  # CO_VARARGS, without importing a potentially half-initialized inspect module.
                rest = variables.get(code.co_varnames[code.co_argcount + code.co_kwonlyargcount])
                if type(rest) is tuple:
                    arguments += rest
            caller = frame.f_back
            if caller is not None and any(
                (type(value) is int and other is caller) or (
                    value is caller and type(other) is str and other in (
                        'call', 'return', 'exception', 'line', 'opcode', 'c_call', 'c_return', 'c_exception',
                    )
                )
                for value, other in zip(arguments, arguments[1:])
            ):
                return True
            if include_audit and any(type(value) is str and type(other) is tuple
                                     for value, other in zip(arguments, arguments[1:])):
                return True
            trace = trace.tb_next
    except PermissionError:
        pass  # Auditing can deny frame inspection as well as the symbol.
    return False


@lru_cache(maxsize=1)
def _sigaction() -> _NativeSigaction:
    pid = getpid()
    if getattr(_sigaction_resolution, 'pid', None) == pid:
        raise RuntimeError('reentrant native SIGINT disposition lookup')
    try:
        _sigaction_resolution.pid = pid
        try:
            try:
                import ctypes  # noqa: PLC0415 — Native snapshots are optional in the source publisher.
            except ImportError as error:
                if _from_callback(BaseException.__dict__['__traceback__'].__get__(error)):
                    raise
                raise _SigactionUnavailableError from None

            process = getattr(ctypes, 'pythonapi', None)
            if process is None:
                factory = getattr(ctypes, 'CDLL', None)
                if factory is None:
                    raise _SigactionUnavailableError
                process = factory(None)
            function = process['sigaction']
        except PermissionError as error:
            if _from_callback(BaseException.__dict__['__traceback__'].__get__(error)):
                raise
            raise _SigactionUnavailableError from None
        function.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
        function.restype = ctypes.c_int
        return cast(_NativeSigaction, function)
    finally:
        _sigaction_resolution.pid = None


def _native_sigaction() -> Optional[_NativeSigaction]:
    if sys.platform in ('darwin', 'linux'):
        try:
            return _sigaction()
        except _SigactionUnavailableError:
            pass
    return None


def _get_signal(signum: int) -> Optional[_SignalHandler]:
    handler: Optional[_SignalHandler] = _signal.getsignal(signum)
    return signal.Handlers(handler) if type(handler) is int else handler


def _set_signal(signum: int, handler: _SignalHandler, native: Optional[_NativeSigaction]) -> Optional[_SignalHandler]:
    current = _get_signal(signum)
    previous = current
    action = None
    if native is not None and callable(current) and callable(handler):
        import ctypes  # noqa: PLC0415 — The native symbol has already initialized ctypes.

        # Aligned opaque storage for Darwin and Linux libc sigaction structures.
        action = ctypes.byref((ctypes.c_uint64 * 32)())
        if native(signum, None, action) != 0:
            raise OSError('cannot read the native SIGINT disposition')
    try:
        # The handler is already registered or ours. Avoid signal.signal's
        # int conversion, which can execute user code on a callable object.
        selected = int(handler) if type(handler) is signal.Handlers else handler
        previous = _signal.signal(int(signum), selected)
    finally:
        # Python callables share the same native trampoline. Reapply the
        # caller's OS flags and mask while keeping the new Python callable.
        if native is not None and action is not None and previous is current and _get_signal(signum) is handler:
            if native(signum, action, None) != 0:
                raise OSError('cannot restore the native SIGINT disposition')
            replacement = _get_signal(signum)
            if replacement is not None and replacement is not handler:
                _restore_sigint(replacement, replacement, None)
    return signal.Handlers(previous) if type(previous) is int else previous


def _direct_error(error: BaseException, operation: Callable[..., Any]) -> bool:
    # A caller signal adds a frame outside the registration call.
    trace = BaseException.__dict__['__traceback__'].__get__(error)
    called = trace.tb_next if trace is not None else None
    try:
        return called is None or (called.tb_next is None and
                                  called.tb_frame.f_code is getattr(operation, '__code__', None))
    except PermissionError:
        return True  # Only a direct operation's frame remained before inspection was denied.


def _restore_sigint(
    owner: _SignalHandler, target: _SignalHandler, native: Optional[_NativeSigaction],
    setter: Callable[[int, _SignalHandler, Optional[_NativeSigaction]], Optional[_SignalHandler]] = _set_signal,
) -> None:
    interruptions: List[BaseException] = []
    while True:
        try:
            if _get_signal(signal.SIGINT) is not owner:
                break
            displaced = setter(signal.SIGINT, target, native)
        except BaseException as error:
            if issubclass(type(error), (OSError, TypeError, ValueError)) and _direct_error(error, setter):
                interruptions.clear()
                raise
            if not interruptions:
                interruptions.append(error)
            continue
        if displaced is owner or displaced is None:
            break
        # A caller changed the handler just before the swap. Give it back.
        owner, target = target, displaced
    if interruptions:
        _raise_original(interruptions.pop())


def _raise_original(error: BaseException) -> NoReturn:
    """Preserve host context without retaining the exception in its own traceback."""
    descriptor = BaseException.__dict__['__context__']
    context = descriptor.__get__(error)
    try:
        raise error
    finally:
        descriptor.__set__(error, context)
        del error
