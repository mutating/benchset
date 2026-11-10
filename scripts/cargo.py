"""Cargo output paths shared by build and release consumers."""

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from errno import EACCES, EBADF, ELOOP, ENOENT, ENOTDIR
from functools import lru_cache
from importlib.machinery import SourceFileLoader
from pathlib import Path
from threading import current_thread, local, main_thread
from time import sleep
from types import ModuleType
from typing import Any, BinaryIO, Callable, Iterator, Mapping, Optional, Tuple, cast

_support_initialization = local()


@lru_cache(maxsize=1)
def _signal_support() -> ModuleType:
    # Source builds run before the Python package has installed metadata.
    pid = os.getpid()
    if getattr(_support_initialization, 'pid', None) == pid:
        raise RuntimeError('reentrant publication signal support initialization')
    try:
        _support_initialization.pid = pid
        source = Path(__file__).resolve().parents[1] / 'benchset' / '_signal_state.py'
        module = ModuleType('_benchset_build_signals')
        module.__file__ = str(source)
        SourceFileLoader(module.__name__, str(source)).exec_module(module)
        return module
    finally:
        _support_initialization.pid = None


_PublicationSignal = Tuple[ModuleType, Any]


def _handles_publication_sigint() -> bool:
    return current_thread() is main_thread() and signal.getsignal(signal.SIGINT) is signal.default_int_handler


def _publication_signal() -> _PublicationSignal:
    support = _signal_support()
    return support, support._native_sigaction()


def _filesystem_error(error: BaseException, *, include_audit: bool = True) -> bool:
    """Distinguish an OS operation's traceback from a caller signal during it."""
    return not _signal_support()._from_callback(
        BaseException.__dict__['__traceback__'].__get__(error), include_audit=include_audit,
    )


def _stat_if_present(read: Callable[[], os.stat_result]) -> Optional[os.stat_result]:
    try:
        return read()
    except OSError as error:
        if not _filesystem_error(error, include_audit=False):
            raise
        # Preserve Path.exists' legacy error policy. Python 3.14 treats
        # every OS stat error as absence.
        if sys.version_info < (3, 14) and not (
            error.errno in (ENOENT, ENOTDIR, EBADF, ELOOP) or getattr(error, 'winerror', None) in (21, 123, 1921)
        ):
            raise
        return None


def target_directory(root: Path, environment: Optional[Mapping[str, str]] = None) -> Path:
    """Resolve CARGO_TARGET_DIR against Cargo's working directory, or use target."""
    environment = os.environ if environment is None else environment
    return root / environment.get('CARGO_TARGET_DIR', 'target')


def build(root: Path, arguments: list, environment: Optional[Mapping[str, str]] = None) -> bytes:
    """Build native code and read its exact Rust-generated metadata snapshot."""
    result = subprocess.run([*arguments, '--message-format=json-render-diagnostics'], cwd=root,
                            env=environment, stdout=subprocess.PIPE, encoding='utf-8', check=True)
    paths = []
    for raw_line in result.stdout.split('\n'):
        line = raw_line.lstrip(' \t\r')
        if not line.startswith('{'):
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue  # A compiler wrapper can also write to stdout.
        output, variables = message.get('out_dir'), message.get('env')
        if message.get('reason') != 'build-script-executed' or not isinstance(output, str) or not isinstance(variables, list):
            continue
        for entry in variables:
            if isinstance(entry, list) and len(entry) == 2 and entry[0] == 'BENCHSET_METADATA_PATH_HEX':
                reported = Path(os.fsdecode(bytes.fromhex(entry[1])))
                # Cached build-script output can predate a target directory move.
                paths.append(Path(output) / reported.name)
    if len(paths) != 1:
        raise RuntimeError('Cargo did not report exactly one benchset metadata snapshot')
    return paths[0].read_bytes()


def _open_package_lock(path: Path) -> BinaryIO:
    if os.name == 'nt':
        return open(path, 'a+b')
    state = _stat_if_present(path.lstat)
    if state is not None:
        if stat.S_ISLNK(state.st_mode):
            raise OSError(f'invalid publication lock {path}')
        return open(path, 'r+b')
    try:
        return open(path, 'x+b')
    except FileExistsError as error:
        if not _filesystem_error(error):
            raise
        # Opening an existing lock must not create a dangling symlink's target.
        return open(path, 'r+b')


@contextmanager
def _package_lock(path: Path) -> Iterator[None]:
    """Keep the kernel lock owned by a file, including interrupted acquisition."""
    with _open_package_lock(path) as lock, ExitStack() as cleanup:
        if os.name == 'nt':
            import msvcrt  # noqa: PLC0415 — Platform-specific standard library.

            windows = cast(Any, msvcrt)
            while True:
                lock.seek(0)
                try:
                    windows.locking(lock.fileno(), windows.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if not _filesystem_error(error) or error.errno != EACCES:
                        raise
                    sleep(.05)
        else:
            import fcntl  # noqa: PLC0415 — Platform-specific standard library.

            opened = os.fstat(lock.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or not os.path.samestat(opened, path.lstat()):
                raise OSError(f'invalid publication lock {path}')
            try:
                os.fchmod(lock.fileno(), 0o666)
            except PermissionError as error:
                # Another UID may own the lock. A caller interruption must escape.
                if not _filesystem_error(error):
                    raise
            cleanup.callback(fcntl.flock, lock, fcntl.LOCK_UN)
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def publish(package: Path, library: Path, metadata: bytes) -> Path:
    """Serialize a package's complete publication, including its rollback snapshot."""
    requested = package
    package, library = package.absolute(), library.absolute()
    package.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=package.resolve()) as source_directory:
        # Resolve signals only after capturing the image paired with this metadata.
        image = Path(source_directory) / library.name
        shutil.copy2(library, image)
        policy = _publication_signal() if _handles_publication_sigint() else None
        while True:
            target = package.resolve()
            with _package_lock(target / '.benchset.publish.lock'):
                if policy is not None or not _handles_publication_sigint():
                    installed = _publish(target, image, metadata, policy)
                    break
            # The caller enabled default SIGINT while we acquired the lock.
            # Release it before symbol resolution, which can run audit hooks.
            policy = _publication_signal()
    try:
        return requested / installed.relative_to(target) if requested.samefile(target) else installed
    except OSError as error:
        if not _filesystem_error(error):
            raise
        return installed


def _rollback_publication(
    backup: Optional[Path], installed: Path, staged_library: Path, staged_metadata: Path,
) -> None:
    """Restore only an incomplete pair, even if the caller interrupts rollback."""
    for attempt in range(3):
        try:
            if _stat_if_present(staged_metadata.stat) is not None and _stat_if_present(staged_library.stat) is None:
                if backup is None:
                    installed.unlink(missing_ok=True)
                elif _stat_if_present(backup.stat) is not None:
                    backup.replace(installed)
            return
        except BaseException as error:
            # Caller signals and audit hooks can both add Python frames.
            if attempt == 2 or (
                issubclass(type(error), (OSError, TypeError, ValueError)) and _filesystem_error(error)
            ):
                raise


def _publish(package: Path, library: Path, metadata: bytes, policy: Optional[_PublicationSignal]) -> Path:
    """Stage both products and retain a complete generation after failed publication."""
    destination = package / '_native'
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination) as temporary:
        staged = Path(temporary)
        installed = destination / library.name
        backup = staged / 'previous-library' if _stat_if_present(installed.stat) is not None else None
        if backup is not None:
            shutil.copy2(installed, backup)
        staged_library = staged / library.name
        staged_metadata = staged / '_metadata.json'
        shutil.copy2(library, staged_library)
        staged_metadata.write_bytes(metadata)
        interrupted = False

        def interrupt(_signum: int, _frame: object) -> None:
            nonlocal interrupted
            interrupted = True

        # Check Ctrl+C between commits; finish any rollback before propagating it.
        handler: Any = signal.default_int_handler
        support, native = policy if policy is not None else (None, None)
        if support is not None and not _handles_publication_sigint():
            support = None
        try:
            if support is not None:
                selected = support._set_signal(signal.SIGINT, interrupt, native)
                if selected is not None:
                    handler = selected
            staged_library.replace(installed)
            if interrupted:
                raise KeyboardInterrupt
            staged_metadata.replace(package / '_metadata.json')
        except BaseException:
            _rollback_publication(backup, installed, staged_library, staged_metadata)
            raise
        finally:
            if support is not None:
                support._restore_sigint(interrupt, handler, native)
            if interrupted:
                raise KeyboardInterrupt
    return destination / library.name


# Load the metadata-free host adapter before any publication can hold a lock.
_signal_support()
