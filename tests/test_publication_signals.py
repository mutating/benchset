"""Publication locks keep descriptors owned across ordinary and signal errors."""

from __future__ import annotations

import errno
import importlib.util
import os
import signal
import subprocess
import sys
import weakref
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from typing import Any

import pytest
from filelock import FileLock, Timeout

from scripts import cargo

_OWNERS: weakref.WeakValueDictionary[int, Any] = weakref.WeakValueDictionary()


@pytest.fixture(autouse=True)
def _observe_owned_lock_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failing mutations close file objects, avoiding later closes of reused fds."""
    original = Path.open

    def opened(path: Path, *args: Any, **kwargs: Any) -> Any:
        file = original(path, *args, **kwargs)
        if path.name == '.benchset.publish.lock':
            _OWNERS[file.fileno()] = file
        return file

    monkeypatch.setattr(Path, 'open', opened)


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

    Their current thread still looks like the threading module's main thread,
    while signal.signal is forbidden. The unchanged unlocked publisher supplies
    the interpreter's original success/error boundary: Python 3.8 can have a
    locally identical default handler and reject signal setup, while newer
    interpreters see a foreign handler and publish normally. Locking must not
    add another error or retry forever in either case.
    """
    root = str(Path(__file__).resolve().parents[1])
    inner = f'''import sys, tempfile
sys.path.insert(0, {root!r})
from pathlib import Path
from scripts.cargo import _publish, publish
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
    expected = attempt(root / 'reference', _publish)
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
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=False, timeout=3)
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
