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
import math
import os
import platform
import re
import runpy
import shutil
import subprocess
import sys
import tarfile
import venv
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

import pytest

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


@pytest.mark.parametrize('configuration', [(encoding, count, invalid)
    for encoding in ('ascii', 'cp1251', 'utf-8') for count, invalid in ((0, False), (1, False), (2, False), (2, True))])
def test_source_install_cli_capture_matches_its_echo_stream(  # noqa: PLR0915 - one installed CLI process across its preparation and verification steps
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, configuration: Tuple[str, int, bool],
) -> None:
    """Validate the isolated install commands as well as decoded CLI output.

    Every install/API/dependency check must propagate failure, both CLI entry
    points must run outside the checkout, and inherited Cargo directories must
    be hidden so this gate actually exercises automatic toolchain bootstrap.
    Running the entry point also prevents a removed main call from silently
    disabling CI; a malformed child result must fail before it is echoed.
    """
    encoding, completed_count, invalid_output = configuration
    original_run = subprocess.run
    captured: List[subprocess.CompletedProcess] = []
    calls: List[Tuple[List[str], Dict[str, Any]]] = []
    tool_paths = [tmp_path / name for name in ('other-tools', 'cargo-tools', 'windows-cargo-tools')]
    for path in tool_paths:
        path.mkdir()
    (tool_paths[1] / 'cargo').touch()
    (tool_paths[2] / 'cargo.exe').touch()
    monkeypatch.setenv('PATH', os.pathsep.join(map(str, tool_paths)))
    program = 'COMPLETED_COUNT = ' + str(completed_count) + '''
from benchset import BenchmarkResult, BenchmarkRunner, cli
calls = 0
def completed(_self, name, *, warmup_seconds, measurement_seconds, **_options):
    global calls
    calls += 1
    if calls > COMPLETED_COUNT:
        return BenchmarkResult(name, reason='measurement window was too short to complete any work', workers=1,
                               warmup_seconds=warmup_seconds, measurement_seconds=measurement_seconds)
    return BenchmarkResult(name, status='completed', score=1, pi_equivalent=1, workers=1,
                           warmup_seconds=warmup_seconds, measurement_seconds=measurement_seconds)
BenchmarkRunner.run = completed
raise SystemExit(cli.main(['--warmup=0', '--duration=.05']))
'''

    def run_step(arguments: List[str], **options: Any) -> subprocess.CompletedProcess:
        calls.append((arguments, options.copy()))
        if options.get('capture_output'):
            options['env'] = dict(options['env'], PYTHONPATH=str(ROOT))
            result = original_run([sys.executable, '-c', program], **options)
            if invalid_output:
                result.stdout = result.stdout.replace(' x pi', ' x other').replace(' \u00d7 pi', ' \u00d7 other')
            captured.append(result)
            return result
        return subprocess.CompletedProcess(arguments, 0)

    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding=encoding)
    errors = io.StringIO()
    with monkeypatch.context() as scoped:
        scoped.setenv('PYTHONIOENCODING', 'utf-8')
        scoped.setattr(sys, 'stdout', stream)
        scoped.setattr(sys, 'stderr', errors)
        scoped.setattr(subprocess, '_text_encoding', lambda: 'ascii', raising=False)
        scoped.setattr(locale, 'getpreferredencoding', lambda _setlocale=True: 'ascii')
        scoped.setattr(subprocess, 'run', run_step)
        scoped.setattr(shutil, 'which', lambda *_args, **_kwargs: None)
        scoped.setattr(sys, 'path', sys.path.copy())
        if invalid_output:
            with pytest.raises(RuntimeError, match='invalid completed result'):
                runpy.run_path(str(ROOT / 'scripts/check_source_install.py'), run_name='__main__')
        else:
            runpy.run_path(str(ROOT / 'scripts/check_source_install.py'), run_name='__main__')
        stream.flush()
    text = buffer.getvalue().decode(encoding)
    stream.detach()
    if invalid_output:
        assert len(captured) == 1
        assert len(calls) == 4
        assert not text
        assert not errors.getvalue()
        return
    assert len(captured) == 2
    assert text.count('1 \u00d7 pi' if encoding == 'utf-8' else '1 x pi') == (6 if completed_count == 2 else completed_count * 2)
    assert errors.getvalue().count('measurement window was too short to complete any work') == (0 if completed_count == 2 else 2)
    assert len(calls) == 6
    venv = Path(calls[0][0][-1])
    python = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    console = venv / ('Scripts/benchset.exe' if os.name == 'nt' else 'bin/benchset')
    assert calls[0] == ([sys.executable, '-m', 'venv', str(venv)], {'check': True})
    assert calls[1][0] == [str(python), '-m', 'pip', 'install', str(ROOT)]
    assert calls[2][0] == [str(python), '-c', INSTALLED_API_CHECK]
    assert calls[3][0] == [str(console), '--warmup', '0', '--duration', '0.05']
    assert calls[4][0] == [str(python), '-m', 'benchset', '--warmup', '0', '--duration', '0.05']
    assert calls[5][0] == [str(python), '-m', 'pip', 'check']
    for index, (_arguments, options) in enumerate(calls[1:], 1):
        assert options['cwd'] == venv.parent
        assert options['env']['PATH'] == str(tool_paths[0])
        assert options['env']['PYTHONIOENCODING'] == encoding
        assert options['check'] is (index not in (3, 4))


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
    """Compile shipped Rust APIs as an external crate, including the suite fields.

    In-crate tests can access private fields. External progress/sample readers
    protect diagnostics, cancellation, defaults and suite types from accidental
    visibility loss while also checking their useful runtime relationships.
    """
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
    # Extend the published example with a suite check that does no benchmark work.
    example = (unpacked / 'examples/native/rust.rs').read_text(encoding='utf-8')
    suite_check = '''fn inspect_progress(progress: benchset::Progress) {
    let phase: benchset::Phase = progress.phase;
    assert!(progress.workers >= 1);
    assert!((0.0..=1.0).contains(&progress.fraction));
    assert!(progress.phase_elapsed_seconds >= 0.0);
    assert!(progress.phase_duration_seconds >= 0.0);
    println!("Progress {:?}: {}", phase, progress.iterations);
}
fn inspect_sample(sample: &benchset::Sample) {
    assert!(sample.elapsed_seconds >= 0.0 && sample.duration_seconds > 0.0);
    assert_eq!(sample.iterations_per_second, sample.iterations as f64 / sample.duration_seconds);
}
fn main() {
    assert_eq!(benchset::BENCHMARKS.map(|benchmark| benchmark.name()), ["single_core", "multi_core"]);
    assert_eq!(Options::default().warmup.as_secs_f64(), benchset::DEFAULT_WARMUP_SECONDS);
    assert_eq!(Options::default().measurement.as_secs_f64(), benchset::DEFAULT_MEASUREMENT_SECONDS);
    let cancelled = Cancellation::default();
    assert!(!cancelled.is_cancelled());
    cancelled.cancel();
    assert!(cancelled.clone().is_cancelled());
    // Public calibration fields must remain accessible outside the crate even
    // when ffi.rs can still read them through pub(crate) visibility.
    let machine: &benchset::ReferenceMachine = &benchset::PI_REFERENCE_MACHINE;
    assert_eq!(machine.cpu_architecture, "aarch64");
    let reference: &benchset::ReferenceRun = &benchset::PI_REFERENCE_RUN;
    assert_eq!(reference.started_at_unix_seconds, 1792767590.517391);
    assert_eq!(reference.start_temperature_c, 41.35);
    assert_eq!(reference.end_temperature_c, 66.65);
    let entries: &[benchset::ReferenceBenchmarkResult] = reference.results;
    assert_eq!(entries.iter().map(|entry| (entry.benchmark, entry.score)).collect::<Vec<_>>(),
               vec![("single_core", 47231.227029007096), ("multi_core", 181953.17419139884)]);
    let suite: benchset::SuiteResult = benchset::run_all(
        Options { warmup: Duration::ZERO, measurement: Duration::ZERO },
        &Cancellation::default(), |_, _| {},
    );
    let results: Vec<benchset::BenchmarkResult> = suite.results;
    let scores: benchset::AggregatedScores = suite.aggregated_scores;
    let full_pi: Option<f64> = scores.pi_equivalent;
    assert_eq!(results.len(), 1);
    assert_eq!(results[0].status, Status::Failed);
    assert!(full_pi.is_none());
    assert!(benchset::SuiteResult::new(results).aggregated_scores.pi_equivalent.is_none());
'''
    assert example.count('fn main() {') == 1
    diagnostics = '''
    assert_eq!(result.benchmark, Benchmark::SingleCore);
    assert_eq!(result.version, benchset::VERSION);
    assert!(result.workers >= 1);
    assert_eq!((result.warmup_seconds, result.measurement_seconds), (0.1, 0.2));
    assert_eq!(result.samples.iter().map(|sample| sample.iterations).sum::<u64>(), result.iterations);
    for sample in &result.samples { inspect_sample(sample); }
    if let Some(variation) = result.variation_percent { assert!(variation.is_finite() && variation >= 0.0); }
    assert!(!result.unstable || result.variation_percent.is_some());
    println!("Checksum {}", result.checksum);
'''
    client = example.replace('fn main() {', suite_check, 1).replace('_| {}', 'progress| inspect_progress(progress)')
    client = client.replace('    drop(result);', diagnostics + '    drop(result);')
    (consumer / 'src/main.rs').write_text(client, encoding='utf-8')
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
@pytest.mark.parametrize('include_summary', [False, True])
def test_installed_cli_verifier_accepts_supported_stream_encodings(
    installed_cli_results: Tuple[benchset.BenchmarkResult, ...], encoding: str, include_summary: bool,
) -> None:
    """Accept the whole rendered suite, including its summary, in supported encodings.

    Individual records alone cannot reveal a verifier that misreads a valid
    aggregate summary as an extra sample row from the installed CLI.
    """
    from benchset import SuiteResult  # noqa: PLC0415

    text = '\n'.join(render_installed_cli_result(result, encoding) for result in installed_cli_results)
    if include_summary:
        text += render_installed_cli_result(SuiteResult.from_results(installed_cli_results), encoding)
    assert ('\u00d7 pi' if encoding == 'utf-8' else 'x pi') in text
    verify_cli(text)


def render_installed_cli_result(
    result: Union[benchset.BenchmarkResult, benchset.SuiteResult], encoding: str = 'utf-8',
) -> str:
    """Render either result form with the real CLI writer and stream encoding."""
    from benchset import SuiteResult, cli  # noqa: PLC0415

    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding=encoding, newline='\n')
    if isinstance(result, SuiteResult):
        cli.render_suite(result, stream)
    else:
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


@pytest.mark.parametrize('control', ['\r', '\x1b'])
def test_installed_cli_verifier_rejects_controls_inside_a_suite_summary(
    installed_cli_results: Tuple[benchset.BenchmarkResult, ...], control: str,
) -> None:
    """Reject terminal controls before a summary can hide them from record parsing.

    Each stream has only one kind of control character; accepting it would hide
    accidental terminal-mode output in an otherwise valid installed CLI run.
    """
    from benchset import SuiteResult  # noqa: PLC0415

    summary = render_installed_cli_result(SuiteResult.from_results(installed_cli_results))
    output = '\n'.join(render_installed_cli_result(result) for result in installed_cli_results)
    output += summary.replace(' : ', ' : ' + control, 1)
    with pytest.raises(RuntimeError):
        verify_cli(output)


@pytest.mark.parametrize('completed_count', [0, 1, 2])
def test_installed_cli_verifier_accepts_windows_line_endings(
    installed_cli_results: Tuple[benchset.BenchmarkResult, ...], completed_count: int,
) -> None:
    """Normalize CRLF in complete success/no-work streams, including the summary."""
    from benchset import SuiteResult  # noqa: PLC0415

    results = installed_cli_results[:completed_count]
    output = '\n'.join(render_installed_cli_result(result)
                       for result in installed_cli_results[:completed_count])
    errors = ''
    if completed_count < 2:
        failed = replace(installed_cli_results[completed_count], status='failed',
                         reason='measurement window was too short to complete any work',
                         score=None, pi_equivalent=None, iterations=0, checksum=0, samples=())
        errors = render_installed_cli_result(failed)
        results += (failed,)
    summary = render_installed_cli_result(SuiteResult.from_results(results))
    if completed_count < 2:
        errors += summary
    else:
        output += summary
    verify_cli(output.replace('\n', '\r\n'), errors.replace('\n', '\r\n'), int(completed_count < 2))


@pytest.mark.parametrize('encoding', ['utf-8', 'ascii', 'cp1251'])
@pytest.mark.parametrize('completed_count', [0, 1, 2])
@pytest.mark.parametrize('zero_sample', [False, True])
def test_installed_cli_verifier_accepts_only_completed_prefix_or_exact_no_work(
    installed_cli_results: Tuple[benchset.BenchmarkResult, ...], encoding: str, completed_count: int, zero_sample: bool,
) -> None:
    """Accept complete CLI streams for success or an honest stopped no-work suite.

    A failure summary belongs on stderr after the failed record; omitting it
    from fixtures can hide rejection of valid output on slow or loaded hosts.
    """
    from benchset import Sample, SuiteResult  # noqa: PLC0415

    results = installed_cli_results[:completed_count]
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
        results += (failed,)
    summary = render_installed_cli_result(SuiteResult.from_results(results), encoding)
    if completed_count < 2:
        errors += summary
    else:
        output += summary
    verify_cli(output, errors, int(completed_count < 2))


@pytest.mark.parametrize('mutation', [
    'generic failure', 'nonzero iterations', 'present ratio', 'present score',
    'present variation', 'unstable', 'nonzero sample', 'wrong status',
    'wrong order', 'wrong duration', 'wrong warmup', 'extra stderr',
    'failure exit zero', 'failure exit two', 'success exit one', 'success exit minus one',
    'missing success', 'continued after failure',
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
    elif mutation == 'failure exit two':
        code = 2
    elif mutation in ('success exit one', 'success exit minus one'):
        output, errors = output + '\n' + render_installed_cli_result(second), ''
        if mutation == 'success exit minus one':
            code = -1
    elif mutation == 'missing success':
        errors, code = '', 0
    elif mutation == 'continued after failure':
        output += '\n' + render_installed_cli_result(second)
    with pytest.raises(RuntimeError):
        verify_cli(output, errors, code)


@pytest.mark.parametrize(('field', 'value'), [
    ('Reference machine', 'another reference'), ('Unit', 'operations'),
    ('Workers', '0'), ('Workers', '-1'), ('Workers', 'unknown'),
    ('Warmup seconds', '0.1'), ('Measurement seconds', '0.06'),
    ('Status', 'failed'), ('Success', 'False'),
    ('Pi equivalent', '1 x other'), ('Pi equivalent', '1'),
    ('Pi equivalent', '0 x pi'), ('Pi equivalent', '-1 x pi'),
    ('Pi equivalent', 'nan x pi'), ('Pi equivalent', 'inf x pi'),
    ('Version', None), ('duplicate', None), ('extra', None),
])
def test_installed_cli_verifier_rejects_corrupted_completed_result_fields(
    installed_cli_results: Tuple[benchset.BenchmarkResult, ...], field: str, value: Optional[str],
) -> None:
    """Reject plausible CLI regressions before declaring an installed build healthy.

    Correct stop behavior is insufficient when a completed record loses fields,
    duplicates a key, changes its reference/unit, or emits a nonpositive/nonfinite
    multiplier. Only the selected field changes so another error cannot mask it.
    """
    first, second = installed_cli_results
    section = render_installed_cli_result(first)
    if field == 'duplicate':
        section = section.replace('Samples:\n', 'Version : duplicate\nSamples:\n')
    elif field == 'extra':
        section = section.replace('Samples:\n', 'Unexpected : value\nSamples:\n')
    else:
        pattern = r'^\s*' + re.escape(field) + r'\s+ : .*\n'
        section, count = re.subn(pattern, '' if value is None else field + ' : ' + value + '\n',
                                section, count=1, flags=re.MULTILINE)
        assert count == 1
    with pytest.raises(RuntimeError):
        verify_cli(section + '\n' + render_installed_cli_result(second))


@pytest.mark.parametrize(('elapsed', 'duration', 'iterations', 'rate'), [
    (-1, 1, 0, 0), (0, 0, 0, 0), (0, -1, 0, 0),
    (0, 1, -1, 0), (0, 1, 0, -1), (0, 1, 0, 1),
    (float('nan'), 1, 0, 0), (0, float('nan'), 0, 0),
    (0, 1, 0, float('nan')), (0, 1, 0, float('inf')),
])
@pytest.mark.parametrize('damaged_index', [0, 1, 2])
def test_installed_cli_verifier_requires_zero_work_samples_with_valid_time_windows(  # noqa: PLR0913 — Independent components and history positions.
    installed_cli_results: Tuple[benchset.BenchmarkResult, ...],
    elapsed: float, duration: float, iterations: int, rate: float, damaged_index: int,
) -> None:
    """Allow an honest short-window failure without accepting malformed sample data.

    Each case changes one sample component, protecting the time/rate conditions
    individually rather than rejecting only a row whose every value is wrong.
    Put that row at every position so checking only the last sample cannot hide
    malformed earlier history behind a valid final sample.
    """
    from benchset import Sample  # noqa: PLC0415

    first, second = installed_cli_results
    samples = [Sample(0.01, 0.01, 0, 0), Sample(0.02, 0.01, 0, 0), Sample(0.05, 0.03, 0, 0)]
    samples[damaged_index] = Sample(elapsed, duration, iterations, rate)
    failed = replace(second, status='failed', reason='measurement window was too short to complete any work',
                     score=None, pi_equivalent=None, iterations=0, checksum=0, samples=tuple(samples))
    with pytest.raises(RuntimeError):
        verify_cli(render_installed_cli_result(first), render_installed_cli_result(failed), 1)


@pytest.mark.parametrize('damage', ['wrong header', 'missing rows'])
def test_installed_cli_verifier_requires_a_recognizable_nonempty_sample_table(
    installed_cli_results: Tuple[benchset.BenchmarkResult, ...], damage: str,
) -> None:
    """Require both a known table header and data rows for a reported no-work sample.

    Start with a valid failure record and change only its table structure so
    invalid numeric values cannot mask either missing structural requirement.
    """
    from benchset import Sample  # noqa: PLC0415

    first, second = installed_cli_results
    failed = replace(second, status='failed', reason='measurement window was too short to complete any work',
                     score=None, pi_equivalent=None, iterations=0, checksum=0,
                     samples=(Sample(0.05, 0.05, 0, 0),))
    prefix, samples = render_installed_cli_result(failed).split('Samples:\n', 1)
    rows = samples.splitlines()
    if damage == 'wrong header':
        rows[0] = 'unrecognized | sample | table | header'
    else:
        rows = rows[:1]
    with pytest.raises(RuntimeError):
        verify_cli(render_installed_cli_result(first), prefix + 'Samples:\n' + '\n'.join(rows) + '\n', 1)


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


@pytest.mark.skipif(sys.platform != 'darwin', reason='Mach-O install names and code signing')
@pytest.mark.parametrize('target', ['aarch64-apple-darwin', 'x86_64-apple-darwin'])
def test_macos_native_zip_has_relocatable_install_name_and_valid_signature(
    source_snapshot: Path, rust_tools: Tuple[Dict[str, str], str, str], tmp_path: Path, target: str,
) -> None:
    environment, _cargo, _host = rust_tools
    execute([sys.executable, 'scripts/package_native.py', '--target', target], source_snapshot, environment)
    archive = source_snapshot / 'release-artifacts' / ('benchset-' + target + '.zip')
    with zipfile.ZipFile(archive) as package:
        package.extractall(tmp_path)
    library = tmp_path / 'lib/libbenchset.dylib'
    identity = execute(['otool', '-D', str(library)], tmp_path, dict(os.environ))
    assert identity.splitlines()[1:] == ['@rpath/libbenchset.dylib']
    execute(['codesign', '--verify', '--strict', str(library)], tmp_path, dict(os.environ))


def test_unpacked_native_zip_links_its_public_header_and_library(
    native_bundle: Path, tmp_path: Path,
) -> None:
    """Run native consumers and verify the C example reports real progress and Pi units.

    Linking alone cannot detect a correct callback/result displayed with the
    wrong percentage or ratio. The comparison permits the example's deliberate
    decimal rounding, and honest no-work runs retain their exact failure reason.
    """
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
            if source == 'examples/native/c.c':
                frames = re.findall(r'^phase=(\d+) progress=([\d.]+)%$', result.stdout, re.MULTILINE)
                assert frames
                assert all(0 <= float(percent) <= 100 for _phase, percent in frames)
                assert int(frames[-1][0]) == (3 if result.returncode == 0 else 5)
                if result.returncode == 0:
                    assert float(frames[-1][1]) == 100
                    record = re.search(r'benchset \S+: ([\d.eE+-]+) iterations/s, ([\d.eE+-]+) x pi', result.stdout)
                    assert record is not None
                    score, equivalent = map(float, record.groups())
                    assert score > 0
                    assert equivalent > 0
                    assert math.isclose(equivalent, score / 47231.227029007096,
                                        rel_tol=5e-6, abs_tol=0.000501 / 47231.227029007096)


@pytest.mark.skipif(sys.platform != 'darwin', reason='XCFramework and Swift package target macOS')
def test_unpacked_swift_package_public_consumer_and_both_xcframework_slices(  # noqa: PLR0915 - one complete offline package and public consumer
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise shipped Swift types without @testable and inspect both native slices.

    Explicit types and realistic progress/sample diagnostics catch accidental
    loss of public visibility, which privileged Swift unit imports cannot detect.
    """
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

// Invalid timing exercises the public suite surface without doing benchmark work.
let suite: SuiteResult = try BenchmarkRunner().runAll(warmupSeconds: -1, measurementSeconds: 0.05)
let suiteResults: [BenchmarkResult] = suite.results
let suiteScores: AggregatedScores = suite.aggregatedScores
let suiteEquivalent: Double? = suiteScores.piEquivalent
precondition(suiteResults.count == 1 && suiteResults[0].benchmark == .singleCore)
precondition(!suiteResults[0].success && suiteEquivalent == nil)
precondition(SuiteResult(results: suiteResults).aggregatedScores.piEquivalent == nil)

// Neither run() nor a validation failure needs to initialize the lazy Swift
// metadata globals. First access happens concurrently inside a live callback.
let coldRunner = BenchmarkRunner()
let coldFailure = try coldRunner.run(.singleCore, warmupSeconds: -1, measurementSeconds: 0.05)
precondition(!coldFailure.success)
var observedReference = false
let coldCancelled = try coldRunner.run(.singleCore, warmupSeconds: 10, measurementSeconds: 1) { progress in
    precondition(progress.workers >= 1 && (0...1).contains(progress.fraction))
    precondition(progress.phaseElapsedSeconds >= 0)
    precondition(progress.phaseDurationSeconds == (progress.phase == 1 ? 10 : 1))
    print("Progress", progress.iterations)
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
print("Example outcome", result.status)
let typedMachine: ReferenceMachine = machine
let typedReference: ReferenceRun = reference
precondition(machine.name == "Raspberry Pi 5 Model B Rev 1.0")
precondition(machine.shortName == "pi")
precondition(machine.osName == "Ubuntu Server" && machine.osVersion == "26.04 LTS")
precondition(machine.cpuArchitecture == "aarch64")
precondition(machine.memoryBytes == UInt64(8589934592) && machine.cpuCores == 4)
precondition(piReferenceMachine.name == machine.name)
precondition(reference.startedAtUnixSeconds == 1792767590.517391)
precondition(reference.startedAt.timeIntervalSince1970 == reference.startedAtUnixSeconds)
precondition(reference.startTemperatureC == 41.35 && reference.endTemperatureC == 66.65)
precondition(reference.warmupSeconds == 1248)
let scores = Dictionary(uniqueKeysWithValues: reference.results.map { ($0.benchmark, $0.score) })
precondition(scores == ["single_core": 47231.227029007096, "multi_core": 181953.17419139884])
func expectMeasurementOutcome(_ result: BenchmarkResult, benchmarkName: String,
                              warmup: Double, measurement: Double) {
    precondition(result.workers >= 1 && result.elapsedSeconds >= 0)
    precondition(result.warmupSeconds == warmup && result.measurementSeconds == measurement)
    precondition(result.samples.reduce(UInt64(0)) { $0 + $1.iterations } == result.iterations)
    for sample in result.samples {
        precondition(sample.elapsedSeconds >= 0 && sample.durationSeconds > 0)
        precondition(sample.iterationsPerSecond == Double(sample.iterations) / sample.durationSeconds)
    }
    if let variation = result.variationPercent { precondition(variation.isFinite && variation >= 0) }
    precondition(!result.unstable || result.variationPercent != nil)
    print("Diagnostics", result.checksum, result.samples.count)
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
expectMeasurementOutcome(result, benchmarkName: "single_core", warmup: 0.1, measurement: 0.2)
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
    expectMeasurementOutcome(result, benchmarkName: name, warmup: 0, measurement: 0.05)
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
    assert 'Reference: Raspberry Pi 5 Model B Rev 1.0 (pi)' in output
    if 'Example outcome 0\n' in output:
        record = re.search(r'benchset \S+: ([\d.eE+-]+) iterations/s, ([\d.eE+-]+) x pi', output)
        assert record is not None
        score, equivalent = map(float, record.groups())
        assert score > 0
        assert math.isclose(equivalent, score / 47231.227029007096, rel_tol=1e-12)
    else:
        assert 'Example outcome 2\n' in output
        assert 'measurement window was too short to complete any work' in output
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
