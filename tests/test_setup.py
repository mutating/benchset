"""Wheel and editable-build contracts without invoking a compiler or setuptools CLI."""

from __future__ import annotations

import runpy
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import setuptools  # type: ignore[import-untyped]
from setuptools.command.build_py import build_py  # type: ignore[import-untyped]
from setuptools.command.develop import develop  # type: ignore[import-untyped]
from wheel.bdist_wheel import bdist_wheel  # type: ignore[import-untyped]

from scripts import version


@pytest.fixture
def setup_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """Load setup and retain its real command registry, isolating build outputs.

    Instantiating command classes directly conceals missing or misspelled
    registrations: pip would then invoke setuptools' ordinary Python commands.
    """
    distributions = []

    def register(**kwargs: Any) -> None:
        distribution_class = kwargs.pop('distclass', setuptools.Distribution)
        distributions.append(distribution_class(dict(kwargs, version='1.2.3')))

    monkeypatch.setattr(setuptools, 'setup', register)
    monkeypatch.setattr(version, 'sync_version', lambda _root, **_kwargs: '1.2.3')
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'setup.py'))
    namespace['distribution'] = distributions[0]
    globals_ = namespace['BuildNative'].run.__globals__
    monkeypatch.setitem(globals_, 'ROOT', tmp_path)
    return namespace


@pytest.mark.parametrize(('system', 'machine', 'target'), [
    ('darwin', 'arm64', 'aarch64-apple-darwin'),
    ('darwin', 'x86_64', 'x86_64-apple-darwin'),
    ('linux', 'aarch64', 'aarch64-unknown-linux-gnu'),
    ('linux', 'AMD64', 'x86_64-unknown-linux-gnu'),
    ('win32', 'AMD64', 'x86_64-pc-windows-msvc'),
    ('win32', 'arm64', None), ('freebsd', 'x86_64', None), ('linux', 'riscv64', None),
])
def test_wheel_target_matches_interpreter_and_rejects_unsupported_platforms(
    setup_module: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    system: str, machine: str, target: str | None,
) -> None:
    """Map host spellings to supported Rust triples before compiling a wheel.

    Unsupported CPUs/OSes and 32-bit interpreters must fail explicitly instead
    of silently shipping a library that the Python interpreter cannot load.
    """
    monkeypatch.setattr(setup_module['sys'], 'platform', system)
    monkeypatch.setattr(setup_module['platform'], 'machine', lambda: machine)
    monkeypatch.setattr(setup_module['struct'], 'calcsize', lambda _format: 8)
    if target is None:
        with pytest.raises(RuntimeError, match='Unsupported'):
            setup_module['rust_target']()
    else:
        assert setup_module['rust_target']() == target
    monkeypatch.setattr(setup_module['struct'], 'calcsize', lambda _format: 4)
    with pytest.raises(RuntimeError, match='64-bit'):
        setup_module['rust_target']()


@pytest.mark.parametrize('mode', [(editable, deployment, cargo_output) for editable in (False, True)
                                  for deployment in ('', '10.1', '14.0')
                                  for cargo_output in (None, 'relative-output', 'absolute-output')])
@pytest.mark.parametrize('configuration', [
    ('darwin', 'arm64', 'aarch64-apple-darwin', 'libbenchset.dylib'),
    ('darwin', 'x86_64', 'x86_64-apple-darwin', 'libbenchset.dylib'),
    ('linux', 'x86_64', 'x86_64-unknown-linux-gnu', 'libbenchset.so'),
    ('win32', 'AMD64', 'x86_64-pc-windows-msvc', 'benchset.dll'),
])
def test_native_build_installs_fresh_target_binary_in_wheel_or_editable_package(  # noqa: PLR0915 - one publication contract across build configurations
    setup_module: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    mode: tuple[bool, str, str | None], configuration: tuple[str, str, str, str],
) -> None:
    """Build the selected triple and copy its fresh bytes to the intended package.

    Stale libraries from another platform must disappear; non-library package
    data must survive. During copying, readers must still see the complete old
    library, even if copying fails. Both editable installs and wheel staging
    use this path; interrupted staging must leave no extra package files.
    Cargo's child environment controls the output path, including relative paths.
    """
    editable, deployment, cargo_output = mode
    system, machine, target, filename = configuration
    monkeypatch.setattr(setup_module['sys'], 'platform', system)
    monkeypatch.setattr(setup_module['platform'], 'machine', lambda: machine)
    monkeypatch.setattr(build_py, 'run', lambda _self: None)
    globals_ = setup_module['BuildNative'].run.__globals__
    environment = {'PATH': 'prepared-tools'}
    if cargo_output is not None:
        environment['CARGO_TARGET_DIR'] = str(tmp_path / cargo_output) if cargo_output == 'absolute-output' else cargo_output
    if deployment:
        environment['MACOSX_DEPLOYMENT_TARGET'] = deployment
    monkeypatch.setitem(globals_, 'rust_environment', lambda root, triple: environment.copy()
                        if (root, triple) == (tmp_path, target) else pytest.fail('wrong Rust target'))
    monkeypatch.setattr(setup_module['shutil'], 'which', lambda tool, **kwargs: '/prepared/cargo'
                        if tool == 'cargo' and kwargs['path'] == 'prepared-tools' else None)
    calls = []
    compiled = tmp_path / (cargo_output or 'target') / target / 'release' / filename

    def compile_(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        assert arguments == ['/prepared/cargo', 'build', '--release', '--locked', '--target', target]
        assert kwargs['cwd'] == tmp_path
        assert kwargs['check'] is True
        if system == 'darwin':
            minimum = '11.0' if machine == 'arm64' else '10.12'
            assert kwargs['env']['MACOSX_DEPLOYMENT_TARGET'] == ('14.0' if deployment == '14.0' else minimum)
        compiled.parent.mkdir(parents=True, exist_ok=True)
        compiled.write_bytes(b'fresh compiler output for ' + target.encode())
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(setup_module['subprocess'], 'run', compile_)
    command = setup_module['distribution'].get_command_obj('build_py')
    command.build_lib = str(tmp_path / 'wheel-staging')
    command.editable_mode = editable
    destination = (tmp_path if editable else Path(command.build_lib)) / 'benchset/_native'
    destination.mkdir(parents=True)
    for name in ('old.so', 'old.dll', 'old.dylib', 'metadata.txt'):
        (destination / name).write_text('keep only metadata')
    installed = destination / filename
    installed.write_bytes(b'previous native library')
    previous = installed.read_bytes()
    copy = setup_module['shutil'].copy2

    def copy_with_reader(source: Path, output: Path) -> Path:
        Path(output).write_bytes(b'partial copy')
        assert installed.read_bytes() == previous
        # Interrupt the second publication after writing only part of its file.
        if len(calls) == 2:
            raise OSError('copy interrupted')
        return copy(source, output)

    monkeypatch.setattr(setup_module['shutil'], 'copy2', copy_with_reader)
    command.run()
    assert (destination / filename).read_bytes() == compiled.read_bytes()
    assert (destination / 'metadata.txt').read_text() == 'keep only metadata'
    previous = installed.read_bytes()
    with pytest.raises(OSError, match='copy interrupted'):
        command.run()
    assert len(calls) == 2
    assert installed.read_bytes() == previous
    assert sorted(path.name for path in destination.iterdir()) == sorted([filename, 'metadata.txt'])


@pytest.mark.parametrize(('machine', 'old_tag', 'expected'), [
    ('arm64', 'macosx_10_9_universal2', 'macosx_11_0_arm64'),
    ('x86_64', 'macosx_10_9_universal2', 'macosx_10_12_x86_64'),
    ('arm64', 'macosx_14_0_arm64', 'macosx_14_0_arm64'),
    ('x86_64', 'macosx_14_0_x86_64', 'macosx_14_0_x86_64'),
])
def test_platform_wheel_is_python_independent_and_respects_macos_minimum(
    setup_module: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    machine: str, old_tag: str, expected: str,
) -> None:
    """Keep the native wheel loadable on its declared CPU/OS and every Python ABI.

    The tag may raise an old minimum, but must preserve a newer requested OS;
    declaring universal2 for a single-architecture binary is invalid.
    """
    monkeypatch.setattr(setup_module['sys'], 'platform', 'darwin')
    monkeypatch.setattr(setup_module['platform'], 'machine', lambda: machine)
    monkeypatch.setattr(bdist_wheel, 'get_tag', lambda _self: ('cp311', 'cp311', old_tag))
    distribution = setup_module['distribution']
    assert distribution.has_ext_modules() is True
    command = distribution.get_command_obj('bdist_wheel')
    assert command.get_tag() == ('py3', 'none', expected)


@pytest.mark.parametrize('metadata_version', ['1.2.2', '1.2.4'])
def test_native_build_refuses_metadata_from_another_package_version(
    setup_module: dict[str, Any], monkeypatch: pytest.MonkeyPatch, metadata_version: str,
) -> None:
    """Reject both older and newer metadata before building a mislabeled wheel."""
    monkeypatch.setattr(build_py, 'run', lambda _self: None)
    distribution = setup_module['distribution']
    distribution.metadata.version = metadata_version
    command = distribution.get_command_obj('build_py')
    with pytest.raises(RuntimeError, match='metadata does not match'):
        command.run()


@pytest.mark.parametrize('uninstall', [False, True])
def test_legacy_editable_install_builds_in_place_but_uninstall_does_not_compile(
    setup_module: dict[str, Any], monkeypatch: pytest.MonkeyPatch, uninstall: bool,
) -> None:
    """Preserve legacy pip's editable native build without rebuilding on removal."""
    calls: list[tuple[str, Any]] = []
    build = SimpleNamespace(editable_mode=False)
    command = setup_module['distribution'].get_command_obj('develop')
    command.uninstall = uninstall
    monkeypatch.setattr(command, 'reinitialize_command', lambda name: build if name == 'build_py'
                        else pytest.fail('wrong editable build command'))
    monkeypatch.setattr(command, 'run_command', lambda name: calls.append((name, build.editable_mode)))
    monkeypatch.setattr(develop, 'run', lambda _self: calls.append(('develop', None)))
    command.run()
    expected: list[tuple[str, Any]] = [('develop', None)] if uninstall else [('build_py', True), ('develop', None)]
    assert calls == expected
