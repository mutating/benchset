"""Filter the publication lock without changing registered installer copying."""

from __future__ import annotations

import os
import stat
from distutils.errors import DistutilsFileError
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest
from filelock import FileLock

from tests import test_setup

installer_setup = test_setup.setup_module


def _source(directory: Path) -> Tuple[Path, Dict[str, bytes]]:
    """Create complete opaque build outputs; no compiler or native load is needed."""
    contents = {
        'benchset/__init__.py': b'ANSWER = 42\n',
        'benchset/_metadata.json': b'{"generation": "complete"}\n',
        'benchset/_native/libbenchset.so': b'complete native image',
        'benchset/data/weights/table.bin': b'calibration payload',
    }
    for name, data in contents.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return directory, contents


def _command(setup: Dict[str, Any], source: Path, destination: object) -> Any:
    command = setup['distribution'].get_command_obj('install_lib')
    command.build_dir, command.install_dir = str(source), destination
    return command


def _link(path: Path, target: Path, *, directory: bool) -> None:
    """Skip only platforms that cannot create the required symbolic link."""
    try:
        path.symlink_to(target, target_is_directory=directory)
    except NotImplementedError:
        pytest.skip('Symbolic links are unavailable')
    except OSError as error:
        if os.name == 'nt' and getattr(error, 'winerror', None) == 1314:
            pytest.skip('Creating Windows symbolic links requires privileges')
        raise


@pytest.mark.parametrize('linked', ['package', 'native', 'nested'])
@pytest.mark.parametrize('spelling', ['plain', 'dot'])
def test_registered_installer_follows_directory_links_and_reports_every_file(
    installer_setup: Dict[str, Any], tmp_path: Path, linked: str, spelling: str,
) -> None:
    """Directory links remain usable build outputs, including native products.

    The normal setuptools copier followed directory links. Activating its
    archive copier merely to exclude a lock silently omits these directories,
    yielding missing native code or even an entirely absent Python package.
    Exercise the registered command, both deep and top-level links, and raw
    destination spelling rather than depending on a particular copy backend.
    """
    source, contents = _source(tmp_path / 'build')
    relative = {'package': 'benchset', 'native': 'benchset/_native',
                'nested': 'benchset/data/weights'}[linked]
    original = source / relative
    actual = tmp_path / 'actual-products'
    original.rename(actual)
    _link(original, actual, directory=True)
    output = tmp_path / 'installed'
    raw = str(output) if spelling == 'plain' else str(tmp_path) + '/./installed'
    command = _command(installer_setup, source, raw)
    returned = command.install()
    assert {os.path.relpath(path, raw) for path in returned} == set(contents)
    for name, data in contents.items():
        installed = output / name
        assert installed.is_file()
        assert not installed.is_symlink()
        assert installed.read_bytes() == data


@pytest.mark.parametrize('force', [False, True])
@pytest.mark.parametrize('destination_age', ['older', 'equal', 'newer'])
def test_registered_installer_retains_force_and_timestamp_update_semantics(
    installer_setup: Dict[str, Any], tmp_path: Path, force: bool, destination_age: str,
) -> None:
    """Without force, equal or newer installed data must not be overwritten.

    An older destination is updated and force overrides all timestamp cases.
    The returned output list still includes a file whose copy was skipped,
    preserving setuptools' reporting contract for incremental installations.
    """
    source, _contents = _source(tmp_path / 'build')
    original = source / 'benchset/__init__.py'
    output = tmp_path / 'installed'
    destination = output / 'benchset/__init__.py'
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b'newer local data')
    timestamp = 1_600_000_000
    offset = {'older': -10, 'equal': 0, 'newer': 10}[destination_age]
    os.utime(original, (timestamp, timestamp))
    os.utime(destination, (timestamp + offset, timestamp + offset))
    command = _command(installer_setup, source, str(output))
    command.force = force
    returned = command.install()
    copied = force or destination_age == 'older'
    assert destination.read_bytes() == (original.read_bytes() if copied else b'newer local data')
    assert destination.stat().st_mtime == (timestamp if copied else timestamp + offset)
    assert str(destination) in returned


@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('force', [False, True])
def test_registered_installer_dry_run_reports_outputs_without_writing(
    installer_setup: Dict[str, Any], tmp_path: Path, existing: bool, force: bool,
) -> None:
    """A dry run predicts files while preserving existing data and directories.

    Supported older setuptools honored this flag before lock filtering selected
    an archive copier that writes unconditionally. Preserve the intended dry-run
    contract on newer setuptools too, including force=True and a fresh target.
    """
    source, contents = _source(tmp_path / 'build')
    output = tmp_path / 'installed'
    preserved = output / 'keep.txt'
    if existing:
        output.mkdir()
        preserved.write_bytes(b'caller data')
    before = sorted(str(path.relative_to(output)) for path in output.rglob('*')) if existing else []
    command = _command(installer_setup, source, str(output))
    command.dry_run, command.force = True, force
    returned = command.install()
    assert {os.path.relpath(path, output) for path in returned} == set(contents)
    assert output.exists() is existing
    assert sorted(str(path.relative_to(output)) for path in output.rglob('*')) == before
    if existing:
        assert preserved.read_bytes() == b'caller data'


def test_registered_installer_skips_nfs_tombstones_at_every_depth(
    installer_setup: Dict[str, Any], tmp_path: Path,
) -> None:
    """Transient NFS files and directories must not become installed resources.

    The original copier skipped .nfs* entries while copying normal hidden data.
    Keep that behavior when walking linked native-product directories as well.
    """
    source, contents = _source(tmp_path / 'build')
    for name in ('.nfs-root', 'benchset/_native/.nfs-library', 'benchset/.nfs-dir/stale'):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'open deleted file')
    normal = source / 'benchset/.keep'
    normal.write_bytes(b'ordinary hidden resource')
    contents['benchset/.keep'] = normal.read_bytes()
    output = tmp_path / 'installed'
    returned = _command(installer_setup, source, str(output)).install()
    assert {os.path.relpath(path, output) for path in returned} == set(contents)
    assert {str(path.relative_to(output)) for path in output.rglob('*') if path.is_file()} == set(contents)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX file and directory permission semantics')
@pytest.mark.parametrize('existing', [False, True])
def test_registered_installer_preserves_file_metadata_and_existing_directory_modes(
    installer_setup: Dict[str, Any], tmp_path: Path, existing: bool,
) -> None:
    """File modes/mtimes survive copying without inheriting source directory modes.

    Reusing command copy helpers preserves executable and read-only files, keeps
    an existing installation directory's permissions, and honors the creation
    umask for new directories. This rejects an unfiltered copytree replacement.
    """
    source, contents = _source(tmp_path / 'build')
    (source / 'benchset').chmod(0o770)
    modes = {'benchset/__init__.py': 0o640, 'benchset/_native/libbenchset.so': 0o444}
    timestamp = 1_600_000_000
    for name, mode in modes.items():
        path = source / name
        path.chmod(mode)
        os.utime(path, (timestamp, timestamp))
    output = tmp_path / 'installed'
    package = output / 'benchset'
    if existing:
        package.mkdir(parents=True)
        package.chmod(0o705)
    mask = os.umask(0o022)
    try:
        _command(installer_setup, source, str(output)).install()
    finally:
        os.umask(mask)
    assert stat.S_IMODE(package.stat().st_mode) == (0o705 if existing else 0o755)
    for name, mode in modes.items():
        copied = output / name
        assert copied.read_bytes() == contents[name]
        assert stat.S_IMODE(copied.stat().st_mode) == mode
        assert copied.stat().st_mtime == timestamp


def test_registered_installer_keeps_empty_directories_and_dereferences_file_links(
    installer_setup: Dict[str, Any], tmp_path: Path,
) -> None:
    """Tree copying retains empty directories and copies file-link contents.

    A filter must not drop directories with no regular files or turn a source
    file symlink into an installed symlink when preserve_symlinks is false.
    """
    source, _contents = _source(tmp_path / 'build')
    (source / 'benchset/empty').mkdir()
    link = source / 'benchset/linked.bin'
    _link(link, source / 'benchset/data/weights/table.bin', directory=False)
    output = tmp_path / 'installed'
    _command(installer_setup, source, str(output)).install()
    assert (output / 'benchset/empty').is_dir()
    copied = output / 'benchset/linked.bin'
    assert copied.read_bytes() == b'calibration payload'
    assert not copied.is_symlink()


@pytest.mark.parametrize('kind', ['missing', 'file'])
def test_registered_tree_copy_rejects_a_non_directory_input(
    installer_setup: Dict[str, Any], tmp_path: Path, kind: str,
) -> None:
    """Invalid copy roots raise the normal distutils error rather than succeeding.

    os.walk alone yields no files for these inputs. Keep the original explicit
    directory validation and leave the proposed installation target untouched.
    """
    source = tmp_path / 'source'
    if kind == 'file':
        source.write_bytes(b'not a directory')
    output = tmp_path / 'installed'
    command = _command(installer_setup, source, str(output))
    with pytest.raises(DistutilsFileError, match='not a directory'):
        command.copy_tree(str(source), str(output))
    assert not output.exists()


@pytest.mark.parametrize('kind', ['missing', 'file'])
def test_registered_tree_dry_run_tolerates_an_unavailable_input(
    installer_setup: Dict[str, Any], tmp_path: Path, kind: str,
) -> None:
    """A direct dry-run copy treats missing or non-directory input as empty.

    The original copier could predict no outputs before a build had produced
    its directory. Real copying must still reject the same invalid roots.
    """
    source = tmp_path / 'source'
    if kind == 'file':
        source.write_bytes(b'not a directory')
    output = tmp_path / 'installed'
    command = _command(installer_setup, source, str(output))
    command.dry_run = True
    assert command.copy_tree(str(source), str(output)) == []
    assert not output.exists()


@pytest.mark.skipif(os.name != 'posix' or getattr(os, 'geteuid', lambda: 0)() == 0,
                    reason='POSIX DAC permissions must be enforced for the test user')
def test_registered_installer_reports_an_unreadable_subdirectory(
    installer_setup: Dict[str, Any], tmp_path: Path,
) -> None:
    """Unreadable input must fail visibly instead of installing a partial tree.

    Archive os.walk silently discarded scan errors. A native or package-data
    directory that cannot be enumerated must retain the usual distutils error.
    """
    source, _contents = _source(tmp_path / 'build')
    protected = source / 'benchset/private'
    protected.mkdir()
    (protected / 'data.bin').write_bytes(b'unreadable data')
    protected.chmod(0)
    output = tmp_path / 'installed'
    try:
        with pytest.raises(DistutilsFileError, match='error listing files'):
            _command(installer_setup, source, str(output)).install()
    finally:
        protected.chmod(0o700)


@pytest.mark.skipif(os.name != 'posix' or getattr(os, 'geteuid', lambda: 0)() == 0,
                    reason='POSIX DAC permissions must be enforced for the test user')
@pytest.mark.parametrize('method', ['copy_tree', 'install'])
@pytest.mark.parametrize('blocked', ['root', 'nested'])
def test_registered_installer_dry_run_skips_unreadable_directories(
    installer_setup: Dict[str, Any], tmp_path: Path, method: str, blocked: str,
) -> None:
    """Dry runs ignore scan errors while retaining every readable planned file.

    An unreadable root predicts no files; an unreadable child must not discard
    readable siblings. Exercise direct copying and the registered installation
    path without weakening the real-copy error contract or creating outputs.
    """
    source, contents = _source(tmp_path / 'build')
    protected = source if blocked == 'root' else source / 'benchset/private'
    protected.mkdir(exist_ok=True)
    (protected / 'hidden.bin').write_bytes(b'inaccessible data')
    protected.chmod(0)
    output = tmp_path / 'installed'
    command = _command(installer_setup, source, str(output))
    command.dry_run = True
    try:
        returned = command.copy_tree(str(source), str(output)) if method == 'copy_tree' else command.install()
    finally:
        protected.chmod(0o700)
    assert {os.path.relpath(path, output) for path in returned} == (set() if blocked == 'root' else set(contents))
    assert not output.exists()


@pytest.mark.parametrize('spelling', ['plain', 'dot'])
def test_registered_installer_excludes_publication_locks_without_touching_live_inodes(
    installer_setup: Dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str,
) -> None:
    """Filtering skips only the publication lock before copying or removing it.

    Hold both source and existing destination locks. Neither live inode nor its
    timestamp may be changed by installation; ordinary other-package lock data
    must still be copied. A post-copy unlink would break the destination owner.
    Also inspect paths during each file copy, so temporarily renaming a source
    lock cannot hide behind otherwise identical before/after inode snapshots.
    """
    source, contents = _source(tmp_path / 'build')
    other = source / 'other/.benchset.publish.lock'
    other.parent.mkdir()
    other.write_bytes(b'other package data')
    contents['other/.benchset.publish.lock'] = other.read_bytes()
    output = tmp_path / 'installed'
    destination = output / 'benchset/.benchset.publish.lock'
    destination.parent.mkdir(parents=True)
    original = source / 'benchset/.benchset.publish.lock'
    raw = str(output) if spelling == 'plain' else str(tmp_path) + '/./installed'
    with FileLock(str(original)), FileLock(str(destination)):
        os.utime(original, (1_600_000_000, 1_600_000_000))
        os.utime(destination, (1_600_000_010, 1_600_000_010))

        def states() -> list[tuple[int, int]]:
            return [(path.stat().st_ino, path.stat().st_mtime_ns) for path in (original, destination)]

        before = states()
        command = _command(installer_setup, source, raw)
        command.force = True
        copy = command.copy_file

        def observe(*arguments: Any, **options: Any) -> Any:
            assert states() == before
            return copy(*arguments, **options)

        monkeypatch.setattr(command, 'copy_file', observe)
        returned = command.install()
        assert states() == before
        assert {os.path.relpath(path, raw) for path in returned} == set(contents)
        for name, data in contents.items():
            assert (output / name).read_bytes() == data


def test_registered_installer_retains_inherited_namespace_exclusions(
    installer_setup: Dict[str, Any], tmp_path: Path,
) -> None:
    """The lock filter keeps setuptools' existing namespace-package exclusions.

    Replacing the copying backend must not reinstall namespace __init__ files
    while excluding the new lock. Other namespace resources remain available.
    """
    source, _contents = _source(tmp_path / 'build')
    namespace = source / 'legacy_namespace'
    namespace.mkdir()
    (namespace / '__init__.py').write_bytes(b'namespace marker')
    (namespace / 'data.bin').write_bytes(b'namespace resource')
    distribution = installer_setup['distribution']
    distribution.namespace_packages = ['legacy_namespace']
    install = distribution.get_command_obj('install')
    install.single_version_externally_managed = True
    install.record = str(tmp_path / 'installed-files.txt')
    output = tmp_path / 'installed'
    returned = _command(installer_setup, source, str(output)).install()
    assert not (output / 'legacy_namespace/__init__.py').exists()
    assert str(output / 'legacy_namespace/__init__.py') not in returned
    assert (output / 'legacy_namespace/data.bin').read_bytes() == b'namespace resource'
