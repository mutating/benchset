"""Exercise the developer build entry point without invoking a compiler."""

from __future__ import annotations

import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import version


@pytest.mark.parametrize('configuration', [
    ('darwin', 'libbenchset.dylib'), ('linux', 'libbenchset.so'), ('win32', 'benchset.dll'),
])
@pytest.mark.parametrize('cargo_output', [None, 'relative-output', 'absolute-output'])
def test_developer_build_copies_the_fresh_host_library_after_successful_cargo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configuration: tuple[str, str],
    capsys: pytest.CaptureFixture[str], cargo_output: str | None,
) -> None:
    """Run the shipped script and load the host's newly compiled library path.

    Compilation must finish before publishing a complete replacement. A reader
    during copying must still see the previous library, never partial bytes;
    interrupted copies must leave that library intact and remove staging files.
    Custom Cargo output directories must supply fresh bytes, never stale target/.
    The matching Rust-generated metadata must also reach the source package.
    The copied entry point keeps its real relative-path calculations intact.
    """
    system, filename = configuration
    script = tmp_path / 'scripts/build_native.py'
    script.parent.mkdir()
    script.write_bytes((Path(__file__).resolve().parents[1] / 'scripts/build_native.py').read_bytes())
    (tmp_path / 'benchset').mkdir()
    installed = tmp_path / 'benchset/_native' / filename
    if cargo_output is None:
        monkeypatch.delenv('CARGO_TARGET_DIR', raising=False)
    else:
        monkeypatch.setenv('CARGO_TARGET_DIR', str(tmp_path / cargo_output) if cargo_output == 'absolute-output' else cargo_output)
    output = tmp_path / (cargo_output or 'target')
    synchronized = []
    monkeypatch.setattr(version, 'sync_version', lambda root: synchronized.append(root))
    monkeypatch.setattr(sys, 'platform', system)

    def compile_(arguments: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        assert arguments == ['cargo', 'build', '--release', '--locked', '--message-format=json-render-diagnostics']
        assert options == {'cwd': tmp_path, 'check': True, 'env': None, 'stdout': subprocess.PIPE, 'encoding': 'utf-8'}
        assert synchronized[-1] == tmp_path
        library = output / 'release' / filename
        library.parent.mkdir(parents=True, exist_ok=True)
        library.write_bytes(b'fresh native library')
        metadata = output / 'reported-build-script-output/metadata.json'
        metadata.parent.mkdir(parents=True, exist_ok=True)
        metadata.write_text('{"fresh": true}\n')
        message = {'reason': 'build-script-executed', 'env': [['BENCHSET_METADATA_PATH_HEX', os.fsencode(metadata).hex()]]}
        return subprocess.CompletedProcess(arguments, 0, stdout=json.dumps(message) + '\n')

    monkeypatch.setattr(subprocess, 'run', compile_)
    copy = shutil.copy2
    previous = None
    copy_fails = False

    def copy_with_reader(source: Path, destination: Path) -> Path:
        Path(destination).write_bytes(b'partial copy')
        assert (installed.read_bytes() if installed.exists() else None) == previous
        if copy_fails:
            raise OSError('copy interrupted')
        return copy(source, destination)

    monkeypatch.setattr(shutil, 'copy2', copy_with_reader)
    runpy.run_path(str(script), run_name='__main__')
    previous = installed.read_bytes()
    runpy.run_path(str(script), run_name='__main__')
    assert installed.read_bytes() == b'fresh native library'
    assert (tmp_path / 'benchset/_metadata.json').read_text() == '{"fresh": true}\n'
    copy_fails = True
    with pytest.raises(OSError, match='copy interrupted'):
        runpy.run_path(str(script), run_name='__main__')
    assert installed.read_bytes() == previous
    assert list(installed.parent.iterdir()) == [installed]
    assert synchronized == [tmp_path, tmp_path, tmp_path]
    assert capsys.readouterr().out.splitlines() == [str(installed), str(installed)]
