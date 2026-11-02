"""Cargo output paths shared by build and release consumers."""

import json
import os
import shutil
import signal
import subprocess
import tempfile
from contextlib import ExitStack, contextmanager
from errno import EACCES
from pathlib import Path
from threading import current_thread, main_thread
from time import sleep
from typing import Iterator, Mapping, Optional


def target_directory(root: Path, environment: Optional[Mapping[str, str]] = None) -> Path:
    """Resolve CARGO_TARGET_DIR against Cargo's working directory, or use target."""
    environment = os.environ if environment is None else environment
    return root / environment.get('CARGO_TARGET_DIR', 'target')


def build(root: Path, arguments: list, environment: Optional[Mapping[str, str]] = None) -> bytes:
    """Build native code and read its exact Rust-generated metadata snapshot."""
    result = subprocess.run([*arguments, '--message-format=json-render-diagnostics'], cwd=root,
                            env=environment, stdout=subprocess.PIPE, encoding='utf-8', check=True)
    paths = [Path(os.fsdecode(bytes.fromhex(value))) for line in result.stdout.splitlines()
             for message in [json.loads(line)] if message.get('reason') == 'build-script-executed'
             for key, value in message['env'] if key == 'BENCHSET_METADATA_PATH_HEX']
    if len(paths) != 1:
        raise RuntimeError('Cargo did not report exactly one benchset metadata snapshot')
    return paths[0].read_bytes()


@contextmanager
def _package_lock(path: Path) -> Iterator[None]:
    """Keep the kernel lock owned by a file, including interrupted acquisition."""
    with path.open('a+b') as lock, ExitStack() as cleanup:
        if os.name == 'nt':
            import msvcrt  # noqa: PLC0415 — Platform-specific standard library.

            while True:
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if error.errno != EACCES:
                        raise
                    sleep(.05)
        else:
            import fcntl  # noqa: PLC0415 — Platform-specific standard library.

            try:
                os.fchmod(lock.fileno(), 0o666)
            except PermissionError:
                pass  # Another UID may own this writable persistent lock.
            cleanup.callback(fcntl.flock, lock, fcntl.LOCK_UN)
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def publish(package: Path, library: Path, metadata: bytes) -> Path:
    """Serialize a package's complete publication, including its rollback snapshot."""
    package.mkdir(parents=True, exist_ok=True)
    with _package_lock(package.resolve() / '.benchset.publish.lock'):
        return _publish(package, library, metadata)


def _publish(package: Path, library: Path, metadata: bytes) -> Path:
    """Stage both products and retain a complete generation after failed publication."""
    destination = package / '_native'
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination) as temporary:
        staged = Path(temporary)
        previous = staged / 'previous-library'
        installed = destination / library.name
        if installed.exists():
            shutil.copy2(installed, previous)
        shutil.copy2(library, staged / library.name)
        (staged / '_metadata.json').write_bytes(metadata)
        interrupted = False

        def interrupt(_signum: int, _frame: object) -> None:
            nonlocal interrupted
            interrupted = True

        # Check Ctrl+C between commits; finish any rollback before propagating it.
        handles_signals = (current_thread() is main_thread()
                           and signal.getsignal(signal.SIGINT) is signal.default_int_handler)
        handler = signal.signal(signal.SIGINT, interrupt) if handles_signals else None
        try:
            (staged / library.name).replace(installed)
            if interrupted:
                raise KeyboardInterrupt
            (staged / '_metadata.json').replace(package / '_metadata.json')
        except BaseException:
            # A rename may finish before SIGINT: roll back only an incomplete pair.
            if (staged / '_metadata.json').exists() and not (staged / library.name).exists():
                if previous.exists():
                    previous.replace(installed)
                else:
                    installed.unlink()
            raise
        finally:
            if handler is not None:
                signal.signal(signal.SIGINT, handler)
            if interrupted:
                raise KeyboardInterrupt
    return destination / library.name
