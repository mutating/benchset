"""Source-install bootstrap: private environments, caching and verified downloads."""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Dict, List, Tuple

import pytest

from scripts import build_backend as backend

TARGET = 'aarch64-apple-darwin'


def project(tmp_path: Path) -> Path:
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'rust-toolchain.toml').write_text('[toolchain]\nchannel = "1.94.1"\n')
    return root


def cached_tools(cache: Path, target: str = TARGET) -> List[Path]:
    suffix = '.exe' if target.endswith('windows-msvc') else ''
    location = cache / '1.94.1' / target
    return [location / 'cargo/bin' / ('cargo' + suffix),
            location / 'rustup/toolchains' / ('1.94.1-' + target) / 'bin' / ('rustc' + suffix)]


def ready(cache: Path, target: str = TARGET) -> None:
    for path in cached_tools(cache, target):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'test executable')


def no_cargo(monkeypatch: pytest.MonkeyPatch, cache: Path) -> None:
    monkeypatch.setattr(backend.shutil, 'which', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backend, 'cache_directory', lambda _environment: cache)


def test_available_cargo_requires_no_network_or_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    before = dict(os.environ)
    monkeypatch.setattr(backend.shutil, 'which', lambda *_args, **_kwargs: '/bin/cargo')
    environment = backend.rust_environment(Path('missing source'), TARGET)
    assert environment == before
    assert environment is not os.environ


@pytest.mark.parametrize('configuration', [
    ('darwin', '', '', 'Library/Caches'),
    ('linux', 'XDG_CACHE_HOME', '/custom/cache', '.cache'),
    ('win32', 'LOCALAPPDATA', '/custom/cache', 'AppData/Local'),
])
def test_platform_cache_location(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                 configuration: Tuple[str, str, str, str]) -> None:
    system, variable, configured, fallback = configuration
    monkeypatch.setattr(backend.sys, 'platform', system)
    monkeypatch.setattr(backend.Path, 'home', lambda: tmp_path)
    environment = {variable: configured} if variable else {}
    expected = Path(configured) if configured else tmp_path / fallback
    assert backend.cache_directory(environment) == expected / 'benchset/rust'
    assert backend.cache_directory({}) == tmp_path / fallback / 'benchset/rust'


@pytest.mark.parametrize('channel', ['stable', 'nightly', '1.94', '../unsafe', ''])
def test_invalid_pin_fails_before_downloading(tmp_path: Path, channel: str) -> None:
    root = project(tmp_path)
    (root / 'rust-toolchain.toml').write_text('[toolchain]\nchannel = "' + channel + '"\n')
    with pytest.raises(RuntimeError, match='exact Rust version'):
        backend.pinned_version(root)


@pytest.mark.parametrize('target', [TARGET, 'x86_64-apple-darwin', 'x86_64-unknown-linux-gnu',
                                   'aarch64-unknown-linux-gnu', 'x86_64-pc-windows-msvc'])
def test_bootstrap_uses_private_pinned_environment_and_reuses_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: str,
) -> None:
    root, cache = project(tmp_path), tmp_path / 'cache'
    no_cargo(monkeypatch, cache)
    before = dict(os.environ)
    calls: List[List[str]] = []
    downloads: List[str] = []

    def download(host: str, destination: Path) -> None:
        downloads.append(host)
        destination.write_bytes(b'installer')

    def run(command: List[str], *, cwd: Path, env: Dict[str, str], check: bool) -> subprocess.CompletedProcess[str]:
        assert check
        assert cwd == root
        assert Path(env['CARGO_HOME']) == cache / '1.94.1' / target / 'cargo'
        assert Path(env['RUSTUP_HOME']) == cache / '1.94.1' / target / 'rustup'
        assert env['RUSTUP_TOOLCHAIN'] == '1.94.1-' + target
        assert env['PATH'].split(os.pathsep)[0] == str(cache / '1.94.1' / target / 'cargo/bin')
        calls.append(command)
        if '--no-modify-path' in command:
            assert command[command.index('--default-toolchain') + 1] == '1.94.1'
            assert command[command.index('--default-host') + 1] == target
            assert command[command.index('--profile') + 1] == 'minimal'
            assert env['RUSTUP_INIT_SKIP_PATH_CHECK'] == 'yes'
            ready(cache, target)
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(backend, 'download_installer', download)
    monkeypatch.setattr(backend.subprocess, 'run', run)
    environment = backend.rust_environment(root, target)
    assert os.environ == before
    assert backend.rust_environment(root, target) == environment
    assert downloads == [target]
    assert len(calls) == 3  # installer + two health checks, no second install
    assert not list((cache / '1.94.1' / target).glob('installer-*'))
    assert 'RUSTUP_INIT_SKIP_PATH_CHECK' not in environment or 'RUSTUP_INIT_SKIP_PATH_CHECK' in before


@pytest.mark.parametrize('missing', [0, 1])
def test_partial_cache_is_repaired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing: int) -> None:
    root, cache = project(tmp_path), tmp_path / 'cache'
    no_cargo(monkeypatch, cache)
    ready(cache)
    cached_tools(cache)[missing].unlink()
    installed: List[bool] = []
    monkeypatch.setattr(backend, 'download_installer', lambda _target, path: path.write_bytes(b'installer'))

    def run(command: List[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if '--no-modify-path' in command:
            installed.append(True)
            ready(cache)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(backend.subprocess, 'run', run)
    backend.rust_environment(root, TARGET)
    assert installed == [True]


def test_install_failure_cleans_temporary_files_and_can_retry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root, cache = project(tmp_path), tmp_path / 'cache'
    no_cargo(monkeypatch, cache)
    before = dict(os.environ)
    monkeypatch.setattr(backend, 'download_installer', lambda _target, path: path.write_bytes(b'installer'))

    def fail(command: List[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(backend.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        backend.rust_environment(root, TARGET)
    assert os.environ == before
    assert not list((cache / '1.94.1' / TARGET).glob('installer-*'))
    ready(cache)
    monkeypatch.setattr(backend.subprocess, 'run', lambda *args, **_kwargs: subprocess.CompletedProcess(args[0], 0))
    backend.rust_environment(root, TARGET)


def test_installer_success_without_tools_is_not_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root, cache = project(tmp_path), tmp_path / 'cache'
    no_cargo(monkeypatch, cache)
    monkeypatch.setattr(backend, 'download_installer', lambda _target, path: path.write_bytes(b'installer'))
    monkeypatch.setattr(backend.subprocess, 'run', lambda *args, **_kwargs: subprocess.CompletedProcess(args[0], 0))
    with pytest.raises(RuntimeError, match='did not create'):
        backend.rust_environment(root, TARGET)


@pytest.mark.parametrize('target', [TARGET, 'x86_64-pc-windows-msvc'])
def test_download_verifies_checksum_before_executable_permissions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: str) -> None:
    content = b'installer binary' * 100
    checksum = hashlib.sha256(content).hexdigest().encode()
    urls: List[str] = []

    def request(url: str, *, timeout: int) -> io.BytesIO:
        assert timeout == 120
        urls.append(url)
        return io.BytesIO(checksum + b'  rustup-init\n' if url.endswith('.sha256') else content)
    monkeypatch.setattr(backend, 'urlopen', request)
    path = tmp_path / 'installer'
    backend.download_installer(target, path)
    assert path.read_bytes() == content
    assert urls[0] == urls[1] + '.sha256'
    assert urls[1].endswith('rustup-init.exe' if target.endswith('windows-msvc') else 'rustup-init')
    if os.name != 'nt':
        assert path.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize('checksum', [b'', b'not a hash', b'0' * 64])
def test_bad_download_cannot_be_executed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, checksum: bytes) -> None:
    monkeypatch.setattr(backend, 'urlopen', lambda url, **_kwargs: io.BytesIO(checksum if url.endswith('.sha256') else b'untrusted'))
    path = tmp_path / 'installer'
    with pytest.raises(RuntimeError, match='checksum'):
        backend.download_installer(TARGET, path)
    if path.exists() and os.name != 'nt':
        assert not path.stat().st_mode & 0o111


def test_concurrent_builds_install_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root, cache = project(tmp_path), tmp_path / 'cache'
    no_cargo(monkeypatch, cache)
    installing, release = Event(), Event()
    downloads: List[str] = []

    def download(target: str, path: Path) -> None:
        downloads.append(target)
        path.write_bytes(b'installer')

    def run(command: List[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if '--no-modify-path' in command:
            installing.set()
            assert release.wait(5)
            ready(cache)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(backend, 'download_installer', download)
    monkeypatch.setattr(backend.subprocess, 'run', run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(backend.rust_environment, root, TARGET)
        try:
            assert installing.wait(5)
            second = pool.submit(backend.rust_environment, root, TARGET)
        finally:
            release.set()
        results = [first.result(timeout=5), second.result(timeout=5)]
    assert results[0] == results[1]
    assert downloads == [TARGET]
