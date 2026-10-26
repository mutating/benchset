"""Compile real independent C/C++ consumers against the packaged native ABI."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[1]


def compile_and_run_consumer(  # noqa: PLR0913 - explicit build inputs and execution policy
    source: Path, include_dir: Path, library: Path, working_dir: Path,
    extra_link_args: Tuple[str, ...] = (), allow_no_work: bool = False,
) -> subprocess.CompletedProcess:
    """Build and execute one public consumer, with a working directory outside checkout."""
    is_cpp = source.suffix == '.cpp'
    candidates = ('c++', 'clang++', 'g++', 'cl') if is_cpp else ('cc', 'clang', 'gcc', 'cl')
    if sys.platform == 'win32':
        candidates = ('cl',)  # The Windows release contains MSVC libraries.
    compiler = next((path for name in candidates if (path := shutil.which(name))), None)
    if compiler is None:
        pytest.skip('No C/C++ compiler is installed')
    assert library.is_file(), 'Build the native library before running native consumers'
    executable = working_dir / ('consumer.exe' if os.name == 'nt' else 'consumer')
    link_library = library
    if os.name == 'nt' and library.suffix == '.dll':
        # Cargo emits a COFF import library for its DLL. Source test setup keeps
        # it in target/release while copying the DLL beside the Python package.
        import_libraries = [library.with_suffix('.dll.lib'),
                            library.parent / 'benchset.lib',
                            ROOT / 'target' / 'release' / 'benchset.dll.lib']
        link_library = next((path for path in import_libraries if path.is_file()), library)
    is_msvc = Path(compiler).stem.lower() == 'cl'
    if is_msvc:
        standard = '/std:c++14' if is_cpp else '/std:c11'
        command = [compiler, '/nologo', standard, '/MD', '/W4', '/WX',
                   '/I' + str(include_dir), str(source), str(link_library),
                   '/Fe' + str(executable)]
    else:
        standard = '-std=c++11' if is_cpp else '-std=c11'
        command = [compiler, standard, '-Wall', '-Wextra', '-Werror',
                   '-I', str(include_dir), str(source), str(link_library), '-o', str(executable)]
        if is_cpp and os.name != 'nt':
            command.append('-pthread')
        if os.name != 'nt' and library.suffix in ('.dylib', '.so'):
            command.append('-Wl,-rpath,' + str(library.parent))
    if os.name == 'nt' and link_library.name == 'benchset.lib':
        system_libraries = ['advapi32', 'bcrypt', 'kernel32', 'ntdll', 'userenv', 'ws2_32']
        command.extend([name + '.lib' if is_msvc else '-l' + name for name in system_libraries])
    command.extend(extra_link_args)
    compiled = subprocess.run(command, cwd=working_dir, capture_output=True, text=True, check=False)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    environment = os.environ.copy()
    for variable in ('DYLD_LIBRARY_PATH', 'LD_LIBRARY_PATH', 'PATH'):
        environment[variable] = str(library.parent) + os.pathsep + environment.get(variable, '')
    result = subprocess.run([str(executable)], cwd=working_dir, env=environment,
                            capture_output=True, text=True, timeout=30, check=False)
    if (allow_no_work and result.returncode == 1 and 'Reference:' in result.stdout
            and result.stderr == 'measurement window was too short to complete any work\n'):
        return result
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stderr
    return result


@pytest.mark.parametrize('source', ['reference.c', 'reference.cpp'])
def test_windows_consumers_select_the_msvc_compiler_for_msvc_libraries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str,
) -> None:
    library = tmp_path / 'benchset.lib'
    library.touch()
    commands: List[List[str]] = []

    def execute(command: List[str], **_options: Any) -> subprocess.CompletedProcess:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, '', '')

    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(shutil, 'which', lambda name: name)
    monkeypatch.setattr(subprocess, 'run', execute)
    compile_and_run_consumer(ROOT / 'tests/native' / source, ROOT / 'include', library, tmp_path)
    assert commands[0][0] == 'cl'
    assert '/WX' in commands[0]


@pytest.mark.parametrize('case', [
    (False, 1, True, 'no-work', False),
    (True, 1, True, 'no-work', True),
    (True, 2, True, 'no-work', False),
    (True, 1, False, 'no-work', False),
    (True, 1, True, 'other', False),
    (True, 1, True, 'extra', False),
], ids=['strict-default', 'exact-opt-in', 'wrong-exit', 'missing-reference', 'other-reason', 'extra-stderr'])
def test_consumer_no_work_exception_is_opt_in_and_exact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: Tuple[bool, int, bool, str, bool],
) -> None:
    allow_no_work, returncode, reference, diagnostic, accepted = case
    library = tmp_path / 'libbenchset.so'
    library.touch()
    stderr = 'measurement window was too short to complete any work\n'
    if diagnostic == 'other':
        stderr = 'another benchmark is already running\n'
    elif diagnostic == 'extra':
        stderr += 'extra diagnostic\n'
    stdout = 'Reference: Raspberry Pi 5 Model B Rev 1.0 (pi)\n' if reference else ''
    outcomes = iter((subprocess.CompletedProcess([], 0, '', ''),
                     subprocess.CompletedProcess([], returncode, stdout, stderr)))
    monkeypatch.setattr(shutil, 'which', lambda _name: 'cc')
    monkeypatch.setattr(subprocess, 'run', lambda *_args, **_kwargs: next(outcomes))
    arguments = (ROOT / 'examples/native/c.c', ROOT / 'include', library, tmp_path)
    if accepted:
        result = compile_and_run_consumer(*arguments, allow_no_work=allow_no_work)
        assert (result.returncode, result.stdout, result.stderr) == (returncode, stdout, stderr)
    else:
        with pytest.raises(AssertionError):
            compile_and_run_consumer(*arguments, allow_no_work=allow_no_work)


@pytest.mark.parametrize(('source', 'message'), [
    ('reference.c', 'C ABI 1 reference smoke passed'),
    ('reference.cpp', 'C++ ABI 1 reference smoke passed'),
    ('legacy_v1.c', 'legacy ABI 1 consumer remains compatible'),
    ('legacy_v2.c', 'legacy V2 result consumer remains compatible'),
    ('config_prefix.c', 'C config prefix stops at a protected page'),
])
def test_public_native_consumers(tmp_path: Path, source: str, message: str) -> None:
    filenames: Tuple[str, str, str] = ('benchset.dll', 'libbenchset.dylib', 'libbenchset.so')
    filename = filenames[0] if sys.platform == 'win32' else filenames[1 if sys.platform == 'darwin' else 2]
    library = ROOT / 'benchset' / '_native' / filename
    result = compile_and_run_consumer(ROOT / 'tests' / 'native' / source,
                                      ROOT / 'include', library, tmp_path)
    assert result.stdout.strip() == message
