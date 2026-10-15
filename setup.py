"""Build an interpreter-independent platform wheel containing the Rust CDylib."""
import platform
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

from setuptools import Distribution, setup
from setuptools.command.build_py import build_py
from wheel.bdist_wheel import bdist_wheel

from scripts.build_backend import rust_environment

ROOT = Path(__file__).parent.resolve()


def rust_target():
    if struct.calcsize('P') != 8:
        raise RuntimeError('benchset requires a 64-bit Python interpreter')
    machine = platform.machine().lower()
    if machine in ('arm64', 'aarch64'):
        architecture = 'aarch64'
    elif machine in ('amd64', 'x86_64'):
        architecture = 'x86_64'
    else:
        raise RuntimeError('Unsupported Python architecture: ' + machine)
    systems = {'darwin': 'apple-darwin', 'linux': 'unknown-linux-gnu', 'win32': 'pc-windows-msvc'}
    if sys.platform not in systems or (sys.platform == 'win32' and architecture != 'x86_64'):
        raise RuntimeError('Unsupported platform for prebuilt benchset: ' + sys.platform)
    return architecture + '-' + systems[sys.platform]


class NativeDistribution(Distribution):
    def has_ext_modules(self):
        return True


class BuildNative(build_py):
    def run(self):
        super().run()
        native_version = re.search(r'^version\s*=\s*[\'"]([^\'"]+)', (ROOT / 'Cargo.toml').read_text(), re.M).group(1)
        if native_version != self.distribution.get_version():
            raise RuntimeError('Cargo and Python metadata versions must match')
        target = rust_target()
        environment = rust_environment(ROOT, target)
        cargo = shutil.which('cargo', path=environment.get('PATH'))
        if cargo is None:
            raise RuntimeError('Cargo is unavailable after automatic Rust preparation')
        if sys.platform == 'darwin':
            minimum = '11.0' if target.startswith('aarch64') else '10.12'
            requested = environment.get('MACOSX_DEPLOYMENT_TARGET', minimum)
            if tuple(map(int, requested.split('.'))) < tuple(map(int, minimum.split('.'))):
                requested = minimum
            environment['MACOSX_DEPLOYMENT_TARGET'] = requested
        subprocess.run([cargo, 'build', '--release', '--locked', '--target', target],
                       cwd=ROOT, env=environment, check=True)
        filename = 'benchset.dll' if sys.platform == 'win32' else (
            'libbenchset.dylib' if sys.platform == 'darwin' else 'libbenchset.so'
        )
        destination = Path(self.build_lib) / 'benchset' / '_native'
        destination.mkdir(parents=True, exist_ok=True)
        for old in destination.iterdir():
            if old.suffix in ('.so', '.dll', '.dylib'):
                old.unlink()
        shutil.copy2(ROOT / 'target' / target / 'release' / filename, destination / filename)


class PlatformWheel(bdist_wheel):
    def get_tag(self):
        _python, _abi, platform = super().get_tag()
        if sys.platform == 'darwin':
            machine = rust_target().split('-')[0]
            architecture = 'arm64' if machine == 'aarch64' else 'x86_64'
            minimum = (11, 0) if architecture == 'arm64' else (10, 12)
            existing = tuple(map(int, platform.split('_')[1:3]))
            major, minor = max(existing, minimum)
            platform = 'macosx_%d_%d_%s' % (major, minor, architecture)
        return 'py3', 'none', platform


setup(distclass=NativeDistribution, cmdclass={'build_py': BuildNative, 'bdist_wheel': PlatformWheel})
