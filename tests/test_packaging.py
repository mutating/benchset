"""Package assembly must publish the current files, not stale staging contents."""

import os
import plistlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from scripts import package_native


@pytest.mark.parametrize('cargo_output', [None, 'relative-output', 'absolute-output'])
@pytest.mark.parametrize('configuration', [
    ('aarch64-apple-darwin', False, None),
    ('x86_64-unknown-linux-gnu', False, None),
    ('x86_64-pc-windows-msvc', False, None),
    ('aarch64-apple-darwin', True, None),
    *[('aarch64-apple-darwin', True, command) for command in
      ('cargo', 'second-cargo', 'install_name_tool', 'codesign', 'lipo')],
    ('x86_64-unknown-linux-gnu', True, 'unsupported-host'),
])
def test_package_rebuilds_current_native_and_swift_snapshots(  # noqa: PLR0915 - one assembly checked across its output formats
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configuration: tuple, cargo_output: Optional[str],
) -> None:
    """Rebuild portable archives from fresh target outputs and remove obsolete files.

    Distinct compiler outputs expose wrong-target or stale-library packaging.
    Fake compiler/linker commands leave the real copying, manifests and ZIP
    generation intact, so this covers repeat builds without requiring every SDK.
    Tool failures must propagate before replacing the affected published ZIP;
    an unsupported Apple host must be rejected before invoking Apple build tools.
    Every platform and universal archive must honor Cargo's selected output directory.
    """
    target, apple, failure = configuration
    project = tmp_path / 'project'
    project.mkdir()
    if cargo_output is None:
        monkeypatch.delenv('CARGO_TARGET_DIR', raising=False)
    else:
        monkeypatch.setenv('CARGO_TARGET_DIR', str(project / cargo_output) if cargo_output == 'absolute-output' else cargo_output)
    cargo_directory = project / (cargo_output or 'target')
    checkout = Path(__file__).resolve().parents[1]
    (project / 'pyproject.toml').write_text('[project]\nversion = "1.2.3"\n')
    (project / 'Cargo.toml').write_text('[package]\nname = "benchset"\nversion = "0.0.0"\n')
    (project / 'README.md').write_text('benchset = "0.0.0"\n.exact("0.0.0")\n')
    for name in ('LICENSE', 'Package.swift'):
        shutil.copy2(checkout / name, project / name)
    shutil.copytree(checkout / 'include', project / 'include')
    (project / 'native').mkdir()
    calibration = (checkout / 'native/reference-calibration.json').read_bytes()
    (project / 'native/reference-calibration.json').write_bytes(calibration)
    current = {
        'Sources/Benchset/Current.swift': b'public struct Current {}\n',
        'tests/swift/CurrentTests.swift': b'import Benchset\n',
    }
    for name, content in current.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    stale = [
        'release-artifacts/' + target + '/lib/obsolete-library',
        'release-artifacts/Benchset-Swift/Sources/Benchset/Obsolete.swift',
        'Artifacts/Benchset.xcframework/macos-arm64_x86_64/Headers/obsolete.h',
    ]
    for name in stale:
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'obsolete')
    builds: List[str] = []
    products: Dict[str, Dict[str, bytes]] = {}
    native_zip = project / 'release-artifacts' / ('benchset-' + target + '.zip')
    swift_zip = project / 'release-artifacts/Benchset-Swift.zip'
    native_zip.write_bytes(b'previous native archive')
    swift_zip.write_bytes(b'previous Swift archive')

    def run(arguments: List[str], **_options: Any) -> subprocess.CompletedProcess:
        selected = 'cargo' if failure == 'second-cargo' and builds else failure
        if arguments[0] == selected:
            if _options.get('check'):
                raise subprocess.CalledProcessError(7, arguments)
            return subprocess.CompletedProcess(arguments, 7)
        if arguments[0] == 'cargo':
            assert arguments[1] == 'build'
            assert '--locked' in arguments
            assert _options['cwd'] == project
            architecture = arguments[arguments.index('--target') + 1]
            builds.append(architecture)
            names = (['benchset.lib', 'benchset.dll', 'benchset.dll.lib'] if architecture.endswith('windows-msvc')
                     else ['libbenchset.a', 'libbenchset.dylib' if architecture.endswith('apple-darwin') else 'libbenchset.so'])
            output = cargo_directory / architecture / ('release' if '--release' in arguments else 'debug')
            output.mkdir(parents=True, exist_ok=True)
            products[architecture] = {name: (architecture + ':' + name).encode() for name in names}
            for name, data in products[architecture].items():
                (output / name).write_bytes(data)
        elif arguments[0] == 'lipo':
            end = arguments.index('-output')
            Path(arguments[end + 1]).write_bytes(b'universal\0' + b'\0'.join(Path(name).read_bytes() for name in arguments[2:end]))
        else:
            assert arguments[0] in ('install_name_tool', 'codesign')
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(package_native, 'ROOT', project)
    monkeypatch.setattr(package_native.subprocess, 'run', run)
    monkeypatch.setattr(sys, 'argv', ['package_native.py', '--target', target, *(['--apple'] if apple else [])])
    if apple:
        monkeypatch.setattr(sys, 'platform', 'linux' if failure == 'unsupported-host' else 'darwin')
    if failure:
        if failure == 'unsupported-host':
            with pytest.raises(SystemExit, match='2'):
                package_native.main()
        else:
            with pytest.raises(subprocess.CalledProcessError):
                package_native.main()
        assert swift_zip.read_bytes() == b'previous Swift archive'
        if failure in ('cargo', 'install_name_tool', 'codesign'):
            assert native_zip.read_bytes() == b'previous native archive'
        return
    package_native.main()

    with zipfile.ZipFile(project / 'release-artifacts' / ('benchset-' + target + '.zip')) as archive:
        assert set(archive.namelist()) == {
            *('lib/' + name for name in products[target]),
            *('include/' + path.name for path in (project / 'include').iterdir()),
            'README.md', 'LICENSE', 'native/reference-calibration.json',
        }
        for name, data in products[target].items():
            assert archive.read('lib/' + name) == data
        assert archive.read('include/benchset.h') == (project / 'include/benchset.h').read_bytes()
        assert archive.read('native/reference-calibration.json') == calibration
        assert archive.read('LICENSE') == (project / 'LICENSE').read_bytes()
        assert '1.2.3' in archive.read('README.md').decode()
    expected_targets = {'aarch64-apple-darwin', 'x86_64-apple-darwin'} if apple else {target}
    assert set(builds) == expected_targets
    if not apple:
        return
    with zipfile.ZipFile(project / 'release-artifacts/Benchset.xcframework.zip') as archive:
        info = plistlib.loads(archive.read('Benchset.xcframework/Info.plist'))
        assert info['CFBundlePackageType'] == 'XFWK'
        assert info['XCFrameworkFormatVersion'] == '1.0'
        libraries = info['AvailableLibraries']
        assert len(libraries) == 1
        library = libraries[0]
        assert library['SupportedPlatform'] == 'macos'
        assert set(library['SupportedArchitectures']) == {'arm64', 'x86_64'}
        base = 'Benchset.xcframework/' + library['LibraryIdentifier'] + '/'
        binary = archive.read(base + library['LibraryPath'])
        for architecture in expected_targets:
            assert products[architecture]['libbenchset.a'] in binary
        assert archive.read(base + library['HeadersPath'] + '/benchset.h') == (project / 'include/benchset.h').read_bytes()
        assert not any('obsolete' in name.lower() for name in archive.namelist())
    with zipfile.ZipFile(project / 'release-artifacts/Benchset-Swift.zip') as archive:
        for name, content in current.items():
            assert archive.read(name) == content
        assert archive.read('Package.swift') == (project / 'Package.swift').read_bytes()
        assert archive.read('native/reference-calibration.json') == calibration
        assert archive.read('README.md') == (project / 'README.md').read_bytes()
        assert archive.read('LICENSE') == (project / 'LICENSE').read_bytes()
        assert archive.read('Artifacts/' + base + library['LibraryPath']) == binary
        assert not any('obsolete' in name.lower() for name in archive.namelist())


@pytest.mark.parametrize(('script', 'options'), [
    ('package_native.py', ('--target', '--apple')),
    ('check_native_coverage.py', ('rust', 'swift', '--llvm-tools')),
])
def test_native_tool_entrypoints_show_usage_without_invoking_compilers(
    tmp_path: Path, script: str, options: tuple,
) -> None:
    """Keep standalone native tooling wired to its CLI before any build or gate runs.

    A removed main call can make the coverage CI job silently succeed without
    checking anything. Help must work outside the checkout with no tools on PATH.
    """
    path = Path(__file__).resolve().parents[1] / 'scripts' / script
    # Older interpreters still need the installed tomli backport. On 3.11+,
    # exclude site packages too, so an editable install cannot mask bad imports.
    isolation = ['-I', *(['-S'] if sys.version_info >= (3, 11) else [])]
    result = subprocess.run([sys.executable, *isolation, str(path), '--help'], cwd=tmp_path,
                            env=dict(os.environ, PATH=''), capture_output=True, text=True, check=False, timeout=10)
    assert result.returncode == 0
    assert not result.stderr
    assert 'usage:' in result.stdout
    assert all(option in result.stdout for option in options)
