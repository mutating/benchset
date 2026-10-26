"""Execute public consumers of the actual, unpacked release formats.

These checks intentionally use fresh directories and short measurement windows.
They do not rely on the external calibration experiment or the checkout's
bundled native library. The Apple checks run after package_native.py --apple.
"""

from __future__ import annotations

import inspect
import io
import json
import locale
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import venv
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import pytest

from scripts import check_source_install
from scripts.check_source_install import INSTALLED_API_CHECK, verify_cli

if TYPE_CHECKING:
    import benchset

ROOT = Path(__file__).resolve().parents[1]


def test_artifact_checks_collect_with_only_pytest_installed(tmp_path: Path) -> None:
    from importlib.metadata import distribution  # noqa: PLC0415

    from packaging.requirements import Requirement  # noqa: PLC0415

    source, dependencies = tmp_path / 'source', tmp_path / 'pytest-only'
    source.mkdir()
    dependencies.mkdir()
    # Reproduce the Swift-only jobs: a clean checkout, pytest and its own
    # dependencies, with no installed benchset metadata or site startup hooks.
    for directory in ('benchset', 'scripts'):
        shutil.copytree(ROOT / directory, source / directory,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '_native'))
    (source / 'tests').mkdir()
    for name in ('__init__.py', 'helpers.py', 'test_reference_artifacts.py'):
        shutil.copy2(ROOT / 'tests' / name, source / 'tests' / name)
    for name in ('README.md', 'Cargo.toml', 'pyproject.toml'):
        shutil.copy2(ROOT / name, source / name)

    pending, copied = ['pytest'], set()
    while pending:
        name = pending.pop()
        if name in copied:
            continue
        copied.add(name)
        package = distribution(name)
        for requirement in map(Requirement, package.requires or ()):
            if requirement.marker is None or requirement.marker.evaluate({'extra': ''}):
                pending.append(requirement.name)
        for path in package.files or ():
            if '..' in path.parts or path.suffix == '.pyc':
                continue
            destination = dependencies / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(package.locate_file(path)), destination)

    program = '''
import importlib.metadata
import sys
sys.path[:0] = sys.argv[1:3]
try:
    importlib.metadata.version('benchset')
except importlib.metadata.PackageNotFoundError:
    pass
else:
    raise AssertionError('the isolated checkout unexpectedly has benchset metadata')
import pytest
status = pytest.main(['-q', 'tests/test_reference_artifacts.py', '-k', 'swift'])
assert status == 0, status
assert 'benchset' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-I', '-S', '-c', program, str(source), str(dependencies)],
                            cwd=source, capture_output=True, encoding='utf-8', check=False, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '1 passed, 1 skipped' in result.stdout


def execute(arguments: List[str], cwd: Path, environment: Dict[str, str]) -> str:
    result = subprocess.run(arguments, cwd=cwd, env=dict(environment, PYTHONIOENCODING='utf-8'),
                            capture_output=True, encoding='utf-8', check=False, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_artifact_capture_uses_utf8_without_changing_the_callers_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(subprocess, '_text_encoding', lambda: 'ascii', raising=False)
    monkeypatch.setattr(locale, 'getpreferredencoding', lambda _setlocale=True: 'ascii')
    environment = dict(os.environ, PYTHONIOENCODING='utf-8:strict')
    before = environment.copy()
    assert execute([sys.executable, '-c', 'print(chr(0x3c0))'], tmp_path, environment) == '\u03c0\n'
    assert environment == before


@pytest.mark.parametrize('encoding', ['ascii', 'cp1251', 'utf-8'])
def test_source_install_cli_capture_matches_its_echo_stream(
    monkeypatch: pytest.MonkeyPatch, encoding: str,
) -> None:
    original_run = subprocess.run
    captured: List[subprocess.CompletedProcess] = []
    program = '''
from benchset import BenchmarkResult, BenchmarkRunner, cli
def completed(_self, name, *, warmup_seconds, measurement_seconds, **_options):
    return BenchmarkResult(name, status='completed', score=1, pi_equivalent=1, workers=1,
                           warmup_seconds=warmup_seconds, measurement_seconds=measurement_seconds)
BenchmarkRunner.run = completed
raise SystemExit(cli.main(['--warmup=0', '--duration=.05']))
'''

    def run_step(arguments: List[str], **options: Any) -> subprocess.CompletedProcess:
        if options.get('capture_output'):
            options['env'] = dict(options['env'], PYTHONPATH=str(ROOT))
            result = original_run([sys.executable, '-c', program], **options)
            captured.append(result)
            return result
        return subprocess.CompletedProcess(arguments, 0)

    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding=encoding)
    with monkeypatch.context() as scoped:
        scoped.setenv('PYTHONIOENCODING', 'utf-8')
        scoped.setattr(sys, 'stdout', stream)
        scoped.setattr(subprocess, '_text_encoding', lambda: 'ascii', raising=False)
        scoped.setattr(locale, 'getpreferredencoding', lambda _setlocale=True: 'ascii')
        scoped.setattr(subprocess, 'run', run_step)
        scoped.setattr(shutil, 'which', lambda *_args, **_kwargs: None)
        check_source_install.main()
        stream.flush()
    text = buffer.getvalue().decode(encoding)
    stream.detach()
    assert len(captured) == 2
    assert text.count('1 \u00d7 pi' if encoding == 'utf-8' else '1 x pi') == 4


@pytest.fixture(scope='module')
def source_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    source = tmp_path_factory.mktemp('reference-source') / 'source'
    source.mkdir()
    for name in ('benchset', 'native', 'include', 'scripts', 'examples', 'Sources'):
        shutil.copytree(ROOT / name, source / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.so', '*.dll', '*.dylib'))
    shutil.copytree(ROOT / 'tests/swift', source / 'tests/swift')
    for name in ('Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml', 'pyproject.toml',
                 'setup.py', 'MANIFEST.in', 'Package.swift', 'README.md', 'LICENSE'):
        shutil.copy2(ROOT / name, source / name)
    return source


@pytest.fixture(scope='module')
def rust_tools() -> Tuple[Dict[str, str], str, str]:
    from scripts.build_backend import rust_environment  # noqa: PLC0415

    architecture = 'aarch64' if platform.machine().lower() in ('aarch64', 'arm64') else 'x86_64'
    system = {'darwin': 'apple-darwin', 'linux': 'unknown-linux-gnu', 'win32': 'pc-windows-msvc'}[sys.platform]
    target = architecture + '-' + system
    environment = rust_environment(ROOT, target)
    environment['PYTHONIOENCODING'] = 'utf-8'
    # Archive builds keep Cargo outputs inside their independent source trees.
    # Do not let the checkout's coverage startup register copies as separate runtime
    # sources; the real checkout is fully instrumented by the parent test run.
    for variable in ('CARGO_TARGET_DIR', 'PYTHONPATH', 'COVERAGE_PROCESS_START', 'COVERAGE_FILE'):
        environment.pop(variable, None)
    cargo = shutil.which('cargo', path=environment.get('PATH'))
    assert cargo is not None
    return environment, cargo, target


def test_artifact_build_environment_isolates_outputs_without_changing_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    cargo = tmp_path / ('cargo.exe' if os.name == 'nt' else 'cargo')
    cargo.touch()
    cargo.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path))
    inherited = {
        'CARGO_TARGET_DIR': str(tmp_path / 'shared-target'),
        'PYTHONPATH': str(tmp_path / 'checkout'),
        'COVERAGE_PROCESS_START': str(tmp_path / 'coverage.ini'),
        'COVERAGE_FILE': str(tmp_path / '.coverage'),
    }
    for variable, value in inherited.items():
        monkeypatch.setenv(variable, value)

    environment, selected_cargo, _target = inspect.unwrap(rust_tools)()

    assert Path(selected_cargo).samefile(cargo)
    assert all(variable not in environment for variable in inherited)
    assert all(os.environ[variable] == value for variable, value in inherited.items())


def test_unpacked_crate_builds_and_runs_a_public_rust_consumer(
    source_snapshot: Path, rust_tools: Tuple[Dict[str, str], str, str], tmp_path: Path,
) -> None:
    environment, cargo, _target = rust_tools
    # The separate snapshot includes uncommitted feature work, but excludes all
    # native build products. Cargo's include list determines the shipped files.
    execute([cargo, 'package', '--locked', '--offline', '--allow-dirty', '--no-verify'],
            source_snapshot, environment)
    archive = next((source_snapshot / 'target/package').glob('*.crate'))
    unpacked_directory = tmp_path / 'crate-π-🍓'
    unpacked_directory.mkdir()
    with tarfile.open(archive) as package:
        package.extractall(unpacked_directory)
    unpacked = next(unpacked_directory.glob('benchset-*'))
    calibration = json.loads((unpacked / 'native/reference-calibration.json').read_text(encoding='utf-8'))
    assert calibration
    execute([cargo, 'test', '--lib', '--locked', '--offline', '--', '--test-threads=1'],
            unpacked, environment)
    consumer = tmp_path / 'consumer'
    (consumer / 'src').mkdir(parents=True)
    documented_version = re.search(r'^benchset = "([^"]+)"', (unpacked / 'README.md').read_text(encoding='utf-8'), re.MULTILINE)
    assert documented_version is not None
    (consumer / 'Cargo.toml').write_text(
        '[package]\nname = "reference-consumer"\nversion = "0.0.0"\nedition = "2021"\n'
        '[dependencies]\nbenchset = { path = ' + json.dumps(str(unpacked), ensure_ascii=False)
        + ', version = ' + json.dumps(documented_version.group(1)) + ' }\n',
        encoding='utf-8',
    )
    # This is the published example, not an internal module import.
    shutil.copy2(unpacked / 'examples/native/rust.rs', consumer / 'src/main.rs')
    output = execute([cargo, 'run', '--offline', '--quiet'], consumer, environment)
    assert 'Reference: Raspberry Pi 5 Model B Rev 1.0' in output
    if 'status: Completed,' in output:
        assert 'pi_equivalent: Some(' in output
    else:
        assert 'status: Failed,' in output
        assert 'reason: "measurement window was too short to complete any work"' in output
        assert 'score: None,' in output
        assert 'pi_equivalent: None,' in output


def test_documented_swift_dependency_pin_matches_the_package_version() -> None:
    from scripts.version import read_version  # noqa: PLC0415

    pin = re.search(r'\.exact\("([^"]+)"\)', (ROOT / 'README.md').read_text(encoding='utf-8'))
    assert pin is not None
    assert pin.group(1) == read_version(ROOT)


def test_documented_cancellation_boundary_matches_runner_lifetime() -> None:
    from benchset import BenchmarkResult, BenchmarkRunner, Progress  # noqa: PLC0415
    from tests.helpers import assert_runner_reusable  # noqa: PLC0415

    runner = BenchmarkRunner()
    immediate_reuse: List[BenchmarkResult] = []

    def cancel(progress: Progress) -> None:
        if progress.phase == 'warmup':
            runner.cancel()
            # cancel() has returned, but the active run still owns the runner.
            immediate_reuse.append(runner.run('single_core', warmup_seconds=0, measurement_seconds=0.01))

    result = runner.run('single_core', warmup_seconds=10, measurement_seconds=1, on_progress=cancel)
    assert result.status == 'cancelled'
    assert immediate_reuse
    assert all(value.reason == 'this runner is already running' for value in immediate_reuse)
    assert_runner_reusable(runner)
    # The documented completion boundary must name the awaited call explicitly.
    documentation = ' '.join((ROOT / 'README.md').read_text(encoding='utf-8').split())
    assert 'After `run()` returns, all native workers are joined' in documentation


def test_documentation_checks_ignore_the_default_file_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    original_read_text = Path.read_text

    def read_text(path: Path, encoding: Optional[str] = None, errors: Optional[str] = None) -> str:
        # Exercise the actual file decoder even when this interpreter uses UTF-8 mode.
        return original_read_text(path, encoding='ascii' if encoding is None else encoding, errors=errors)

    monkeypatch.setattr(Path, 'read_text', read_text)
    test_documented_swift_dependency_pin_matches_the_package_version()
    test_documented_cancellation_boundary_matches_runner_lifetime()


@pytest.mark.parametrize('encoding', ['utf-8', 'ascii', 'cp1251'])
def test_installed_cli_verifier_accepts_supported_stream_encodings(encoding: str) -> None:
    from benchset import PI_REFERENCE_RUN, BenchmarkResult, cli  # noqa: PLC0415

    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding=encoding, newline='\n')
    for index, reference in enumerate(PI_REFERENCE_RUN.results):
        if index:
            stream.write('\n')
        cli.render(BenchmarkResult(reference.benchmark, status='completed',
                                   score=reference.score, pi_equivalent=1.0, workers=1,
                                   warmup_seconds=0, measurement_seconds=0.05), stream)
    stream.flush()
    text = buffer.getvalue().decode(encoding)
    assert ('\u00d7 pi' if encoding == 'utf-8' else 'x pi') in text
    verify_cli(text)
    stream.detach()


def render_installed_cli_result(result: benchset.BenchmarkResult, encoding: str = 'utf-8') -> str:
    from benchset import cli  # noqa: PLC0415

    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding=encoding, newline='\n')
    cli.render(result, stream)
    stream.flush()
    text = buffer.getvalue().decode(encoding)
    stream.detach()
    return text


@pytest.fixture
def installed_cli_results() -> Tuple[benchset.BenchmarkResult, ...]:
    from benchset import PI_REFERENCE_RUN, BenchmarkResult, Sample  # noqa: PLC0415

    return tuple(BenchmarkResult(
        reference.benchmark, status='completed', score=2000,
        pi_equivalent=2000 / reference.score, warmup_seconds=0, measurement_seconds=0.05,
        elapsed_seconds=0.05, workers=1, iterations=100, checksum=7,
        samples=(Sample(0.05, 0.05, 100, 2000),),
    ) for reference in PI_REFERENCE_RUN.results)


@pytest.mark.parametrize('completed_count', [0, 1, 2])
def test_installed_cli_verifier_accepts_windows_line_endings(
    installed_cli_results: Tuple[benchset.BenchmarkResult, ...], completed_count: int,
) -> None:
    output = '\n'.join(render_installed_cli_result(result)
                       for result in installed_cli_results[:completed_count])
    errors = ''
    if completed_count < 2:
        failed = replace(installed_cli_results[completed_count], status='failed',
                         reason='measurement window was too short to complete any work',
                         score=None, pi_equivalent=None, iterations=0, checksum=0, samples=())
        errors = render_installed_cli_result(failed)
    verify_cli(output.replace('\n', '\r\n'), errors.replace('\n', '\r\n'), int(completed_count < 2))


@pytest.mark.parametrize('encoding', ['utf-8', 'ascii', 'cp1251'])
@pytest.mark.parametrize('completed_count', [0, 1, 2])
@pytest.mark.parametrize('zero_sample', [False, True])
def test_installed_cli_verifier_accepts_only_completed_prefix_or_exact_no_work(
    installed_cli_results: Tuple[benchset.BenchmarkResult, ...], encoding: str, completed_count: int, zero_sample: bool,
) -> None:
    from benchset import Sample  # noqa: PLC0415

    output = '\n'.join(render_installed_cli_result(result, encoding)
                       for result in installed_cli_results[:completed_count])
    errors = ''
    if completed_count < 2:
        failed = replace(
            installed_cli_results[completed_count], status='failed',
            reason='measurement window was too short to complete any work',
            score=None, pi_equivalent=None, iterations=0, checksum=0,
            samples=(Sample(0.05, 0.05, 0, 0),) if zero_sample else (),
        )
        errors = render_installed_cli_result(failed, encoding)
    verify_cli(output, errors, int(completed_count < 2))


@pytest.mark.parametrize('mutation', [
    'generic failure', 'nonzero iterations', 'present ratio', 'present score',
    'present variation', 'unstable', 'nonzero sample', 'wrong status',
    'wrong order', 'wrong duration', 'wrong warmup', 'extra stderr',
    'failure exit zero', 'success exit one', 'missing success', 'continued after failure',
])
def test_installed_cli_verifier_rejects_other_failures_and_invalid_stop_behavior(
    installed_cli_results: Tuple[benchset.BenchmarkResult, ...], mutation: str,
) -> None:
    from benchset import Sample  # noqa: PLC0415

    first, second = installed_cli_results
    failed = replace(second, status='failed', reason='measurement window was too short to complete any work',
                     score=None, pi_equivalent=None, iterations=0, checksum=0, samples=())
    variants = {
        'generic failure': replace(failed, reason='unexpected native failure'),
        'nonzero iterations': replace(failed, iterations=1),
        'present ratio': replace(failed, pi_equivalent=0.0),
        'present score': replace(failed, score=0.0),
        'present variation': replace(failed, variation_percent=0.0),
        'unstable': replace(failed, unstable=True),
        'nonzero sample': replace(failed, samples=(Sample(0.05, 0.05, 1, 20),)),
        'wrong status': replace(failed, status='cancelled'),
        'wrong order': replace(failed, benchmark='single_core'),
        'wrong duration': replace(failed, measurement_seconds=0.06),
        'wrong warmup': replace(failed, warmup_seconds=0.01),
    }
    failed = variants.get(mutation, failed)
    output, errors, code = render_installed_cli_result(first), render_installed_cli_result(failed), 1
    if mutation == 'extra stderr':
        errors += 'unexpected native failure\n'
    elif mutation == 'failure exit zero':
        code = 0
    elif mutation == 'success exit one':
        output, errors = output + '\n' + render_installed_cli_result(second), ''
    elif mutation == 'missing success':
        errors, code = '', 0
    elif mutation == 'continued after failure':
        output += '\n' + render_installed_cli_result(second)
    with pytest.raises(RuntimeError):
        verify_cli(output, errors, code)


@pytest.mark.parametrize('failure', ['no work', 'generic failure', 'nonzero work'])
def test_installed_api_check_visits_both_workloads_and_accepts_only_exact_no_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str,
) -> None:
    import ctypes  # noqa: PLC0415

    import benchset  # noqa: PLC0415

    calls: List[str] = []

    def execute(name: str, **_options: object) -> benchset.BenchmarkResult:
        if name == 'unknown':
            return benchset.BenchmarkResult(name)
        calls.append(name)
        reason = ('unexpected native failure' if failure == 'generic failure'
                  else 'measurement window was too short to complete any work')
        return benchset.BenchmarkResult(name, reason=reason, iterations=int(failure == 'nonzero work'))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(benchset, 'run', execute)
    # Also restore the temporary import guard if a consumer assertion fails.
    monkeypatch.setattr(ctypes, 'CDLL', ctypes.CDLL)
    if failure == 'no work':
        exec(INSTALLED_API_CHECK, {})
    else:
        with pytest.raises(AssertionError):
            exec(INSTALLED_API_CHECK, {})
    assert calls == ['single_core', 'multi_core']


@pytest.fixture
def bumped_source_snapshot(source_snapshot: Path, tmp_path: Path) -> Path:
    from scripts.version import read_version  # noqa: PLC0415

    versioned_source = tmp_path / 'versioned-source'
    shutil.copytree(source_snapshot, versioned_source, ignore=shutil.ignore_patterns('target', 'build', 'dist'))
    configuration = versioned_source / 'pyproject.toml'
    original_version = read_version(versioned_source)
    # A real future bump starts by editing only the canonical Python manifest.
    configuration.write_text(configuration.read_text(encoding='utf-8').replace(
        'version = "' + original_version + '"', 'version = "9.8.7"', 1,
    ), encoding='utf-8')
    return versioned_source


@pytest.mark.parametrize('hook', [
    'get_requires_for_build_sdist', 'prepare_metadata_for_build_wheel', 'prepare_metadata_for_build_editable',
])
def test_python_build_hooks_synchronize_versions_before_metadata(
    bumped_source_snapshot: Path, rust_tools: Tuple[Dict[str, str], str, str], tmp_path: Path, hook: str,
) -> None:
    from scripts.version import sync_version  # noqa: PLC0415

    environment, _cargo, _target = rust_tools
    metadata = tmp_path / 'metadata'
    metadata.mkdir()
    program = 'from scripts import build_backend; build_backend.' + hook
    program += '(' + repr(str(metadata)) + ')' if hook.startswith('prepare_') else '()'
    execute([sys.executable, '-c', program], bumped_source_snapshot, environment)
    assert sync_version(bumped_source_snapshot, check=True) == '9.8.7'
    if hook.startswith('prepare_'):
        description = next(metadata.glob('*.dist-info/METADATA')).read_text(encoding='utf-8')
        assert '\nVersion: 9.8.7\n' in description


@pytest.mark.parametrize('script', ['build_native.py', 'package_native.py'])
def test_native_build_entrypoints_synchronize_before_invoking_cargo(
    bumped_source_snapshot: Path, rust_tools: Tuple[Dict[str, str], str, str], script: str,
) -> None:
    from scripts.version import sync_version  # noqa: PLC0415

    environment, _cargo, target = rust_tools
    command = [sys.executable, 'scripts/' + script]
    if script == 'package_native.py':
        command.extend(['--target', target])
        if sys.platform == 'darwin':
            command.append('--apple')
    execute(command, bumped_source_snapshot, environment)
    assert sync_version(bumped_source_snapshot, check=True) == '9.8.7'
    filename = 'benchset.dll' if sys.platform == 'win32' else (
        'libbenchset.dylib' if sys.platform == 'darwin' else 'libbenchset.so'
    )
    library = bumped_source_snapshot / (
        'benchset/_native' if script == 'build_native.py' else 'release-artifacts/' + target + '/lib'
    ) / filename
    program = ('import ctypes; lib = ctypes.CDLL(' + repr(str(library)) + '); '
               'lib.benchset_version.restype = ctypes.c_char_p; assert lib.benchset_version() == b"9.8.7"')
    execute([sys.executable, '-c', program], bumped_source_snapshot, environment)
    if script == 'package_native.py' and sys.platform == 'darwin':
        with zipfile.ZipFile(bumped_source_snapshot / 'release-artifacts/Benchset-Swift.zip') as archive:
            assert '.exact("9.8.7")' in archive.read('README.md').decode('utf-8')


def test_sdist_builds_an_installable_wheel_with_api_and_both_cli_entrypoints(
    bumped_source_snapshot: Path, rust_tools: Tuple[Dict[str, str], str, str], tmp_path: Path,
) -> None:
    environment, _cargo, _target = rust_tools
    sdists = tmp_path / 'sdists'
    execute([sys.executable, '-m', 'build', '--sdist', '--outdir', str(sdists)],
            bumped_source_snapshot, environment)
    archive = next(sdists.glob('*.tar.gz'))
    assert archive.name == 'benchset-9.8.7.tar.gz'
    unpacked = tmp_path / 'unpacked'
    unpacked.mkdir()
    with tarfile.open(archive) as package:
        package.extractall(unpacked)
    source = next(unpacked.iterdir())
    assert json.loads((source / 'native/reference-calibration.json').read_text(encoding='utf-8'))
    wheels = tmp_path / 'wheels'
    # Build from the unpacked sdist, whose manifest may differ from the checkout.
    execute([sys.executable, '-m', 'build', '--wheel', '--outdir', str(wheels), str(source)],
            tmp_path, environment)
    wheel = next(wheels.glob('*.whl'))
    assert wheel.name.startswith('benchset-9.8.7-')
    with zipfile.ZipFile(wheel) as package:
        assert {'benchset/py.typed', 'benchset/reference.py', 'benchset/models.py'} <= set(package.namelist())
    virtualenv = tmp_path / 'venv'
    venv.EnvBuilder(with_pip=True, symlinks=os.name != 'nt').create(virtualenv)
    python = virtualenv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    console = virtualenv / ('Scripts/benchset.exe' if os.name == 'nt' else 'bin/benchset')
    execute([str(python), '-m', 'pip', 'install', str(wheel)], tmp_path, environment)
    consumer = tmp_path / 'consumer'
    consumer.mkdir()
    # Every subprocess runs outside both source trees with no PYTHONPATH.
    execute([str(python), '-c', INSTALLED_API_CHECK], consumer, environment)
    installed_path = execute([str(python), '-c', 'import benchset; print(benchset.__file__)'],
                             consumer, environment).strip()
    assert virtualenv.resolve() in Path(installed_path).resolve().parents
    for invocation in ([str(console)], [str(python), '-m', 'benchset']):
        assert execute([*invocation, '--version'], consumer, environment) == '9.8.7\n'
        result = subprocess.run([*invocation, '--warmup', '0', '--duration', '0.05'],
                                cwd=consumer, env=environment, capture_output=True,
                                encoding='utf-8', check=False, timeout=300)
        verify_cli(result.stdout, result.stderr, result.returncode)
    execute([str(python), '-m', 'pip', 'check'], consumer, environment)


@pytest.mark.parametrize('legacy_pip', [False, pytest.param(True, marks=pytest.mark.skipif(
    sys.version_info[:2] != (3, 8), reason='The pip bundled with Windows Python 3.8 predates PEP 660',
))])
def test_editable_install_builds_the_native_version_from_pyproject(
    bumped_source_snapshot: Path, rust_tools: Tuple[Dict[str, str], str, str], tmp_path: Path, legacy_pip: bool,
) -> None:
    environment, _cargo, _target = rust_tools
    virtualenv = tmp_path / 'editable-venv'
    venv.EnvBuilder(with_pip=True, symlinks=os.name != 'nt').create(virtualenv)
    python = virtualenv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if legacy_pip:
        execute([str(python), '-m', 'pip', 'install', 'pip==21.1.1'], tmp_path, environment)
    for expected_version in ('9.8.7', '9.8.8'):
        if expected_version == '9.8.8':
            configuration = bumped_source_snapshot / 'pyproject.toml'
            configuration.write_text(configuration.read_text(encoding='utf-8').replace(
                'version = "9.8.7"', 'version = "9.8.8"', 1,
            ), encoding='utf-8')
        execute([str(python), '-m', 'pip', 'install', '--editable', str(bumped_source_snapshot)], tmp_path, environment)
        execute([str(python), '-c', '''
from benchset import __version__
from benchset._native import Native
assert __version__ == ''' + repr(expected_version) + '''
assert Native().lib.benchset_version() == ''' + repr(expected_version.encode('ascii')) + '\n'], tmp_path, environment)


@pytest.fixture(scope='module')
def native_bundle(source_snapshot: Path, rust_tools: Tuple[Dict[str, str], str, str],
                  tmp_path_factory: pytest.TempPathFactory) -> Path:
    environment, _cargo, target = rust_tools
    execute([sys.executable, 'scripts/package_native.py', '--target', target], source_snapshot, environment)
    archive = source_snapshot / 'release-artifacts' / ('benchset-' + target + '.zip')
    directory = tmp_path_factory.mktemp('native-bundle')
    with zipfile.ZipFile(archive) as package:
        package.extractall(directory)
    assert (directory / 'include/benchset.h').read_bytes() == (ROOT / 'include/benchset.h').read_bytes()
    assert json.loads((directory / 'native/reference-calibration.json').read_text(encoding='utf-8'))
    return directory


def test_unpacked_native_zip_links_its_public_header_and_library(
    native_bundle: Path, tmp_path: Path,
) -> None:
    from tests.test_reference_native import compile_and_run_consumer  # noqa: PLC0415

    extra: Tuple[str, ...]
    # Keep the matrix in one test so xdist also builds this native ZIP only once.
    for linkage in ('dynamic', 'static'):
        if linkage == 'static':
            filename = 'benchset.lib' if os.name == 'nt' else 'libbenchset.a'
            # System dependencies of Rust's static standard library; the dynamic
            # library already records its own dependencies.
            extra = () if os.name == 'nt' else (
                ('-liconv',) if sys.platform == 'darwin' else ('-ldl', '-lpthread', '-lm', '-lrt', '-lutil')
            )
        else:
            filename = 'benchset.dll' if os.name == 'nt' else (
                'libbenchset.dylib' if sys.platform == 'darwin' else 'libbenchset.so'
            )
            extra = ()
        for source, message in (
            ('tests/native/reference.c', 'C ABI 1 reference smoke passed'),
            ('tests/native/reference.cpp', 'C++ ABI 1 reference smoke passed'),
            ('tests/native/legacy_v1.c', 'legacy ABI 1 consumer remains compatible'),
            ('tests/native/legacy_v2.c', 'legacy V2 result consumer remains compatible'),
            ('examples/native/c.c', 'Reference: Raspberry Pi 5 Model B Rev 1.0 (pi)'),
        ):
            directory = tmp_path / linkage / Path(source).name
            directory.mkdir(parents=True)
            label = linkage + ' ' + source
            try:
                result = compile_and_run_consumer(
                    ROOT / source, native_bundle / 'include', native_bundle / 'lib' / filename, directory,
                    extra_link_args=extra,
                    allow_no_work=source == 'examples/native/c.c',
                )
            except AssertionError as error:
                raise AssertionError(label + ': ' + str(error)) from error
            assert message in result.stdout, label + ': ' + result.stdout


@pytest.mark.skipif(sys.platform != 'darwin', reason='XCFramework and Swift package target macOS')
def test_unpacked_swift_package_public_consumer_and_both_xcframework_slices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = ROOT / 'release-artifacts/Benchset-Swift.zip'
    if not archive.is_file():
        pytest.skip('Build the offline Swift ZIP with package_native.py --apple first')
    package = tmp_path / 'Benchset-π-🍓' / 'Benchset'
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(package)
    framework = package / 'Artifacts/Benchset.xcframework/macos-arm64_x86_64'
    assert (framework / 'Headers/benchset.h').read_bytes() == (ROOT / 'include/benchset.h').read_bytes()
    assert (package / 'Sources/Benchset/Benchset.swift').read_bytes() == (ROOT / 'Sources/Benchset/Benchset.swift').read_bytes()
    assert json.loads((package / 'native/reference-calibration.json').read_text(encoding='utf-8'))
    library = framework / 'libbenchset.a'
    remote_bundle = tmp_path / 'remote-artifact'
    with zipfile.ZipFile(ROOT / 'release-artifacts/Benchset.xcframework.zip') as bundle:
        bundle.extractall(remote_bundle)
    remote_framework = remote_bundle / 'Benchset.xcframework/macos-arm64_x86_64'
    assert (remote_framework / 'Headers/benchset.h').read_bytes() == (framework / 'Headers/benchset.h').read_bytes()
    assert (remote_framework / 'libbenchset.a').read_bytes() == library.read_bytes()
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    for architecture in ('arm64', 'x86_64'):
        # Inspect the exported machine-code symbols. Rust's archive also ships
        # auxiliary bitcode that an older Apple LLVM reader cannot parse.
        symbols = execute(['nm', '--no-llvm-bc', '-arch', architecture, '-gU', str(library)],
                          tmp_path, environment)
        for name in ('benchset_reference_machine', 'benchset_reference_run',
                     'benchset_reference_result', 'benchset_result_v2', 'benchset_result_v3'):
            assert '_' + name in symbols, (architecture, name)
    consumer = tmp_path / 'consumer-π'
    (consumer / 'Sources/Consumer').mkdir(parents=True)
    (consumer / 'Package.swift').write_text(
        '// swift-tools-version:5.3\nimport PackageDescription\n'
        'let package = Package(name: "Consumer", platforms: [.macOS(.v10_12)], '
        'dependencies: [.package(path: ' + json.dumps(str(package), ensure_ascii=False) + ')], '
        'targets: [.target(name: "Consumer", dependencies: '
        '[.product(name: "Benchset", package: "Benchset")])])\n',
        encoding='utf-8',
    )
    example = (ROOT / 'examples/native/swift.swift').read_text(encoding='utf-8')
    main = consumer / 'Sources/Consumer/main.swift'
    source = '''
import Benchset
import Dispatch

// Existing clients can exhaustively handle the original public error cases.
func describeLegacyError(_ error: BenchsetError) -> String {
    switch error {
    case .alreadyRunning: return "busy"
    case .allocationFailed: return "allocation"
    case .resultUnavailable: return "result"
    }
}
precondition(describeLegacyError(.alreadyRunning) == "busy")
precondition(describeLegacyError(.allocationFailed) == "allocation")
precondition(describeLegacyError(.resultUnavailable) == "result")

// Neither run() nor a validation failure needs to initialize the lazy Swift
// metadata globals. First access happens concurrently inside a live callback.
let coldRunner = BenchmarkRunner()
let coldFailure = try coldRunner.run(.singleCore, warmupSeconds: -1, measurementSeconds: 0.05)
precondition(!coldFailure.success)
var observedReference = false
let coldCancelled = try coldRunner.run(.singleCore, warmupSeconds: 10, measurementSeconds: 1) { progress in
    if progress.phase == 1 || progress.phase == 2 {
        observedReference = true
        DispatchQueue.concurrentPerform(iterations: 8) { _ in
            let machine: ReferenceMachine = coldFailure.referenceMachine
            let reference: ReferenceRun = coldFailure.referenceRun
            precondition(machine.shortName == "pi" && reference.results.count == 2)
        }
        coldRunner.cancel()
    }
}
precondition(observedReference && coldCancelled.status == 1)
''' + example + '''
let typedMachine: ReferenceMachine = machine
let typedReference: ReferenceRun = reference
precondition(machine.name == "Raspberry Pi 5 Model B Rev 1.0")
precondition(machine.shortName == "pi")
precondition(machine.osName == "Ubuntu Server" && machine.osVersion == "26.04 LTS")
precondition(machine.cpuArchitecture == "aarch64")
precondition(machine.memoryBytes == UInt64(8589934592) && machine.cpuCores == 4)
precondition(reference.startedAtUnixSeconds == 1792767590.517391)
precondition(reference.startedAt.timeIntervalSince1970 == reference.startedAtUnixSeconds)
precondition(reference.startTemperatureC == 41.35 && reference.endTemperatureC == 66.65)
precondition(reference.warmupSeconds == 1248)
let scores = Dictionary(uniqueKeysWithValues: reference.results.map { ($0.benchmark, $0.score) })
precondition(scores == ["single_core": 47231.227029007096, "multi_core": 181953.17419139884])
func expectMeasurementOutcome(_ result: BenchmarkResult, benchmarkName: String) {
    if result.success {
        precondition(result.score != nil && result.piEquivalent != nil)
        precondition(result.score! > 0)
        precondition(result.piEquivalent == result.score! / scores[benchmarkName]!)
    } else {
        precondition(result.status == 2)
        precondition(result.reason == "measurement window was too short to complete any work")
        precondition(result.iterations == 0)
        precondition(result.score == nil && result.piEquivalent == nil)
    }
}
expectMeasurementOutcome(result, benchmarkName: "single_core")
for (benchmark, name) in [(Benchmark.singleCore, "single_core"), (.multiCore, "multi_core")] {
    let result = try BenchmarkRunner().run(benchmark, warmupSeconds: 0, measurementSeconds: 0.05)
    let resultMachine: ReferenceMachine = result.referenceMachine
    let resultReference: ReferenceRun = result.referenceRun
    precondition(resultMachine.name == machine.name && resultMachine.shortName == machine.shortName)
    precondition(resultMachine.memoryBytes == machine.memoryBytes && resultMachine.cpuCores == machine.cpuCores)
    precondition(resultReference.startedAtUnixSeconds == reference.startedAtUnixSeconds)
    precondition(resultReference.warmupSeconds == reference.warmupSeconds)
    let resultScores = Dictionary(uniqueKeysWithValues: resultReference.results.map { ($0.benchmark, $0.score) })
    precondition(resultScores == scores)
    expectMeasurementOutcome(result, benchmarkName: name)
}
var copiedResults = result.referenceRun.results
copiedResults.removeAll()
precondition(result.referenceRun.results.count == 2 && piReferenceRun.results.count == 2)
print("Public Swift pi consumer verified")
'''
    main.write_text(source, encoding='utf-8')
    # No test-only macOS 13 target override: exercise the shipped deployment target.
    output = execute(['swift', 'run', 'Consumer'], consumer, environment)
    assert 'Public Swift pi consumer verified' in output
    try:
        with monkeypatch.context() as scoped:
            # Swift emits the Unicode source path as UTF-8 even with an ASCII parent locale.
            scoped.setattr(subprocess, '_text_encoding', lambda: 'ascii', raising=False)
            scoped.setattr(locale, 'getpreferredencoding', lambda _setlocale=True: 'ascii')
            for expression in (
                'mutableResult.referenceMachine = piReferenceMachine',
                'mutableResult.referenceRun = piReferenceRun',
                'var mutableReference = result.referenceRun\nmutableReference.results.removeAll()',
            ):
                main.write_text(source + '\nvar mutableResult = result\n' + expression + '\n', encoding='utf-8')
                rejected = subprocess.run(['swift', 'build', '--target', 'Consumer'], cwd=consumer,
                                          env=environment, capture_output=True, encoding='utf-8',
                                          check=False, timeout=300)
                diagnostic = rejected.stdout + rejected.stderr
                assert rejected.returncode != 0, expression
                assert 'get-only property' in diagnostic or "'let' constant" in diagnostic, diagnostic
    finally:
        main.write_text(source, encoding='utf-8')
