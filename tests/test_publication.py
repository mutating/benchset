"""A package's native library and Rust metadata must publish as one generation."""

from __future__ import annotations

import builtins
import os
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any, Iterator, List, Optional, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Each publisher is a separate interpreter, so an in-process lock cannot make
# these tests pass. Announcing lock acquisition is only a scheduling handshake:
# the assertions inspect the resulting files, never the lock's implementation.
# If no lock exists, B announces its completed publication before A is released,
# deterministically exposing the original B-library/A-metadata interleaving.
_PUBLISHER = r'''
import os
import sys
from pathlib import Path
from filelock import FileLock
from scripts import cargo
from scripts.cargo import publish

package, compiled, generation, pause, fail = sys.argv[1:]
package, compiled = Path(package), Path(compiled)
replace = Path.replace

report_lock = os.environ.get('BENCHSET_PUBLISH_REPORT_LOCK') == '1'
if pause == 'no' or report_lock:
    if hasattr(cargo, '_package_lock'):
        acquire = cargo._package_lock
        def announce_acquire(path):
            print('LOCK=' + str(path.resolve()) if report_lock else 'LOCK', flush=True)
            return acquire(path)
        cargo._package_lock = announce_acquire
    else:
        # Characterize the previous backend using the same process handshake.
        acquire = FileLock.acquire
        def announce_acquire(self, *args, **kwargs):
            print('LOCK=' + str(Path(self.lock_file).resolve()) if report_lock else 'LOCK', flush=True)
            return acquire(self, *args, **kwargs)
        FileLock.acquire = announce_acquire

def controlled_replace(source, destination):
    if source.name == '_metadata.json' and fail == 'yes':
        raise OSError('metadata rename failed for ' + generation)
    result = replace(source, destination)
    if source.name == compiled.name and pause == 'yes':
        print('LIBRARY', flush=True)
        assert sys.stdin.readline() == 'release\n'
    return result

Path.replace = controlled_replace
print('STARTED', flush=True)
try:
    publish(package, compiled, (generation + ' metadata').encode('ascii'))
except OSError as error:
    print('FAILED: ' + str(error), flush=True)
else:
    print('DONE', flush=True)
'''

_Child = Tuple['subprocess.Popen[str]', 'Queue[str]', List[str], Thread]


@contextmanager
def _publisher(  # noqa: PLR0913 — Independent process/environment fault controls.
    package: Path, compiled: Path, generation: str, *, pause: bool, fail: bool,
    temporary: Optional[Path] = None, report_lock: bool = False,
) -> Iterator[_Child]:
    environment = dict(os.environ)
    if temporary is not None:
        temporary.mkdir()
        environment.update(dict.fromkeys(('TMPDIR', 'TEMP', 'TMP'), str(temporary)))
    if report_lock:
        environment['BENCHSET_PUBLISH_REPORT_LOCK'] = '1'
    process = subprocess.Popen(
        [sys.executable, '-u', '-c', _PUBLISHER, str(package), str(compiled), generation,
         'yes' if pause else 'no', 'yes' if fail else 'no'],
        cwd=ROOT, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    messages: Queue[str] = Queue()
    lines: List[str] = []

    def read() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.append(line.rstrip('\n'))
            messages.put(line.rstrip('\n'))

    reader = Thread(target=read, daemon=True)
    reader.start()
    try:
        yield process, messages, lines, reader
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        reader.join(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _receive(child: _Child) -> str:
    process, messages, lines, _reader = child
    try:
        return messages.get(timeout=10)
    except Empty:
        pytest.fail('publisher stalled: status=%r, messages=%r' % (process.poll(), lines))


def _release(child: _Child) -> None:
    assert child[0].stdin is not None
    child[0].stdin.write('release\n')
    child[0].stdin.flush()


def _finish(child: _Child, *, failed: bool) -> None:
    process, _messages, lines, reader = child
    assert process.wait(timeout=10) == 0
    reader.join(timeout=10)
    assert process.stderr is not None
    assert not process.stderr.read()
    if failed:
        assert lines[-1].startswith('FAILED: metadata rename failed for ')
    else:
        assert lines[-1] == 'DONE'


def _seed(directory: Path, generation: str) -> Path:
    library = directory / '_native/libbenchset.so'
    library.parent.mkdir(parents=True)
    library.write_bytes((generation + ' library').encode('ascii'))
    (directory / '_metadata.json').write_bytes((generation + ' metadata').encode('ascii'))
    return library


def _compiled(directory: Path, generation: str) -> Path:
    path = directory / generation / 'libbenchset.so'
    path.parent.mkdir(parents=True)
    path.write_bytes((generation + ' library').encode('ascii'))
    return path


def _assert_generation(package: Path, generation: str) -> None:
    assert (package / '_native/libbenchset.so').read_bytes() == (generation + ' library').encode('ascii')
    assert (package / '_metadata.json').read_bytes() == (generation + ' metadata').encode('ascii')


@pytest.mark.parametrize(('fail_first', 'fail_second'), [(False, False), (True, False), (True, True)])
def test_concurrent_process_publishers_keep_a_complete_generation(
    tmp_path: Path, fail_first: bool, fail_second: bool,
) -> None:
    """Serialize the previous snapshot, staging, commit and rollback per package.

    A pauses immediately after installing its library. Without serialization B
    publishes fully; A then installs A metadata, or rolls back over B's library.
    Both leave mismatched files. Two failing publishers also cover taking the
    rollback snapshot before acquiring a lock: B must not snapshot A's library
    while A still has the old metadata and may roll back to the old generation.
    All inputs are immutable files; no Cargo/source-edit race is involved.
    """
    package = tmp_path / 'package'
    _seed(package, 'old')
    first, second = _compiled(tmp_path, 'A'), _compiled(tmp_path, 'B')
    with _publisher(package, first, 'A', pause=True, fail=fail_first) as a:
        assert _receive(a) == 'STARTED'
        assert _receive(a) == 'LIBRARY'
        with _publisher(package, second, 'B', pause=False, fail=fail_second) as b:
            assert _receive(b) == 'STARTED'
            boundary = _receive(b)
            assert boundary in {'LOCK', 'DONE'} or boundary.startswith('FAILED: ')
            _release(a)
            _finish(a, failed=fail_first)
            _finish(b, failed=fail_second)
    _assert_generation(package, 'old' if fail_second else 'B')


def test_publication_in_an_independent_package_does_not_wait_for_another_package(tmp_path: Path) -> None:
    """A held package lock must leave publication of unrelated packages available."""
    package_a, package_b = tmp_path / 'package-a', tmp_path / 'package-b'
    _seed(package_a, 'old')
    _seed(package_b, 'old')
    first, second = _compiled(tmp_path, 'A'), _compiled(tmp_path, 'B')
    with _publisher(package_a, first, 'A', pause=True, fail=False) as a:
        assert _receive(a) == 'STARTED'
        assert _receive(a) == 'LIBRARY'
        with _publisher(package_b, second, 'B', pause=False, fail=False) as b:
            assert _receive(b) == 'STARTED'
            boundary = _receive(b)
            if boundary == 'LOCK':
                boundary = _receive(b)
            assert boundary == 'DONE'
            _finish(b, failed=False)
            _release(a)
            _finish(a, failed=False)
    _assert_generation(package_a, 'A')
    _assert_generation(package_b, 'B')


def test_package_publication_uses_the_same_lock_with_different_temporary_directories(tmp_path: Path) -> None:
    """Per-process TMPDIR/TEMP/TMP must not separate one package's lock identity.

    If B announces a different lock, let B finish before releasing A to expose
    the exact mixed generation. Matching locks can immediately release A: its
    OS lock still prevents B from publishing until the complete pair is ready.
    """
    package = tmp_path / 'package'
    _seed(package, 'old')
    first, second = _compiled(tmp_path, 'A'), _compiled(tmp_path, 'B')
    with _publisher(package, first, 'A', pause=True, fail=False,
                    temporary=tmp_path / 'temp-a', report_lock=True) as a:
        assert _receive(a) == 'STARTED'
        first_lock = _receive(a)
        assert first_lock.startswith('LOCK=')
        assert _receive(a) == 'LIBRARY'
        with _publisher(package, second, 'B', pause=False, fail=False,
                        temporary=tmp_path / 'temp-b', report_lock=True) as b:
            assert _receive(b) == 'STARTED'
            second_lock = _receive(b)
            assert second_lock.startswith('LOCK=')
            if first_lock != second_lock:
                assert _receive(b) == 'DONE'
                _finish(b, failed=False)
            _release(a)
            _finish(a, failed=False)
            _finish(b, failed=False)
    _assert_generation(package, 'B')
    assert first_lock == second_lock


@pytest.mark.skipif(os.name != 'posix' or os.geteuid() == 0, reason='Requires non-root POSIX DAC permissions')
@pytest.mark.parametrize('mask', [0o022, 0o077])
def test_generated_publication_lock_remains_usable_by_an_authorized_other_writer(tmp_path: Path, mask: int) -> None:
    """The first writer must not make later package-owner publication impossible.

    A first build under another UID can leave its persistent lock foreign-owned,
    even though the package owner can replace every product through writable
    directories. Simulate that UID's effective access without privileges: after
    the real first publication, map its generated lock's OTHER permission bits
    onto our OWNER bits. Do not unlink the lock or change either output's modes.
    Both ordinary and restrictive umasks must permit another coherent publication
    using the same lock inode; readonly native images remain replaceable. Keep
    a readonly descriptor open so an unsafe unlink/recreate cannot reuse the
    just-freed inode number and make the identity assertion pass accidentally.
    """
    # Keep build tooling separate from the runtime-only imports above.
    from scripts.cargo import publish  # noqa: PLC0415

    package = tmp_path / 'package'
    installed = _seed(package, 'old')
    installed.chmod(0o444)
    first, second = _compiled(tmp_path, 'A'), _compiled(tmp_path, 'B')
    first.chmod(0o444)
    second.chmod(0o444)
    previous_mask = os.umask(mask)
    try:
        assert publish(package, first, b'A metadata') == installed
    finally:
        os.umask(previous_mask)
    _assert_generation(package, 'A')
    assert not installed.stat().st_mode & stat.S_IWUSR

    lock = package / '.benchset.publish.lock'
    generated = lock.stat()
    mode = stat.S_IMODE(generated.st_mode)
    lock.chmod((mode & 0o077) | ((mode & 0o007) << 6))

    with lock.open('rb') as identity:
        assert publish(package, second, b'B metadata') == installed
        _assert_generation(package, 'B')
        assert lock.stat().st_ino == os.fstat(identity.fileno()).st_ino == generated.st_ino
    assert not installed.stat().st_mode & stat.S_IWUSR


@pytest.mark.skipif(os.name != 'posix', reason='POSIX publication lock permissions')
@pytest.mark.parametrize('alias', ['symlink', 'hardlink', 'dangling', 'late-symlink', 'late-dangling'])
def test_publication_lock_does_not_change_permissions_through_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alias: str,
) -> None:
    """An aliased lock path must never create or make an unrelated file writable.

    Install the late symlink just as the path is opened to exercise validation
    of the opened file even if a pre-open pathname check succeeded.
    """
    from scripts.cargo import publish  # noqa: PLC0415

    package = tmp_path / 'package'
    package.mkdir()
    compiled = _compiled(tmp_path, 'new')
    victim = tmp_path / 'private'
    dangling = alias.endswith('dangling')
    if not dangling:
        victim.write_bytes(b'private contents')
        victim.chmod(0o600)
    lock = package / '.benchset.publish.lock'
    if alias == 'hardlink':
        os.link(victim, lock)
    elif alias.startswith('late-'):
        original = builtins.open

        def redirected(path: Path, *args: Any, **options: Any) -> Any:
            if path == lock and not path.is_symlink():
                path.symlink_to(victim)
            return original(path, *args, **options)

        monkeypatch.setattr(builtins, 'open', redirected)
    else:
        lock.symlink_to(victim)

    expected_error = FileNotFoundError if alias == 'late-dangling' else OSError
    with pytest.raises(expected_error):
        publish(package, compiled, b'new metadata')
    assert not (package / '_metadata.json').exists()
    if dangling:
        assert not victim.exists()
    else:
        assert stat.S_IMODE(victim.stat().st_mode) == 0o600
        assert victim.read_bytes() == b'private contents'
