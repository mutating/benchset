"""Compile downstream Rust code against the current library's public API."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from scripts.build_backend import rust_environment
from scripts.version import read_version

ROOT = Path(__file__).resolve().parents[1]


def _compile(arguments: List[str], environment: Dict[str, str], directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, cwd=directory, env=environment, capture_output=True,
                          encoding='utf-8', check=False, timeout=120)


@pytest.fixture(scope='module')
def rust_library(tmp_path_factory: pytest.TempPathFactory) -> Tuple[List[str], Dict[str, str], Path]:
    """Build a fresh rlib without sharing Cargo outputs with other tests."""
    architecture = 'aarch64' if platform.machine().lower() in ('aarch64', 'arm64') else 'x86_64'
    system = {'darwin': 'apple-darwin', 'linux': 'unknown-linux-gnu', 'win32': 'pc-windows-msvc'}[sys.platform]
    target = architecture + '-' + system
    environment = rust_environment(ROOT, target)
    environment['CARGO_PKG_VERSION'] = read_version(ROOT)
    rustc = shutil.which('rustc', path=environment.get('PATH'))
    assert rustc is not None
    compiler = [rustc, '--edition=2021']
    # Direct rustc consumers need the same explicit MSVC linker as Cargo jobs.
    linker = environment.get('CARGO_TARGET_' + target.upper().replace('-', '_') + '_LINKER')
    if linker is not None:
        compiler.extend(['-C', 'linker=' + linker])
    directory = tmp_path_factory.mktemp('rust-public-api')
    library = directory / 'libbenchset.rlib'
    result = _compile([*compiler, '--crate-name', 'benchset', '--crate-type=rlib',
                       str(ROOT / 'native/src/lib.rs'), '-o', str(library)], environment, directory)
    assert result.returncode == 0, 'Fresh Rust library did not compile:\n' + result.stdout + result.stderr
    return compiler, environment, library


@pytest.mark.parametrize('lookup', [
    pytest.param(('from_id', 'u32', '1', '2'), id='from_id'),
    pytest.param(('from_name', "&'static str", '"single_core"', '"multi_core"'), id='from_name'),
])
def test_benchmark_keeps_downstream_extension_trait_lookup(
    rust_library: Tuple[List[str], Dict[str, str], Path], tmp_path: Path,
    lookup: Tuple[str, str, str, str],
) -> None:
    """Internal parsers must not shadow callers' existing Benchmark traits.

    This is a real external crate: both imported and fully qualified public
    Benchmark paths must retain their identity and resolve the caller's trait.
    Custom argument and Result types expose either newly public inherent helper
    independently, even though the library's own Rust tests still compile.
    """
    compiler, environment, library = rust_library
    method, input_type, single, multi = lookup
    source = tmp_path / 'consumer.rs'
    source.write_text(f'''
use benchset::Benchmark;

struct HostValue({input_type});
#[derive(Debug, PartialEq)]
struct HostError;

trait HostLookup: Sized {{
    fn {method}(input: HostValue) -> Result<Self, HostError>;
}}
impl HostLookup for Benchmark {{
    fn {method}(input: HostValue) -> Result<Self, HostError> {{
        match input.0 {{
            {single} => Ok(Self::SingleCore),
            {multi} => Ok(Self::MultiCore),
            _ => Err(HostError),
        }}
    }}
}}

fn main() {{
    let imported: Result<Benchmark, HostError> = Benchmark::{method}(HostValue({single}));
    let qualified: Result<benchset::Benchmark, HostError> =
        benchset::Benchmark::{method}(HostValue({multi}));
    assert_eq!(imported, Ok(Benchmark::SingleCore));
    assert_eq!(qualified, Ok(Benchmark::MultiCore));
    assert_eq!(std::any::type_name::<Benchmark>(), "benchset::Benchmark");
    assert_eq!(std::any::type_name::<benchset::Benchmark>(), "benchset::Benchmark");
}}
''', encoding='utf-8')
    executable = tmp_path / ('consumer.exe' if os.name == 'nt' else 'consumer')
    result = _compile([*compiler, str(source), '--extern', 'benchset=' + str(library),
                       '-o', str(executable)], environment, tmp_path)
    assert result.returncode == 0, 'Public Benchmark extension-trait lookup regressed:\n' + result.stdout + result.stderr
    result = _compile([str(executable)], environment, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
