"""Publication locks keep descriptors owned across ordinary and signal errors."""

from __future__ import annotations

import builtins
import errno
import importlib.util
import io
import os
import signal
import subprocess
import sys
import weakref
from functools import wraps
from pathlib import Path
from threading import Thread
from types import FrameType, SimpleNamespace
from typing import Any

import pytest
from filelock import FileLock, Timeout

from scripts import cargo

_OWNERS: weakref.WeakValueDictionary[int, Any] = weakref.WeakValueDictionary()


@pytest.fixture(params=['nested', 'callable', 'mock', 'dispatcher'])
def _stat_proxy(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Instrument real stat calls with standard function, callable and Mock proxies."""
    from unittest.mock import Mock  # noqa: PLC0415

    original = os.stat

    @wraps(original)
    def inner(*args: Any, **kwargs: Any) -> os.stat_result:
        return original(*args, **kwargs)

    @wraps(inner)
    def outer(*args: Any, **kwargs: Any) -> os.stat_result:
        return inner(*args, **kwargs)

    class Proxy:
        def __call__(self, *args: Any, **kwargs: Any) -> os.stat_result:
            return original(*args, **kwargs)

    def dispatch(operation: str, arguments: tuple, keywords: dict) -> os.stat_result:
        assert operation == 'stat'
        return original(*arguments, **keywords)

    def dispatched(*args: Any, **kwargs: Any) -> os.stat_result:
        return dispatch('stat', args, kwargs)

    monkeypatch.setattr(os, 'stat', {
        'nested': outer, 'callable': Proxy(), 'mock': Mock(wraps=original), 'dispatcher': dispatched,
    }[request.param])


@pytest.fixture(autouse=True)
def _observe_owned_lock_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failing mutations close file objects, avoiding later closes of reused fds."""
    original = builtins.open

    def opened(path: Any, *args: Any, **kwargs: Any) -> Any:
        file = original(path, *args, **kwargs)
        if isinstance(path, Path) and path.name == '.benchset.publish.lock':
            _OWNERS[file.fileno()] = file
        return file

    monkeypatch.setattr(builtins, 'open', opened)


def _products(tmp_path: Path) -> tuple[Path, Path]:
    package = tmp_path / 'package'
    native = package / '_native'
    native.mkdir(parents=True)
    (native / 'libbenchset.so').write_bytes(b'old image')
    (package / '_metadata.json').write_bytes(b'old metadata')
    image = tmp_path / 'libbenchset.so'
    image.write_bytes(b'new image')
    return package, image


def _open(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
    except OSError:
        return False
    return True


def _close_orphaned(descriptors: list[int], lock: Path) -> None:
    """Clean failing reproductions without closing descriptors reused elsewhere."""
    for descriptor in descriptors:
        try:
            actual, expected = os.fstat(descriptor), lock.stat()
            if (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino):
                owner = _OWNERS.get(descriptor)
                if owner is None or owner.closed:
                    os.close(descriptor)
                else:
                    owner.close()
        except OSError:
            pass


@pytest.mark.skipif(os.name != 'posix', reason='Exercise real POSIX flock handoffs')
@pytest.mark.parametrize('when', ['before', 'after'])
@pytest.mark.parametrize('policy', ['default', 'custom-error', 'custom-return'])
@pytest.mark.parametrize('which', ['SIGINT', 'SIGUSR1'])
@pytest.mark.parametrize('stage', ['acquire', 'release'])
def test_signal_at_lock_handoff_keeps_descriptor_owned(  # noqa: PLR0913 — Independent lock handoff, timing, and caller-signal policies.
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, when: str, policy: str, which: str, stage: str,
) -> None:
    """Signals before/after either real lock primitive cannot orphan its descriptor.

    Include SIGUSR1: ownership must not rely on recognizing and deferring only
    Ctrl+C. Custom exceptions keep identity/context; nonraising handlers keep
    running and the same persistent inode remains available to another owner.
    """
    import fcntl  # noqa: PLC0415 — POSIX-only primitive, after the skip.

    package, image = _products(tmp_path)
    original = fcntl.flock
    touched: list[int] = []
    events: list[str] = []
    failure = RuntimeError('caller signal failed')
    context = LookupError('caller context')
    failure.__context__ = context
    signum = getattr(signal, which)

    def handler(_signum: int, _frame: Any) -> None:
        events.append('signal')
        if policy == 'custom-error':
            raise failure

    def interrupted(file: Any, flags: int) -> Any:
        selected = (bool(flags & fcntl.LOCK_EX) if stage == 'acquire' else flags == fcntl.LOCK_UN) and not touched
        if selected:
            touched.append(file if isinstance(file, int) else file.fileno())
            if when == 'before':
                signal.raise_signal(signum)
        result = original(file, flags)
        if selected and when == 'after':
            signal.raise_signal(signum)
        return result

    previous = signal.signal(signum, signal.default_int_handler if policy == 'default' else handler)
    try:
        with monkeypatch.context() as hooks:
            hooks.setattr(fcntl, 'flock', interrupted)
            if policy == 'custom-return':
                cargo.publish(package, image, b'new metadata')
            else:
                with pytest.raises(KeyboardInterrupt if policy == 'default' else RuntimeError) as caught:
                    cargo.publish(package, image, b'new metadata')
                if policy == 'custom-error':
                    assert caught.value is failure
                    assert failure.__context__ is context
        assert len(touched) == 1
        assert not _open(touched[0]), 'the signal abandoned an open descriptor'
        with FileLock(str(package / '.benchset.publish.lock'), timeout=0):
            pass
        expected = b'new' if stage == 'release' or policy == 'custom-return' else b'old'
        assert (package / '_native/libbenchset.so').read_bytes() == expected + b' image'
        assert (package / '_metadata.json').read_bytes() == expected + b' metadata'
        assert events == ([] if policy == 'default' else ['signal'])
    finally:
        signal.signal(signum, previous)
        _close_orphaned(touched, package / '.benchset.publish.lock')


@pytest.mark.skipif(os.name != 'posix', reason='Observe real POSIX descriptor ownership')
@pytest.mark.parametrize('when', ['before', 'after'])
@pytest.mark.parametrize('error_type', [KeyboardInterrupt, RuntimeError, OSError, ValueError])
def test_publication_body_errors_close_the_lock_before_propagation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, when: str, error_type: type[BaseException],
) -> None:
    """Every body exception releases ownership, before or after a complete commit.

    Keep the exact exception and any completed pair. Closing the owned file
    unlocks the kernel resource without a separately interruptible unlock/state
    handoff; another FileLock client may acquire the persistent inode.
    """
    import fcntl  # noqa: PLC0415

    package, image = _products(tmp_path)
    flock, publish = fcntl.flock, cargo._publish
    touched: list[int] = []
    failure = error_type('publication body failed')

    def acquired(file: Any, flags: int) -> Any:
        if flags & fcntl.LOCK_EX:
            touched.append(file if isinstance(file, int) else file.fileno())
        return flock(file, flags)

    def failed(*args: Any) -> Path:
        if when == 'before':
            raise failure
        publish(*args)
        raise failure

    try:
        monkeypatch.setattr(fcntl, 'flock', acquired)
        monkeypatch.setattr(cargo, '_publish', failed)
        with pytest.raises(error_type) as caught:
            cargo.publish(package, image, b'new metadata')
        assert caught.value is failure
        assert len(touched) == 1
        assert not _open(touched[0])
        with FileLock(str(package / '.benchset.publish.lock'), timeout=0):
            pass
        expected = b'old' if when == 'before' else b'new'
        assert (package / '_native/libbenchset.so').read_bytes() == expected + b' image'
        assert (package / '_metadata.json').read_bytes() == expected + b' metadata'
    finally:
        _close_orphaned(touched, package / '.benchset.publish.lock')


@pytest.mark.skipif(os.name != 'posix', reason='Exercise real POSIX flock handoffs')
@pytest.mark.parametrize('raises', [False, True])
@pytest.mark.parametrize('which', ['SIGINT', 'SIGUSR1'])
@pytest.mark.parametrize('stage', ['acquire', 'release'])
def test_lock_handoff_preserves_handler_policy_changes_and_exact_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raises: bool, which: str, stage: str,
) -> None:
    """Handlers may select SIG_IGN without later restoration overwriting them.

    Deliver twice to detect stale handler replay. A raising handler preserves
    its own error and still leaves the descriptor closed and the lock reusable.
    """
    import fcntl  # noqa: PLC0415

    package, image = _products(tmp_path)
    original = fcntl.flock
    touched: list[int] = []
    events: list[str] = []
    failure = RuntimeError('caller selected ignore')
    signum = getattr(signal, which)

    def handler(_signum: int, _frame: Any) -> None:
        events.append('signal')
        signal.signal(signum, signal.SIG_IGN)
        if raises:
            raise failure

    def interrupted(file: Any, flags: int) -> Any:
        result = original(file, flags)
        selected = bool(flags & fcntl.LOCK_EX) if stage == 'acquire' else flags == fcntl.LOCK_UN
        if selected and not touched:
            touched.append(file if isinstance(file, int) else file.fileno())
            signal.raise_signal(signum)
            signal.raise_signal(signum)
        return result

    previous = signal.signal(signum, handler)
    try:
        with monkeypatch.context() as hooks:
            hooks.setattr(fcntl, 'flock', interrupted)
            if raises:
                with pytest.raises(RuntimeError) as caught:
                    cargo.publish(package, image, b'new metadata')
                assert caught.value is failure
            else:
                cargo.publish(package, image, b'new metadata')
        assert signal.getsignal(signum) is signal.SIG_IGN
        assert events == ['signal']
        assert not _open(touched[0])
        with FileLock(str(package / '.benchset.publish.lock'), timeout=0):
            pass
    finally:
        signal.signal(signum, previous)
        _close_orphaned(touched, package / '.benchset.publish.lock')


def test_waiting_for_another_publisher_remains_cancellable() -> None:
    """Ctrl+C exits contention rather than waiting for the other owner's release.

    A FileLock owner releases after publication has returned, with a bounded
    fallback to expose a swallowed interruption without hanging the test runner.
    This also verifies interoperability with the previous locking backend.
    """
    code = '''import os, signal, tempfile, _thread
from pathlib import Path
from threading import Event, Thread, Timer
from filelock import FileLock
from scripts.cargo import publish
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    package = root / 'package'
    package.mkdir()
    image = root / 'libbenchset.so'
    image.write_bytes(b'image')
    owner = FileLock(str(package / '.benchset.publish.lock'), thread_local=False)
    owner.acquire()
    returned = Event()
    fallback = []
    def release():
        fallback.append(not returned.wait(2))
        owner.release()
    worker = Thread(target=release)
    worker.start()
    timer = Timer(.1, lambda: _thread.interrupt_main() if os.name == 'nt' else os.kill(os.getpid(), signal.SIGINT))
    timer.start()
    caught = False
    try:
        try:
            publish(package, image, b'metadata')
        except KeyboardInterrupt:
            caught = True
    finally:
        returned.set()
        timer.cancel()
        timer.join()
        worker.join(5)
        owner.release()
    assert caught
    assert fallback == [False], 'waiting swallowed Ctrl+C'
    assert not worker.is_alive()
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=False, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


def test_background_publication_does_not_install_process_signal_handlers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A background builder needs no process signal hook and may publish normally."""
    package, image = _products(tmp_path)
    errors: list[BaseException] = []

    def forbidden(*_args: Any) -> None:
        raise AssertionError('a background publisher installed a process signal handler')

    def publish() -> None:
        try:
            cargo.publish(package, image, b'new metadata')
        except BaseException as error:  # noqa: BLE001 — Return child-thread errors to the test.
            errors.append(error)

    monkeypatch.setattr(signal, 'signal', forbidden)
    worker = Thread(target=publish)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert not errors
    assert (package / '_metadata.json').read_bytes() == b'new metadata'


def test_kernel_lock_remains_exclusive_through_the_complete_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The body owns the actual kernel lock before and after committing both files.

    An independent FileLock descriptor must fail nonblockingly while the body
    runs. This deterministically rejects a context that acquires and closes its
    file before yielding, which a process scheduling handshake alone can miss.
    """
    package, image = _products(tmp_path)
    original = cargo._publish
    checked: list[str] = []

    def inspect(*args: Any) -> Path:
        for phase in ('before', 'after'):
            if phase == 'after':
                result = original(*args)
            with pytest.raises(Timeout), FileLock(str(package / '.benchset.publish.lock'), timeout=0):
                pass
            checked.append(phase)
        return result

    monkeypatch.setattr(cargo, '_publish', inspect)
    assert cargo.publish(package, image, b'new metadata') == package / '_native/libbenchset.so'
    assert checked == ['before', 'after']


@pytest.mark.skipif(os.name != 'posix' or not hasattr(os, 'fork'), reason='POSIX fork descriptor inheritance')
@pytest.mark.parametrize('stage', ['body-return', 'body-error', 'acquire-interrupt'])
def test_publication_unlocks_while_a_forked_child_keeps_its_inherited_descriptor(stage: str) -> None:
    """Parent cleanup releases the shared lock even while a child retains its fd.

    Closing only the parent's file leaves flock held by the inherited open-file
    description. Keep the child alive on a pipe and verify another FileLock can
    acquire before releasing that pipe. Cover normal return, a body error and a
    signal after successful acquisition but before the context reaches yield.
    """
    code = f'''import fcntl, os, signal, tempfile
from pathlib import Path
from filelock import FileLock
from scripts import cargo
stage = {stage!r}
flock, publish = fcntl.flock, cargo._publish
read_fd, write_fd = os.pipe()
children = []
failure = RuntimeError('body failed')
def fork_child():
    child = os.fork()
    if child == 0:
        os.close(write_fd)
        os.read(read_fd, 1)
        os._exit(0)
    children.append(child)
    os.close(read_fd)
def acquired(file, flags):
    result = flock(file, flags)
    if stage == 'acquire-interrupt' and flags & fcntl.LOCK_EX and not children:
        fork_child()
        signal.raise_signal(signal.SIGINT)
    return result
def body(*args):
    if stage != 'acquire-interrupt':
        fork_child()
        if stage == 'body-error':
            raise failure
    return publish(*args)
fcntl.flock, cargo._publish = acquired, body
try:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        image = root / 'libbenchset.so'
        image.write_bytes(b'image')
        package = root / 'package'
        try:
            cargo.publish(package, image, b'metadata')
        except BaseException as error:
            if stage == 'acquire-interrupt':
                assert isinstance(error, KeyboardInterrupt)
            elif stage == 'body-error':
                assert error is failure
            else:
                raise
        else:
            assert stage == 'body-return'
        assert len(children) == 1
        assert os.waitpid(children[0], os.WNOHANG) == (0, 0)
        with FileLock(str(package / '.benchset.publish.lock'), timeout=0):
            pass
finally:
    if children:
        os.write(write_fd, b'release')
        for child in children:
            os.waitpid(child, 0)
    else:
        os.close(read_fd)
    os.close(write_fd)
print('released-with-live-child')
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=False, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == 'released-with-live-child\n'


@pytest.mark.skipif(importlib.util.find_spec('_xxsubinterpreters') is None,
                    reason='The experimental CPython subinterpreter harness is unavailable')
@pytest.mark.parametrize('custom', [False, True], ids=['foreign-default', 'foreign-custom'])
def test_subinterpreter_publication_does_not_attempt_unsupported_signal_setup(custom: bool) -> None:
    """The lock preserves the existing subinterpreter publication boundary.

    Their current thread still looks like the threading module's main thread.
    Check signal registration independently to preserve the original
    success/error boundary. Python 3.8 can observe a locally identical default
    handler and reject registration. Newer interpreters can see a foreign
    handler and publish normally.
    """
    root = str(Path(__file__).resolve().parents[1])
    inner = f'''import signal, sys, tempfile
sys.path.insert(0, {root!r})
from pathlib import Path
from scripts.cargo import publish
expected = None
if signal.getsignal(signal.SIGINT) is signal.default_int_handler:
    try:
        previous = signal.signal(signal.SIGINT, lambda *_args: None)
    except BaseException as error:
        expected = type(error), str(error)
    else:
        signal.signal(signal.SIGINT, previous)
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    image = root / 'libbenchset.so'
    image.write_bytes(b'image')
    def attempt(package, operation):
        try:
            operation(package, image, b'metadata')
        except BaseException as error:
            return type(error), str(error)
        assert (package / '_native/libbenchset.so').read_bytes() == b'image'
        assert (package / '_metadata.json').read_bytes() == b'metadata'
        return None
    assert attempt(root / 'package', publish) == expected
print('equivalent', flush=True)
'''
    code = f'''import _xxsubinterpreters as interpreters
import signal
previous = signal.getsignal(signal.SIGINT)
if {custom!r}:
    signal.signal(signal.SIGINT, lambda _signum, _frame: None)
interpreter = interpreters.create()
try:
    interpreters.run_string(interpreter, {inner!r})
finally:
    interpreters.destroy(interpreter)
    signal.signal(signal.SIGINT, previous)
'''
    # Coverage's startup hook deadlocks CPython 3.10 subinterpreter teardown.
    # This helper tests build tooling, outside the measured benchset package.
    environment = os.environ.copy()
    environment.pop('COVERAGE_PROCESS_START', None)
    result = subprocess.run([sys.executable, '-c', code], env=environment,
                            capture_output=True, text=True, check=False, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == 'equivalent\n'


@pytest.mark.parametrize('failures', [0, 2])
def test_windows_lock_uses_byte_zero_and_retries_only_contention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failures: int,
) -> None:
    """Emulate the Windows CRT protocol, including nonempty files and EACCES retry."""
    package, image = _products(tmp_path)
    lock = package / '.benchset.publish.lock'
    lock.write_bytes(b'keep this inode and data')
    identity = lock.stat().st_ino
    calls: list[int] = []
    sleeps: list[float] = []

    def locking(descriptor: int, mode: int, count: int) -> None:
        assert mode == 77
        assert count == 1
        assert os.lseek(descriptor, 0, os.SEEK_CUR) == 0
        calls.append(descriptor)
        assert len(calls) <= failures + 1, 'locking continued after success'
        if len(calls) <= failures:
            os.lseek(descriptor, 7, os.SEEK_SET)
            raise OSError(errno.EACCES, 'another owner')

    monkeypatch.setattr(cargo, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(LK_NBLCK=77, locking=locking))
    monkeypatch.setattr(cargo, 'sleep', sleeps.append, raising=False)
    try:
        cargo.publish(package, image, b'new metadata')
        assert len(calls) == failures + 1
        assert sleeps == [.05] * failures
        assert not _open(calls[-1])
        assert lock.stat().st_ino == identity
        assert lock.read_bytes() == b'keep this inode and data'
        assert (package / '_metadata.json').read_bytes() == b'new metadata'
    finally:
        _close_orphaned(calls, lock)


@pytest.mark.parametrize('failure', [
    OSError(errno.EBADF, 'bad descriptor'), OSError(errno.EIO, 'I/O failure'),
    OSError(errno.EAGAIN, 'unexpected retry code'), KeyboardInterrupt(), RuntimeError('caller failed'),
])
def test_windows_lock_errors_propagate_once_and_close_the_descriptor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: BaseException,
) -> None:
    """Non-contention errors and asynchronous exceptions cannot leak or retry.

    The fake grants the lock after repeated failures so an overbroad exception
    handler fails assertions rather than hanging the test runner indefinitely.
    """
    package, image = _products(tmp_path)
    calls: list[int] = []

    def locking(descriptor: int, _mode: int, _count: int) -> None:
        calls.append(descriptor)
        if len(calls) >= 4:
            return
        raise failure

    monkeypatch.setattr(cargo, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(LK_NBLCK=77, locking=locking))
    monkeypatch.setattr(cargo, 'sleep', lambda _seconds: None, raising=False)
    try:
        with pytest.raises(type(failure)) as caught:
            cargo.publish(package, image, b'new metadata')
        assert caught.value is failure
        assert len(calls) == 1
        assert not _open(calls[0])
        assert (package / '_metadata.json').read_bytes() == b'old metadata'
    finally:
        _close_orphaned(calls, package / '.benchset.publish.lock')


def test_windows_lock_does_not_retry_eacces_from_a_caller_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A caller's EACCES is distinct from contention reported by msvcrt.locking."""
    package, image = _products(tmp_path)
    failure = PermissionError(errno.EACCES, 'caller interrupted Windows lock')
    calls: list[int] = []

    def interrupt(_signum: int, _frame: FrameType | None) -> None:
        raise failure

    def locking(descriptor: int, _mode: int, _count: int) -> None:
        calls.append(descriptor)
        if len(calls) == 1:
            signal.raise_signal(signal.SIGINT)

    monkeypatch.setattr(cargo, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(LK_NBLCK=77, locking=locking))
    previous = signal.signal(signal.SIGINT, interrupt)
    try:
        with pytest.raises(PermissionError, match='caller interrupted Windows lock') as caught:
            cargo.publish(package, image, b'new metadata')
        assert caught.value is failure
        assert len(calls) == 1
        assert not _open(calls[0])
        assert (package / '_metadata.json').read_bytes() == b'old metadata'
    finally:
        signal.signal(signal.SIGINT, previous)
        _close_orphaned(calls, package / '.benchset.publish.lock')


@pytest.mark.skipif(os.name != 'posix', reason='POSIX lock creation and reopen')
@pytest.mark.parametrize('exists', [False, True])
def test_interrupted_lock_open_closes_the_descriptor_with_a_retained_traceback(
    tmp_path: Path, exists: bool,
) -> None:
    """The first visible file owner must already be inside the closing scope.

    Interrupt at the first line that can see the opened lock, then retain the
    exception so a leaked descriptor cannot be hidden by frame destruction.
    """
    package, image = _products(tmp_path)
    lock = package / '.benchset.publish.lock'
    if exists:
        lock.touch()
    wrapped: Any = cargo._package_lock
    code = wrapped.__wrapped__.__code__
    opened: list[int] = []
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    previous_trace = sys.gettrace()

    def trace(frame: FrameType, event: str, _value: object) -> Any:
        if frame.f_code is code and event == 'line' and 'lock' in frame.f_locals and not opened:
            opened.append(frame.f_locals['lock'].fileno())
            signal.raise_signal(signal.SIGINT)
        return trace

    try:
        sys.settrace(trace)
        try:
            with pytest.raises(KeyboardInterrupt) as caught:
                cargo.publish(package, image, b'new metadata')
        finally:
            sys.settrace(previous_trace)
        assert caught.value.__traceback__ is not None
        assert len(opened) == 1
        assert not _open(opened[0])
        assert (package / '_metadata.json').read_bytes() == b'old metadata'
    finally:
        signal.signal(signal.SIGINT, previous)
        _close_orphaned(opened, lock)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX fchmod protocol')
@pytest.mark.parametrize('error_type', [PermissionError, OSError])
def test_posix_permission_handoff_ignores_only_foreign_ownership(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error_type: type[OSError],
) -> None:
    """A foreign-owned writable lock remains usable; other chmod errors close its fd."""
    package, image = _products(tmp_path)
    descriptors: list[int] = []
    failure = error_type('permission change failed')

    def failed(descriptor: int, mode: int) -> None:
        assert mode == 0o666
        descriptors.append(descriptor)
        raise failure

    try:
        with monkeypatch.context() as hooks:
            hooks.setattr(os, 'fchmod', failed)
            if error_type is PermissionError:
                cargo.publish(package, image, b'new metadata')
            else:
                with pytest.raises(OSError, match='permission change failed') as caught:
                    cargo.publish(package, image, b'new metadata')
                assert caught.value is failure
        assert len(descriptors) == 1
        assert not _open(descriptors[0])
        with FileLock(str(package / '.benchset.publish.lock'), timeout=0):
            pass
    finally:
        _close_orphaned(descriptors, package / '.benchset.publish.lock')


@pytest.mark.skipif(not hasattr(signal, 'SIGUSR1') or sys.implementation.name != 'cpython',
                    reason='Real POSIX fchmod interruption under CPython profiling')
def test_lock_permission_error_from_a_caller_signal_aborts_publication(tmp_path: Path) -> None:
    """A caller exception at fchmod is distinct from a foreign lock owner's refusal."""
    package, image = _products(tmp_path)
    failure = PermissionError('publication interrupted by caller')

    def interrupt(_signum: int, _frame: FrameType | None) -> None:
        raise failure

    def profile(_frame: FrameType, event: str, function: object) -> None:
        if event == 'c_call' and function is os.fchmod:
            signal.raise_signal(signal.SIGUSR1)

    previous = signal.signal(signal.SIGUSR1, interrupt)
    previous_profile = sys.getprofile()
    try:
        sys.setprofile(profile)
        with pytest.raises(PermissionError, match='publication interrupted by caller') as caught:
            cargo.publish(package, image, b'new metadata')
        assert caught.value is failure
        assert (package / '_metadata.json').read_bytes() == b'old metadata'
        assert (package / '_native/libbenchset.so').read_bytes() == b'old image'
    finally:
        sys.setprofile(previous_profile)
        signal.signal(signal.SIGUSR1, previous)


@pytest.mark.skipif(not hasattr(signal, 'SIGUSR1') or sys.implementation.name != 'cpython',
                    reason='Real caller signals at CPython registration boundaries')
@pytest.mark.parametrize(('boundary', 'action'), [
    ('install', 'raise'), ('restore', 'raise'), ('install', 'replace'), ('restore', 'replace'), ('both', 'replace'),
])
def test_publication_preserves_caller_sigint_policy_at_registration(
    tmp_path: Path, boundary: str, action: str,
) -> None:
    """SIGUSR1 may interrupt registration or select a new SIGINT policy.

    Interrupt after installing the temporary handler or immediately before
    restoring it. An exception must escape after cleanup and a caller's
    replacement must remain active. The two file commits stay coherent.
    If both swaps are interrupted, a previous SIG_IGN must not overwrite
    the callable selected by the second interruption.
    """
    import _signal  # type: ignore[import-not-found]  # noqa: PLC0415

    package, image = _products(tmp_path)
    failure = RuntimeError('publication interrupted by caller signal')
    default = signal.default_int_handler
    calls = 0

    def replacement(_signum: int, _frame: FrameType | None) -> None:
        pass

    def caller_signal(_signum: int, _frame: FrameType | None) -> None:
        if action == 'raise':
            raise failure
        signal.signal(signal.SIGINT, signal.SIG_IGN if boundary == 'both' and calls == 1 else replacement)

    def profile(_frame: FrameType, event: str, function: object) -> None:
        nonlocal calls
        if function is _signal.signal and event == ('c_return' if boundary == 'install' else 'c_call'):
            calls += 1
            if (boundary == 'both' and calls <= 2) or calls == (1 if boundary == 'install' else 2):
                signal.raise_signal(signal.SIGUSR1)

    previous = signal.signal(signal.SIGINT, default)
    previous_caller = signal.signal(signal.SIGUSR1, caller_signal)
    previous_profile = sys.getprofile()
    try:
        sys.setprofile(profile)
        try:
            if action == 'raise':
                with pytest.raises(RuntimeError) as caught:
                    cargo.publish(package, image, b'new metadata')
                assert caught.value is failure
            else:
                cargo.publish(package, image, b'new metadata')
        finally:
            sys.setprofile(previous_profile)
        assert signal.getsignal(signal.SIGINT) is (default if action == 'raise' else replacement)
        generation = b'old' if boundary == 'install' and action == 'raise' else b'new'
        assert (package / '_metadata.json').read_bytes() == generation + b' metadata'
        assert (package / '_native/libbenchset.so').read_bytes() == generation + b' image'
    finally:
        signal.signal(signal.SIGUSR1, previous_caller)
        signal.signal(signal.SIGINT, previous)


@pytest.mark.skipif(not hasattr(signal, 'SIGUSR1'), reason='Real caller signals during POSIX rollback')
@pytest.mark.parametrize('previous_image', [False, True])
@pytest.mark.parametrize('second_signal', ['before', 'after'])
@pytest.mark.parametrize('second_error', [RuntimeError, PermissionError])
def test_repeated_caller_signal_cannot_leave_a_partial_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, previous_image: bool, second_signal: str,
    second_error: type[Exception],
) -> None:
    """A second interruption during rollback must leave the old generation intact.

    Raise after the image rename and on either side of the rollback system
    operation. A caller's PermissionError must not be treated as an OS refusal.
    Retrying an already-completed rollback must be harmless for both old and
    initially absent images.
    """
    package, image = _products(tmp_path)
    installed = package / '_native/libbenchset.so'
    metadata = package / '_metadata.json'
    if not previous_image:
        installed.unlink()
        metadata.unlink()
    replace, unlink = Path.replace, Path.unlink
    errors = [RuntimeError('publication interrupted'), second_error('rollback interrupted')]
    delivered: list[Exception] = []

    def interrupt(_signum: int, _frame: FrameType | None) -> None:
        error = errors[len(delivered)]
        delivered.append(error)
        raise error

    def interrupted_rollback(operation: Any) -> Any:
        if len(delivered) != 1:
            return operation()
        if second_signal == 'before':
            signal.raise_signal(signal.SIGUSR1)
        result = operation()
        signal.raise_signal(signal.SIGUSR1)
        return result

    def replace_path(path: Path, destination: Path) -> Path:
        if path.name == 'previous-library':
            if not path.exists():
                raise OSError('backup already restored')
            return interrupted_rollback(lambda: replace(path, destination))
        result = replace(path, destination)
        if path.name == image.name:
            signal.raise_signal(signal.SIGUSR1)
        return result

    def unlink_path(path: Path, *args: Any, **options: Any) -> None:
        if path == installed:
            interrupted_rollback(lambda: unlink(path, *args, **options))
        else:
            unlink(path, *args, **options)

    monkeypatch.setattr(Path, 'replace', replace_path)
    monkeypatch.setattr(Path, 'unlink', unlink_path)
    previous = signal.signal(signal.SIGUSR1, interrupt)
    try:
        with pytest.raises(RuntimeError) as caught:
            cargo.publish(package, image, b'new metadata')
        assert caught.value is errors[0]
        assert delivered == errors
        if previous_image:
            assert installed.read_bytes() == b'old image'
            assert metadata.read_bytes() == b'old metadata'
        else:
            assert not installed.exists()
            assert not metadata.exists()
    finally:
        signal.signal(signal.SIGUSR1, previous)


def test_rollback_does_not_retry_a_filesystem_refusal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A direct restore failure must surface rather than being retried as a caller signal."""
    package, image = _products(tmp_path)
    replace = Path.replace
    failure = PermissionError(errno.EACCES, 'backup restore refused')
    restores = 0

    def refused(path: Path, destination: Path) -> Path:
        nonlocal restores
        if path.name == '_metadata.json':
            raise OSError('metadata rename failed')
        if path.name == 'previous-library':
            restores += 1
            if restores == 1:
                raise failure
        return replace(path, destination)

    monkeypatch.setattr(Path, 'replace', refused)
    with pytest.raises(PermissionError, match='backup restore refused') as caught:
        cargo.publish(package, image, b'new metadata')
    assert caught.value is failure
    assert restores == 1


@pytest.mark.skipif(sys.implementation.name != 'cpython', reason='CPython filesystem audit event')
def test_persistent_audit_refusal_during_rollback_terminates(tmp_path: Path) -> None:
    """A synchronous veto from an audit hook cannot keep retrying rollback forever."""
    source = '''
import os
import sys
from pathlib import Path
from scripts.cargo import publish

directory = Path(sys.argv[1])
package, image = directory / 'package', directory / 'benchset.so'
image.write_bytes(b'old image')
publish(package, image, b'old metadata')
image.write_bytes(b'new image')
denial = PermissionError('backup restore forbidden by audit')
refusals = []

def audit(event, args):
    if event == 'os.rename':
        source, target = map(lambda path: Path(os.fsdecode(path)).name, args[:2])
        if target == '_metadata.json':
            raise OSError('metadata replace failed')
        if source == 'previous-library':
            refusals.append(source)
            raise denial

sys.addaudithook(audit)
try:
    publish(package, image, b'new metadata')
except PermissionError as error:
    assert error is denial
else:
    raise AssertionError('the audit refusal was swallowed')
assert 1 <= len(refusals) <= 3, refusals
'''
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-c', source, str(tmp_path)], cwd=root,
                            capture_output=True, text=True, timeout=8, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(sys.implementation.name != 'cpython' or sys.platform not in ('darwin', 'linux'),
                    reason='POSIX publication lock and CPython native symbol audit')
@pytest.mark.parametrize('event_kind', ['exec', 'ctypes.dlsym'])
@pytest.mark.parametrize('fork', [False, True])
def test_publication_audit_cannot_reenter_preparation_or_the_package_lock(
    tmp_path: Path, event_kind: str, fork: bool,
) -> None:
    """Reject recursive preparation in the same process and allow it after fork.

    The outer publication continues once the audit hook returns. The child
    verifies its own independent publication before the parent proceeds.
    """
    source = '''
import os
import sys
from pathlib import Path
from scripts.cargo import publish

directory = Path(sys.argv[1])
event_kind = sys.argv[2]
fork = sys.argv[3] == 'True'
package, image = directory / 'package', directory / 'benchset.so'
image.write_bytes(b'image')
events = []
entered = False

def audit(event, args):
    global entered
    if not entered and event == event_kind and ((event == 'ctypes.dlsym' and args[1] == 'sigaction') or
                                                (event == 'exec' and args[0].co_filename.endswith('_signal_state.py'))):
        entered = True
        if fork:
            child = os.fork()
            if child == 0:
                try:
                    assert publish(directory / 'child', image, b'child').read_bytes() == b'image'
                    assert (directory / 'child/_metadata.json').read_bytes() == b'child'
                except BaseException:
                    import traceback
                    traceback.print_exc()
                    os._exit(1)
                os._exit(0)
            events.append(os.waitpid(child, 0)[1])
            return
        try:
            publish(package, image, b'nested')
        except RuntimeError as error:
            events.append(str(error))

sys.setrecursionlimit(100)
sys.addaudithook(audit)
if event_kind == 'exec':
    import importlib
    import scripts.cargo as cargo
    importlib.reload(cargo)
    publish = cargo.publish
assert publish(package, image, b'outer').read_bytes() == b'image'
assert (package / '_metadata.json').read_bytes() == b'outer'
expected = 0 if fork else ('reentrant native SIGINT disposition lookup' if event_kind == 'ctypes.dlsym' else
                          'reentrant publication signal support initialization')
assert events == [expected], events
'''
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-c', source, str(tmp_path), event_kind, str(fork)], cwd=root,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not hasattr(signal, 'SIGUSR1'), reason='POSIX rollback under a custom SIGINT policy')
def test_custom_policy_rollback_does_not_import_signal_support_under_lock(tmp_path: Path) -> None:
    """Rollback under SIG_IGN must classify exceptions without importing a helper.

    A source execution audit hook attempts another publication in the same
    directory. Reuse the real two-signal rollback probe in a fresh process.
    """
    source = '''
import signal
import sys
from pathlib import Path
import pytest
from scripts.cargo import publish
from tests.test_publication_signals import test_repeated_caller_signal_cannot_leave_a_partial_publication as rollback

directory = Path(sys.argv[1])
events = []

def audit(event, args):
    if event == 'exec' and args[0].co_filename.endswith('_signal_state.py'):
        events.append(event)
        publish(directory / 'package', directory / 'libbenchset.so', b'nested')

signal.signal(signal.SIGINT, signal.SIG_IGN)
sys.addaudithook(audit)
with pytest.MonkeyPatch.context() as hooks:
    rollback(hooks, directory, True, 'before', PermissionError)
assert not events, events
'''
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-c', source, str(tmp_path)], cwd=root,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(sys.implementation.name != 'cpython' or sys.platform not in ('darwin', 'linux'),
                    reason='POSIX aliased lock and CPython native symbol audit')
@pytest.mark.parametrize('redirect_at', ['lookup', 'lock', 'commit'])
def test_publication_locks_and_returns_the_same_package(tmp_path: Path, redirect_at: str) -> None:
    """Lock the selected directory and return the actual artifact if its alias changes.

    Redirect before lock selection, while opening the lock, or after the
    publisher has chosen its commit target. A competing descriptor checks that
    metadata is written under the lock for that actual target.
    """
    source = '''
import fcntl
import os
import sys
from pathlib import Path
from scripts.cargo import publish

directory = Path(sys.argv[1])
redirect_at = sys.argv[2]
first, second = directory / 'first', directory / 'second'
first.mkdir()
second.mkdir()
package = directory / 'package'
package.symlink_to(first, target_is_directory=True)
image = directory / 'benchset.so'
image.write_bytes(b'image')
events = []
expected = second if redirect_at == 'lookup' else first

def redirect():
    if not events:
        package.unlink()
        package.symlink_to(second, target_is_directory=True)
        events.append('redirected')

def profile(_frame, event, function):
    if redirect_at == 'lock' and event == 'c_call' and function is os.fchmod:
        redirect()

def audit(event, args):
    if redirect_at == 'lookup' and event == 'ctypes.dlsym' and args[1] == 'sigaction':
        redirect()
    if event == 'os.rename':
        name = Path(os.fsdecode(args[1])).name
        if redirect_at == 'commit' and name == image.name:
            redirect()
        if name == '_metadata.json':
            with (expected / '.benchset.publish.lock').open('r+b') as competing:
                try:
                    fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    events.append('correct lock held')

sys.addaudithook(audit)
sys.setprofile(profile)
try:
    published = publish(package, image, b'metadata')
finally:
    sys.setprofile(None)
assert published == (package if redirect_at == 'lookup' else first) / '_native' / image.name
assert published.read_bytes() == b'image'
assert (expected / '_metadata.json').read_bytes() == b'metadata'
assert not ((first if expected == second else second) / '_metadata.json').exists()
assert events == ['redirected', 'correct lock held'], events
'''
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-c', source, str(tmp_path), redirect_at], cwd=root,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not hasattr(signal, 'SIGUSR1') or sys.implementation.name != 'cpython',
                    reason='CPython stat profiling at publication return')
def test_publication_does_not_hide_caller_error_while_checking_the_return_path(tmp_path: Path) -> None:
    """A caller signal during the final path check must propagate after a complete commit."""
    package, image = _products(tmp_path)
    failure = PermissionError('caller interrupted final path check')

    def interrupt(_signum: int, _frame: FrameType | None) -> None:
        raise failure

    def profile(frame: FrameType, event: str, function: object) -> None:
        if event == 'c_call' and function is os.stat:
            current: FrameType | None = frame
            while current is not None:
                if current.f_code is Path.samefile.__code__:
                    signal.raise_signal(signal.SIGUSR1)
                current = current.f_back

    previous = signal.signal(signal.SIGUSR1, interrupt)
    previous_profile = sys.getprofile()
    try:
        sys.setprofile(profile)
        with pytest.raises(PermissionError, match='caller interrupted final path check') as caught:
            cargo.publish(package, image, b'new metadata')
        assert caught.value is failure
        assert (package / '_metadata.json').read_bytes() == b'new metadata'
        assert (package / '_native/libbenchset.so').read_bytes() == b'new image'
    finally:
        sys.setprofile(previous_profile)
        signal.signal(signal.SIGUSR1, previous)


@pytest.mark.skipif(sys.implementation.name != 'cpython' or sys.platform not in ('darwin', 'linux'),
                    reason='CPython POSIX ctypes audit events')
@pytest.mark.parametrize('audit_mode', [
    'deny-process-image', 'no-ctypes', 'loaded-extension', 'change-directory', 'redirect-source', 'replace-source-file',
    'confine-temp', 'broken-extension', 'partial-ctypes', 'partial-inspect', 'deny-frame-inspection',
])
def test_publication_handles_audit_at_native_snapshot_bootstrap(tmp_path: Path, audit_mode: str) -> None:
    """Audit may restrict temporary files or snapshots, and cannot redirect the built image."""
    source = '''
import os
import signal
import sys
from pathlib import Path

directory, mode = Path(sys.argv[1]), sys.argv[2]
if mode in ('partial-ctypes', 'partial-inspect'):
    from types import ModuleType
    sys.modules[mode[len('partial-'):]] = ModuleType(mode)
from scripts.cargo import publish

source, redirected = directory / 'source', directory / 'redirected'
source.mkdir()
redirected.mkdir()
(source / 'compiled.so').write_bytes(b'intended image')
(source / 'benchset.so').symlink_to(source / 'compiled.so')
(redirected / 'benchset.so').write_bytes(b'different image')
(redirected / 'package').mkdir()
os.chdir(source)
events = []

def audit(event, args):
    if mode == 'deny-frame-inspection' and event == 'object.__getattr__' and args[1] == 'f_code':
        events.append(event)
        raise PermissionError('frame introspection forbidden')
    if mode == 'confine-temp' and event == 'tempfile.mkdtemp':
        events.append(event)
        if source / 'package' not in Path(args[0]).parents:
            raise PermissionError('publication must keep temporary files inside the package')
    if mode == 'deny-process-image' and event == 'ctypes.dlopen' and args[0] is None:
        events.append(event)
        raise PermissionError('native process handle forbidden')
    if mode == 'change-directory' and event == 'ctypes.dlsym' and args[1] == 'sigaction':
        events.append(event)
        os.chdir(redirected)
    if mode == 'redirect-source' and event == 'ctypes.dlsym' and args[1] == 'sigaction':
        events.append(event)
        (source / 'benchset.so').unlink()
        (source / 'benchset.so').symlink_to(redirected / 'benchset.so')
    if mode == 'replace-source-file' and event == 'ctypes.dlsym' and args[1] == 'sigaction':
        events.append(event)
        (redirected / 'benchset.so').replace(source / 'compiled.so')

assert mode == 'partial-ctypes' or 'ctypes' not in sys.modules
if mode == 'no-ctypes':
    sys.modules['_ctypes'] = None
elif mode == 'loaded-extension':
    import _ctypes
    _ctypes.__spec__ = None
elif mode == 'broken-extension':
    from importlib.util import spec_from_loader
    class BrokenExtension:
        def find_spec(self, name, *_args):
            return spec_from_loader(name, self) if name == '_ctypes' else None
        def create_module(self, _spec):
            raise ImportError('optional extension dependency libffi is unavailable', name='_ctypes')
        def exec_module(self, _module):
            raise AssertionError('cannot execute an unavailable extension')
    sys.meta_path.insert(0, BrokenExtension())
elif mode == 'deny-frame-inspection':
    signal.signal(signal.SIGINT, signal.SIG_IGN)
sys.addaudithook(audit)
published = publish(Path('package'), Path('benchset.so'), b'metadata')
assert published.read_bytes() == b'intended image'
assert published.name == 'benchset.so'
assert (source / 'package/_metadata.json').read_bytes() == b'metadata'
assert not (redirected / 'package/_metadata.json').exists()
assert signal.getsignal(signal.SIGINT) is (signal.SIG_IGN if mode == 'deny-frame-inspection' else signal.default_int_handler)
if mode == 'deny-process-image':
    assert events and set(events) == {'ctypes.dlopen'}, events
elif mode == 'no-ctypes':
    assert not events and 'ctypes' not in sys.modules, events
elif mode == 'confine-temp':
    assert events and set(events) == {'tempfile.mkdtemp'}, events
elif mode == 'loaded-extension':
    assert not events
elif mode == 'broken-extension':
    assert not events and 'ctypes' not in sys.modules, events
elif mode in ('partial-ctypes', 'partial-inspect'):
    assert not events
elif mode == 'deny-frame-inspection':
    assert events and set(events) == {'object.__getattr__'}, events
else:
    assert events == ['ctypes.dlsym'], events
'''
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-S', '-c', source, str(tmp_path), audit_mode], cwd=root,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(('error_number', 'winerror'), [
    (errno.ENOENT, None), (errno.ENOTDIR, None), (errno.EBADF, None), (errno.ELOOP, None),
    (errno.EACCES, None), (errno.EIO, None), (errno.EINVAL, 21), (errno.EINVAL, 123), (errno.EINVAL, 1921),
    (errno.EINVAL, 999),
])
@pytest.mark.parametrize('instrumentation', ['none', 'plain', 'metadata'])
def test_stat_absence_policy_matches_the_interpreters_path_exists(
    monkeypatch: pytest.MonkeyPatch, error_number: int, winerror: int | None, instrumentation: str,
) -> None:
    """Preserve pathlib's OS-error policy across versions and transparent tracing wrappers."""
    failure = OSError(error_number, 'OS stat refusal')
    if winerror is not None:
        failure.winerror = winerror  # type: ignore[attr-defined]  # Exercise Windows errors on every host.
    original = Path.exists

    def traced_exists(path: Any, *args: Any, **kwargs: Any) -> bool:
        return original(path, *args, **kwargs)

    if instrumentation != 'none':
        monkeypatch.setattr(Path, 'exists', wraps(original)(traced_exists) if instrumentation == 'metadata' else traced_exists)

    class Probe:
        def stat(self, **_kwargs: Any) -> os.stat_result:
            raise failure

        def __fspath__(self) -> str:
            raise failure

    probe = Probe()
    try:
        expected = Path.exists(probe)  # type: ignore[arg-type]  # Use the real pathlib policy with a controlled stat.
    except OSError:
        failure.__traceback__ = None
        with pytest.raises(OSError, match='OS stat refusal') as caught:
            cargo._stat_if_present(probe.stat)
        assert caught.value is failure
    else:
        assert not expected
        failure.__traceback__ = None
        assert cargo._stat_if_present(probe.stat) is None


@pytest.mark.skipif(not hasattr(signal, 'SIGUSR1'), reason='A real competing caller signal')
@pytest.mark.parametrize('interrupted', [False, True])
def test_transparent_stat_instrumentation_keeps_publication_and_caller_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupted: bool,
) -> None:
    """A tracing wrapper is part of the OS operation, while a signal raised inside it remains caller-owned."""
    package, image = _products(tmp_path)
    original = os.stat
    calls: list[object] = []
    failure = FileNotFoundError(errno.ENOENT, 'caller interrupted instrumented stat')

    def interrupt(_signum: int, _frame: FrameType | None) -> None:
        raise failure

    def traced_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        calls.append(path)
        if interrupted and isinstance(path, Path) and path.name == '.benchset.publish.lock':
            signal.raise_signal(signal.SIGUSR1)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, 'stat', traced_stat)
    previous = signal.signal(signal.SIGUSR1, interrupt)
    try:
        if interrupted:
            with pytest.raises(FileNotFoundError, match='caller interrupted instrumented stat') as caught:
                cargo.publish(package, image, b'new metadata')
            assert caught.value is failure
            assert (package / '_metadata.json').read_bytes() == b'old metadata'
        else:
            assert cargo.publish(package, image, b'new metadata').read_bytes() == b'new image'
            assert (package / '_metadata.json').read_bytes() == b'new metadata'
        assert calls
    finally:
        signal.signal(signal.SIGUSR1, previous)


@pytest.mark.parametrize('failure', [False, True])
@pytest.mark.usefixtures('_stat_proxy')
def test_stat_proxies_preserve_a_complete_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: bool,
) -> None:
    """Transparent stat proxies preserve fresh publication and rollback's original image/metadata pair."""
    package, image = _products(tmp_path)
    if failure:
        (package / '.benchset.publish.lock').touch()
    replace = Path.replace

    def replace_path(path: Path, destination: Path) -> Path:
        if failure and path.name == '_metadata.json':
            raise OSError('metadata rename failed')
        return replace(path, destination)

    monkeypatch.setattr(Path, 'replace', replace_path)
    if failure:
        with pytest.raises(OSError, match='metadata rename failed'):
            cargo.publish(package, image, b'new metadata')
        assert (package / '_native/libbenchset.so').read_bytes() == b'old image'
        assert (package / '_metadata.json').read_bytes() == b'old metadata'
    else:
        assert cargo.publish(package, image, b'new metadata').read_bytes() == b'new image'
        assert (package / '_metadata.json').read_bytes() == b'new metadata'


def test_open_dispatcher_can_reuse_an_existing_publication_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary open tracing must not turn an existing lock into a failed publication."""
    package, image = _products(tmp_path)
    (package / '.benchset.publish.lock').touch()
    original = builtins.open

    def dispatch(operation: str, arguments: tuple, keywords: dict) -> Any:
        assert operation == 'open'
        return original(*arguments, **keywords)

    def traced_open(*args: Any, **kwargs: Any) -> Any:
        return dispatch('open', args, kwargs)

    monkeypatch.setattr(builtins, 'open', traced_open)
    monkeypatch.setattr(io, 'open', traced_open)
    assert cargo.publish(package, image, b'new metadata').read_bytes() == b'new image'
    assert (package / '_metadata.json').read_bytes() == b'new metadata'


@pytest.mark.parametrize('callback', ['audit', 'profile', 'trace'])
@pytest.mark.parametrize('existing', [False, True])
def test_direct_host_callback_can_refuse_publication_lock_open(tmp_path: Path, callback: str, existing: bool) -> None:
    """A host veto is not a kernel EEXIST and must never trigger a second open or a publication."""
    source = '''
import builtins
import errno
import inspect
import signal
import sys
from pathlib import Path
from scripts import cargo
from scripts.cargo import publish

directory, kind = Path(sys.argv[1]), sys.argv[2]
package, image = directory / 'package', directory / 'benchset.so'
package.mkdir()
image.write_bytes(b'image')
lock = package / '.benchset.publish.lock'
if sys.argv[3] == 'True':
    lock.touch()
failure = FileExistsError(errno.EEXIST, 'host lock veto')
events = []
lines, start = inspect.getsourcelines(cargo._open_package_lock)
boundaries = {start + index for index, line in enumerate(lines) if 'return open(path,' in line}
def refuse():
    events.append('refused')
    raise failure
def audit(event, args):
    if event == 'open' and args[0] == str(lock) and not events:
        refuse()
def profile(frame, event, value):
    if event == 'c_call' and value is builtins.open and frame.f_locals.get('path') == lock and not events:
        refuse()
def trace(frame, event, value):
    if event == 'line' and frame.f_code is cargo._open_package_lock.__code__ and frame.f_lineno in boundaries and not events:
        refuse()
    return trace

signal.signal(signal.SIGINT, signal.SIG_IGN)
if kind == 'audit':
    sys.addaudithook(audit)
elif kind == 'profile':
    sys.setprofile(profile)
else:
    sys.settrace(trace)
try:
    publish(package, image, b'metadata')
except FileExistsError as error:
    assert error is failure
else:
    raise AssertionError('publication bypassed the host veto')
assert events == ['refused'], events
assert not (package / '_metadata.json').exists()
'''
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-c', source, str(tmp_path), callback, str(existing)], cwd=root,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('policy', ['default', 'ignore'])
def test_partial_traceback_import_does_not_prevent_source_publication(tmp_path: Path, policy: str) -> None:
    """A child forked during traceback import can publish with and without signal snapshots."""
    source = '''
import signal
import sys
from pathlib import Path
from types import ModuleType

sys.modules['traceback'] = ModuleType('traceback')
from scripts.cargo import publish
root = Path(sys.argv[1])
image = root / 'libbenchset.so'
image.write_bytes(b'image')
signal.signal(signal.SIGINT, signal.SIG_IGN if sys.argv[2] == 'ignore' else signal.default_int_handler)
installed = publish(root / 'package', image, b'metadata')
assert installed.read_bytes() == b'image'
assert (root / 'package/_metadata.json').read_bytes() == b'metadata'
'''
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-S', '-c', source, str(tmp_path), policy], cwd=root,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(os.name != 'posix', reason='Real POSIX symlink and search permissions')
@pytest.mark.parametrize('target', ['loop', 'unreadable'])
def test_publication_preserves_path_exists_policy_for_previous_symlinks(tmp_path: Path, target: str) -> None:
    """Previously replaceable broken images remain repairable without changing permission errors."""
    package, image = _products(tmp_path)
    installed = package / '_native/libbenchset.so'
    installed.unlink()
    protected = tmp_path / 'protected'
    protected.mkdir()
    inaccessible = protected / 'old-image'
    inaccessible.write_bytes(b'old image')
    installed.symlink_to(installed if target == 'loop' else inaccessible)
    protected.chmod(0)
    try:
        try:
            exists = installed.exists()
        except OSError as expected:
            with pytest.raises(type(expected)) as caught:
                cargo.publish(package, image, b'new metadata')
            assert caught.value.errno == expected.errno  # noqa: PT017 — Compare against this interpreter's Path.exists.
            assert installed.is_symlink()
            assert (package / '_metadata.json').read_bytes() == b'old metadata'
        else:
            if exists:
                pytest.skip('The process can access the protected target despite its mode')
            assert cargo.publish(package, image, b'new metadata') == installed
            assert not installed.is_symlink()
            assert installed.read_bytes() == b'new image'
            assert (package / '_metadata.json').read_bytes() == b'new metadata'
    finally:
        protected.chmod(0o700)


@pytest.mark.skipif(not hasattr(signal, 'SIGUSR1') or sys.implementation.name != 'cpython',
                    reason='CPython stat profiling with a real POSIX caller signal')
@pytest.mark.parametrize('boundary', ['lock', 'backup', 'rollback-metadata', 'rollback-library', 'rollback-backup'])
def test_caller_stat_error_is_not_mistaken_for_an_absent_publication_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, boundary: str,
) -> None:
    """An ENOENT raised by the caller is not evidence that a lock or backup is absent."""
    package, image = _products(tmp_path)
    installed = package / '_native/libbenchset.so'
    replace = Path.replace
    first = RuntimeError('caller interrupted the commit')
    second = FileNotFoundError(errno.ENOENT, 'caller interrupted stat')
    committing = False
    interrupted = False
    rolling_back = boundary.startswith('rollback-')

    def replace_path(path: Path, destination: Path) -> Path:
        nonlocal committing
        result = replace(path, destination)
        if rolling_back and destination == installed and path.name == image.name:
            committing = True
            raise first
        return result

    def interrupt(_signum: int, _frame: FrameType | None) -> None:
        raise second

    def profile(frame: FrameType, event: str, function: object) -> None:
        nonlocal interrupted
        if interrupted or event != 'c_call' or function not in (os.stat, os.lstat):
            return
        path = frame.f_locals.get('self')
        expected = {'lock': '.benchset.publish.lock', 'backup': installed.name, 'rollback-metadata': '_metadata.json',
                    'rollback-library': image.name, 'rollback-backup': 'previous-library'}[boundary]
        if isinstance(path, Path) and path.name == expected and (not rolling_back or committing):
            interrupted = True
            signal.raise_signal(signal.SIGUSR1)

    monkeypatch.setattr(Path, 'replace', replace_path)
    previous = signal.signal(signal.SIGUSR1, interrupt)
    previous_profile = sys.getprofile()
    expected_error = first if rolling_back else second
    try:
        sys.setprofile(profile)
        with pytest.raises(type(expected_error)) as caught:
            cargo.publish(package, image, b'new metadata')
        assert caught.value is expected_error
        assert interrupted
        assert installed.read_bytes() == b'old image'
        assert (package / '_metadata.json').read_bytes() == b'old metadata'
    finally:
        sys.setprofile(previous_profile)
        signal.signal(signal.SIGUSR1, previous)


@pytest.mark.skipif(not hasattr(signal, 'SIGUSR1') or sys.implementation.name != 'cpython',
                    reason='CPython exclusive open audit with a real POSIX caller signal')
@pytest.mark.parametrize('opaque_traceback', [False, True])
@pytest.mark.parametrize('existing', [False, True])
def test_caller_file_exists_error_does_not_retry_publication_lock_open(
    tmp_path: Path, opaque_traceback: bool, existing: bool,
) -> None:
    """Only a direct EEXIST permits reopening, even if the caller overrides traceback access."""
    source = '''
import errno
import os
import signal
import sys
from pathlib import Path
from scripts.cargo import publish

directory = Path(sys.argv[1])
package, image = directory / 'package', directory / 'benchset.so'
package.mkdir()
image.write_bytes(b'image')
lock = package / '.benchset.publish.lock'
if sys.argv[3] == 'True':
    lock.touch()

class OpaqueTraceback(FileExistsError):
    @property
    def __traceback__(self):
        raise RuntimeError('caller traceback property must not run')

kind = OpaqueTraceback if sys.argv[2] == 'True' else FileExistsError
failure = kind(errno.EEXIST, 'caller interrupted exclusive open')
attempts = []

def interrupt(_signum, _frame):
    raise failure

def audit(event, args):
    if event == 'open' and args[0] == str(lock):
        attempts.append(args[1])
        signal.raise_signal(signal.SIGUSR1)

signal.signal(signal.SIGUSR1, interrupt)
sys.addaudithook(audit)
try:
    publish(package, image, b'metadata')
except FileExistsError as error:
    assert error is failure
else:
    raise AssertionError('the caller exception was swallowed')
assert not (package / '_metadata.json').exists()
assert len(attempts) == 1, attempts
'''
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-c', source, str(tmp_path), str(opaque_traceback), str(existing)], cwd=root,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not hasattr(signal, 'SIGUSR1') or sys.implementation.name != 'cpython',
                    reason='Real POSIX signal selection during lock acquisition')
def test_publication_uses_the_policy_selected_during_lock_acquisition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """If the caller enables default SIGINT at lock time, use it for commit and rollback.

    Observe the change at fchmod. A later SIGINT during rollback must take
    priority over the initial metadata error without leaving mixed files.
    """
    package, image = _products(tmp_path)
    replace = Path.replace
    selected = False
    rollback_interrupted = False

    def choose_default(_signum: int, _frame: FrameType | None) -> None:
        signal.signal(signal.SIGINT, signal.default_int_handler)

    def profile(_frame: FrameType, event: str, function: object) -> None:
        nonlocal selected
        if not selected and event == 'c_call' and function is os.fchmod:
            selected = True
            signal.raise_signal(signal.SIGUSR1)

    def replace_path(path: Path, destination: Path) -> Path:
        nonlocal rollback_interrupted
        if path.name == '_metadata.json':
            raise OSError('metadata rename failed')
        if path.name == 'previous-library' and not rollback_interrupted:
            rollback_interrupted = True
            signal.raise_signal(signal.SIGINT)
        return replace(path, destination)

    monkeypatch.setattr(Path, 'replace', replace_path)
    previous = signal.signal(signal.SIGINT, lambda *_args: None)
    previous_caller = signal.signal(signal.SIGUSR1, choose_default)
    previous_profile = sys.getprofile()
    try:
        sys.setprofile(profile)
        with pytest.raises(KeyboardInterrupt):
            cargo.publish(package, image, b'new metadata')
        assert selected
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
        assert (package / '_metadata.json').read_bytes() == b'old metadata'
        assert (package / '_native/libbenchset.so').read_bytes() == b'old image'
    finally:
        sys.setprofile(previous_profile)
        signal.signal(signal.SIGUSR1, previous_caller)
        signal.signal(signal.SIGINT, previous)
