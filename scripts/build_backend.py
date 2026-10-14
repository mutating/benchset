"""PEP 517 backend and isolated Rust bootstrap for source installations."""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict
from urllib.request import urlopen

from filelock import FileLock
from setuptools import build_meta  # type: ignore[import-untyped]

build_editable = build_meta.build_editable
build_sdist = build_meta.build_sdist
build_wheel = build_meta.build_wheel
get_requires_for_build_editable = build_meta.get_requires_for_build_editable
get_requires_for_build_sdist = build_meta.get_requires_for_build_sdist
get_requires_for_build_wheel = build_meta.get_requires_for_build_wheel
prepare_metadata_for_build_editable = build_meta.prepare_metadata_for_build_editable
prepare_metadata_for_build_wheel = build_meta.prepare_metadata_for_build_wheel


def cache_directory(environment: Dict[str, str]) -> Path:
    if sys.platform == 'win32':
        base = Path(environment.get('LOCALAPPDATA') or Path.home() / 'AppData/Local')
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library/Caches'
    else:
        base = Path(environment.get('XDG_CACHE_HOME') or Path.home() / '.cache')
    return base / 'benchset/rust'


def pinned_version(root: Path) -> str:
    content = (root / 'rust-toolchain.toml').read_text()
    match = re.search(r'^channel\s*=\s*"(\d+\.\d+\.\d+)"\s*$', content, re.M)
    if match is None:
        raise RuntimeError('rust-toolchain.toml must pin an exact Rust version')
    return match.group(1)


def download_installer(target: str, destination: Path) -> None:
    suffix = '.exe' if target.endswith('windows-msvc') else ''
    url = 'https://static.rust-lang.org/rustup/dist/' + target + '/rustup-init' + suffix
    with urlopen(url + '.sha256', timeout=120) as response:
        checksum_fields = response.read().decode('ascii').split()
    checksum = checksum_fields[0] if checksum_fields else ''
    if re.fullmatch(r'[0-9a-f]{64}', checksum) is None:
        raise RuntimeError('Rust installer checksum is invalid')
    digest = hashlib.sha256()
    with urlopen(url, timeout=120) as response, destination.open('wb') as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != checksum:
        raise RuntimeError('Rust installer checksum mismatch; refusing to execute it')
    destination.chmod(0o700)


def rust_environment(root: Path, target: str) -> Dict[str, str]:
    """Use Cargo on PATH, otherwise bootstrap the pinned SDK in a user cache.

    Only child processes receive the extra environment. rustup's no-modify-path
    mode leaves shell profiles and the Windows user PATH alone. Each version /
    host pair is locked separately so concurrent pip builds cannot corrupt it.
    """
    environment = os.environ.copy()
    if shutil.which('cargo', path=environment.get('PATH')):
        return environment
    version = pinned_version(root)
    location = cache_directory(environment) / version / target
    location.mkdir(parents=True, exist_ok=True)
    cargo_home = location / 'cargo'
    rustup_home = location / 'rustup'
    executable = 'cargo.exe' if target.endswith('windows-msvc') else 'cargo'
    cargo = cargo_home / 'bin' / executable
    environment.update({
        'CARGO_HOME': str(cargo_home),
        'RUSTUP_HOME': str(rustup_home),
        'RUSTUP_TOOLCHAIN': version + '-' + target,
        'PATH': str(cargo_home / 'bin') + os.pathsep + environment.get('PATH', ''),
    })
    with FileLock(str(location / 'install.lock'), timeout=1800):
        compiler = rustup_home / 'toolchains' / (version + '-' + target) / 'bin' / (
            'rustc.exe' if target.endswith('windows-msvc') else 'rustc'
        )
        if not cargo.is_file() or not compiler.is_file():
            print('Preparing Rust ' + version + ' in ' + str(location), flush=True)
            with tempfile.TemporaryDirectory(prefix='installer-', dir=location) as temporary:
                installer = Path(temporary) / ('rustup-init.exe' if executable.endswith('.exe') else 'rustup-init')
                download_installer(target, installer)
                installer_environment = dict(environment, RUSTUP_INIT_SKIP_PATH_CHECK='yes')
                subprocess.run([
                    str(installer), '-y', '--no-modify-path', '--profile', 'minimal',
                    '--default-host', target, '--default-toolchain', version,
                ], cwd=root, env=installer_environment, check=True)
            if not cargo.is_file() or not compiler.is_file():
                raise RuntimeError('Rust bootstrap did not create Cargo and rustc')
        # Detect incomplete/corrupt installations before the actual build.
        subprocess.run([str(cargo), '--version'], cwd=root, env=environment, check=True)
    return environment
