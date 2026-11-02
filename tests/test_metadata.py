"""Native-free public metadata and Rust-generated package snapshots."""

import errno
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Type

import pytest

from scripts.cargo import build, publish

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('argument', [None, '--help', '--version'])
def test_zip_import_keeps_metadata_and_cli_inspection_native_free(tmp_path: Path, argument: Optional[str]) -> None:
    """Imports and metadata inspection work from ZIP packages without loading Rust.

    Archive Python files and the generated data, omitting native binaries.
    A fresh interpreter must use that ZIP and preserve public DTO serialization,
    defaults and CLI help/version rather than treating __file__ as a disk path.
    """
    archive = tmp_path / 'benchset-package.zip'
    with zipfile.ZipFile(archive, 'w') as package:
        for source in (ROOT / 'benchset').glob('*.py'):
            package.write(source, 'benchset/' + source.name)
        package.write(ROOT / 'benchset/_metadata.json', 'benchset/_metadata.json')
    program = '''
import ctypes
import json
import sys
from dataclasses import asdict
def forbidden(*args, **kwargs):
    raise AssertionError('ZIP metadata must not load the native library')
ctypes.CDLL = forbidden
sys.path.insert(0, sys.argv[1])
import benchset
assert benchset.__file__.startswith(sys.argv[1])
assert benchset.BENCHMARKS == ('single_core', 'multi_core')
assert type(benchset.DEFAULT_WARMUP_SECONDS) is float
assert type(benchset.DEFAULT_MEASUREMENT_SECONDS) is float
result = benchset.BenchmarkResult('single_core')
assert result.reference_machine is benchset.PI_REFERENCE_MACHINE
assert result.reference_run is benchset.PI_REFERENCE_RUN
assert json.loads(json.dumps(asdict(result)))['reference_machine']['short_name'] == 'pi'
if len(sys.argv) > 2:
    from benchset.cli import main
    assert main([sys.argv[2]]) == 0
'''
    command = [sys.executable, '-c', program, str(archive)]
    if argument is not None:
        command.append(argument)
    environment = dict(os.environ)
    for key in ('COVERAGE_PROCESS_START', 'COVERAGE_FILE'):
        environment.pop(key, None)
    result = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stderr
    if argument == '--help':
        assert 'usage: benchset' in result.stdout
    elif argument == '--version':
        assert result.stdout.strip()
    else:
        assert not result.stdout


@pytest.mark.skipif(os.name == 'nt', reason='Windows filenames cannot contain line breaks')
@pytest.mark.skipif(shutil.which('cargo') is None, reason='Cargo checks metadata path transport')
@pytest.mark.parametrize('separator', ['\n', '\r', '\r\n'])
def test_cargo_metadata_preserves_target_path_bytes(tmp_path: Path, separator: str) -> None:
    """Cargo's line protocol must preserve control characters in output paths.

    Real Cargo output catches truncation before Python receives the JSON event;
    a mocked event would conceal corruption inside the build-script protocol.
    """
    cargo = shutil.which('cargo')
    assert cargo is not None
    target = tmp_path / ('target' + separator + 'π')
    target.mkdir()
    environment = dict(os.environ, CARGO_TARGET_DIR=str(target))
    metadata = json.loads(build(ROOT, [cargo, 'check', '--locked'], environment))
    assert metadata == json.loads((ROOT / 'benchset/_metadata.json').read_text(encoding='utf-8'))


@pytest.mark.skipif(os.name == 'nt', reason='POSIX output path byte semantics')
@pytest.mark.skipif(shutil.which('rustc') is None, reason='Rust compiler checks the real metadata exporter')
@pytest.mark.parametrize('separator', ['\n', '\r', '\r\n', '\udcff'])
def test_metadata_exporter_preserves_output_path_bytes(tmp_path: Path, separator: str) -> None:
    """The Rust exporter transports exact OS bytes without Cargo-line truncation.

    Compile at a UTF-8 path before providing OUT_DIR at runtime: rustc parses
    its command-line arguments as UTF-8 and panics on non-UTF8 target paths.
    The exporter itself must preserve those bytes wherever the filesystem
    supports them. Real Cargo integration above separately checks its events.
    """
    compiler = shutil.which('rustc')
    assert compiler is not None
    executable = tmp_path / 'exporter'
    subprocess.run([compiler, '--edition=2021', str(ROOT / 'native/build.rs'), '-o', str(executable)], check=True)
    destination = tmp_path / ('output' + separator + 'π')
    try:
        destination.mkdir()
    except OSError as error:
        if separator == '\udcff' and error.errno == errno.EILSEQ:
            pytest.skip('This filesystem rejects non-UTF8 filenames')
        raise
    result = subprocess.run([str(executable)], env=dict(os.environ, OUT_DIR=str(destination)),
                            capture_output=True, text=True, check=True)
    prefix = 'cargo:rustc-env=BENCHSET_METADATA_PATH_HEX='
    paths = [os.fsdecode(bytes.fromhex(line[len(prefix):])) for line in result.stdout.splitlines() if line.startswith(prefix)]
    assert paths == [str(destination / 'benchset-metadata.json')]
    assert json.loads(Path(paths[0]).read_bytes()) == json.loads((ROOT / 'benchset/_metadata.json').read_bytes())


@pytest.mark.parametrize('handler', ['thread', 'custom', 'ignored'])
@pytest.mark.parametrize('failure', [False, True])
def test_publication_preserves_thread_and_signal_handler_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handler: str, failure: bool,
) -> None:
    """Publication works off the main thread and preserves caller SIGINT policy on every exit."""
    package = tmp_path / 'package'
    compiled = tmp_path / 'libbenchset.so'
    compiled.write_bytes(b'new library')
    replace = Path.replace

    def failed_replace(source: Path, destination: Path) -> Path:
        assert signal.getsignal(signal.SIGINT) is expected
        if failure and source.name == '_metadata.json':
            raise OSError('metadata rename failed')
        return replace(source, destination)

    monkeypatch.setattr(Path, 'replace', failed_replace)
    expected = ((lambda _signum, _frame: None) if handler == 'custom' else
                signal.SIG_IGN if handler == 'ignored' else signal.default_int_handler)
    previous = signal.signal(signal.SIGINT, expected)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            def install() -> Path:
                if handler == 'thread':
                    return executor.submit(publish, package, compiled, b'new metadata').result(timeout=5)
                return publish(package, compiled, b'new metadata')

            if failure:
                with pytest.raises(OSError, match='metadata rename failed'):
                    install()
                assert not (package / '_native' / compiled.name).exists()
            else:
                assert install().read_bytes() == b'new library'
                assert (package / '_metadata.json').read_bytes() == b'new metadata'
        assert signal.getsignal(signal.SIGINT) is expected
    finally:
        signal.signal(signal.SIGINT, previous)


@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('error_type', [OSError, KeyboardInterrupt])
def test_sigint_during_publication_rollback_keeps_complete_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool, error_type: Type[BaseException],
) -> None:
    """Finish rollback before reporting Ctrl+C, including a repeated interruption.

    Metadata replacement fails after the library has changed; SIGINT then arrives
    just before restoring the previous library or removing a first installation.
    The previous signal handler must also survive the failed publication.
    """
    package = tmp_path / 'package'
    installed = package / '_native/libbenchset.so'
    installed.parent.mkdir(parents=True)
    snapshot = package / '_metadata.json'
    if existing:
        installed.write_bytes(b'old library')
        snapshot.write_bytes(b'old metadata')
    compiled = tmp_path / installed.name
    compiled.write_bytes(b'new library')
    replace, unlink = Path.replace, Path.unlink
    fired: List[bool] = []

    def interrupt() -> None:
        fired.append(True)
        signal.raise_signal(signal.SIGINT)

    def interrupted_replace(source: Path, destination: Path) -> Path:
        if source.name == snapshot.name:
            raise error_type('metadata rename failed')
        if source.name == 'previous-library':
            interrupt()
        return replace(source, destination)

    def interrupted_unlink(path: Path, **options: bool) -> None:
        if path == installed:
            interrupt()
        unlink(path, **options)

    monkeypatch.setattr(Path, 'replace', interrupted_replace)
    monkeypatch.setattr(Path, 'unlink', interrupted_unlink)
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            publish(package, compiled, b'new metadata')
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    finally:
        signal.signal(signal.SIGINT, previous)
    assert fired == [True]
    if existing:
        assert installed.read_bytes() == b'old library'
        assert snapshot.read_bytes() == b'old metadata'
        assert list(installed.parent.iterdir()) == [installed]
    else:
        assert not snapshot.exists()
        assert list(installed.parent.iterdir()) == []


@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('boundary', ['library', 'metadata'])
def test_publication_signal_after_rename_keeps_one_complete_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool, boundary: str,
) -> None:
    """SIGINT after either completed rename must not mix library and metadata generations.

    The first rename needs rollback; after the metadata rename both new files
    are already committed. Inject a real signal after the filesystem mutation,
    including first installations without a previous library to restore.
    """
    package = tmp_path / 'package'
    installed = package / '_native/libbenchset.so'
    installed.parent.mkdir(parents=True)
    snapshot = package / '_metadata.json'
    if existing:
        installed.write_bytes(b'old library')
        snapshot.write_bytes(b'old metadata')
    compiled = tmp_path / installed.name
    compiled.write_bytes(b'new library')
    replace = Path.replace
    fired: List[bool] = []

    def interrupted_replace(source: Path, destination: Path) -> Path:
        result = replace(source, destination)
        name = compiled.name if boundary == 'library' else snapshot.name
        if source.name == name and not fired:
            fired.append(True)
            signal.raise_signal(signal.SIGINT)
        return result

    monkeypatch.setattr(Path, 'replace', interrupted_replace)
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            publish(package, compiled, b'new metadata')
    finally:
        signal.signal(signal.SIGINT, previous)
    assert fired == [True]
    if boundary == 'metadata' or existing:
        generation = b'new' if boundary == 'metadata' else b'old'
        assert installed.read_bytes() == generation + b' library'
        assert snapshot.read_bytes() == generation + b' metadata'
        assert list(installed.parent.iterdir()) == [installed]
    else:
        assert not snapshot.exists()
        assert list(installed.parent.iterdir()) == []


def test_python_metadata_and_signature_defaults_are_native_free() -> None:
    """Keep ordered names, float defaults and calibration readable without Rust.

    A fresh interpreter forbids CDLL before importing benchset, so a cached
    library cannot conceal eager loading while replacing handwritten constants.
    Signature inspection also catches defaults becoming None or a sentinel.
    """
    program = '''
import ctypes
import inspect
import json
from dataclasses import asdict
def forbidden(*args, **kwargs):
    raise AssertionError('metadata must not load the native library')
ctypes.CDLL = forbidden
import benchset
assert benchset.BENCHMARKS == ('single_core', 'multi_core')
assert isinstance(benchset.BENCHMARKS, tuple)
assert type(benchset.DEFAULT_WARMUP_SECONDS) is float
assert type(benchset.DEFAULT_MEASUREMENT_SECONDS) is float
for function in (benchset.run, benchset.run_all, benchset.BenchmarkRunner.run):
    parameters = inspect.signature(function).parameters
    assert parameters['warmup_seconds'].default == 120.0
    assert parameters['measurement_seconds'].default == 60.0
result = benchset.BenchmarkResult('single_core')
assert result.warmup_seconds == 120.0 and result.measurement_seconds == 60.0
assert result.reference_machine is benchset.PI_REFERENCE_MACHINE
assert result.reference_run is benchset.PI_REFERENCE_RUN
payload = json.loads(json.dumps(asdict(result)))
assert payload['reference_machine']['memory_bytes'] == 8589934592
assert type(payload['reference_machine']['memory_bytes']) is int
assert payload['reference_run']['results'] == [
    {'benchmark': 'single_core', 'score': 47231.227029007096},
    {'benchmark': 'multi_core', 'score': 181953.17419139884},
]
assert type(benchset.PI_REFERENCE_RUN.results) is tuple
print('native-free metadata')
'''
    result = subprocess.run([sys.executable, '-c', program], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'native-free metadata\n'
    assert not result.stderr


@pytest.mark.parametrize('failure', [None, 'compile', 'missing', 'duplicate', 'read', 'copy', 'incoming-copy', 'write', 'library-replace', 'replace'])
def test_build_publishes_exact_reported_metadata_atomically(  # noqa: PLR0915 — one publication contract across failure boundaries.
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    """Publish the current Cargo-reported snapshot without guessing an output path.

    A stale file in the conventional target directory and an unrelated build
    event catch path guessing. Compilation/report/read/copy/write failures
    retain both installed files, including failures at either replacement.
    Staging both products before replacing either prevents interrupted copies
    from mixing new metadata with an old library. Failures clean staging files.
    """
    package = tmp_path / 'package'
    package.mkdir()
    installed = package / '_metadata.json'
    installed.write_bytes(b'old complete snapshot')
    library = package / '_native/libbenchset.so'
    library.parent.mkdir()
    library.write_bytes(b'old complete library')
    stale = tmp_path / 'target/release/benchset-metadata.json'
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b'stale metadata')
    fresh = tmp_path / 'custom-output-π-данные/build-script-out/benchset-metadata.json'
    fresh.parent.mkdir(parents=True)
    fresh.write_bytes(b'fresh complete snapshot')
    compiled = fresh.parent / library.name
    compiled.write_bytes(b'fresh complete library')
    records = [
        {'reason': 'compiler-artifact'},
        {'reason': 'build-script-executed', 'env': [['UNRELATED_PATH', str(stale)]]},
        {'reason': 'build-script-executed', 'env': [['BENCHSET_METADATA_PATH_HEX', os.fsencode(fresh).hex()]]},
    ]
    environment = {'CARGO_TARGET_DIR': str(tmp_path / 'custom-output-π-данные')}

    def compile_(arguments: list, **options: object) -> subprocess.CompletedProcess:
        assert arguments == ['selected-cargo', 'build', '--message-format=json-render-diagnostics']
        assert options == dict(cwd=tmp_path, env=environment, stdout=subprocess.PIPE, encoding='utf-8', check=True)
        assert installed.read_bytes() == b'old complete snapshot'
        if failure == 'compile':
            raise subprocess.CalledProcessError(1, arguments)
        if failure == 'missing':
            records.pop()
        if failure == 'duplicate':
            records.append(records[-1])
        if failure == 'read':
            fresh.unlink()
        return subprocess.CompletedProcess(arguments, 0, stdout='\n'.join(json.dumps(record, ensure_ascii=False) for record in records))

    monkeypatch.setattr(subprocess, 'run', compile_)
    replace = Path.replace

    def replace_with_reader(source: Path, destination: Path) -> Path:
        assert installed.read_bytes() == b'old complete snapshot'
        expected = (b'fresh complete snapshot' if destination == installed else
                    b'old complete library' if source.name == 'previous-library' else b'fresh complete library')
        assert source.read_bytes() == expected
        if failure == 'replace' and destination == installed:
            raise OSError('publication interrupted')
        if failure == 'library-replace' and destination == library:
            raise OSError('library publication interrupted')
        return replace(source, destination)

    monkeypatch.setattr(Path, 'replace', replace_with_reader)
    write = Path.write_bytes

    def write_with_reader(path: Path, data: bytes) -> int:
        assert library.read_bytes() == b'old complete library'
        assert installed.read_bytes() == b'old complete snapshot'
        if failure == 'write':
            raise OSError('snapshot write interrupted')
        return write(path, data)

    monkeypatch.setattr(Path, 'write_bytes', write_with_reader)
    if failure in ('copy', 'incoming-copy'):
        copy = shutil.copy2

        def interrupted_copy(source: Path, destination: Path) -> Path:
            if failure == 'copy' or source == compiled:
                raise OSError('library copy interrupted')
            return copy(source, destination)
        monkeypatch.setattr(shutil, 'copy2', interrupted_copy)

    def install() -> None:
        metadata = build(tmp_path, ['selected-cargo', 'build'], environment)
        publish(package, compiled, metadata)

    if failure:
        with pytest.raises((OSError, RuntimeError, subprocess.CalledProcessError)):
            install()
        assert installed.read_bytes() == b'old complete snapshot'
        assert library.read_bytes() == b'old complete library'
    else:
        install()
        assert installed.read_bytes() == b'fresh complete snapshot'
        assert library.read_bytes() == b'fresh complete library'
    assert sorted(path for path in package.iterdir() if path.name != '.benchset.publish.lock') == sorted([installed, library.parent])
    assert list(library.parent.iterdir()) == [library]


@pytest.mark.parametrize('error_type', [OSError, KeyboardInterrupt])
def test_first_install_metadata_failure_removes_unpublished_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: Type[BaseException],
) -> None:
    """Leave no half-installed native library when first metadata publication fails.

    An existing installation restores its previous library in the failure
    matrix above; a first installation has no backup and must remove the new
    library instead. Both cases leave only complete generations after failure.
    """
    compiled = tmp_path / 'libbenchset.so'
    compiled.write_bytes(b'fresh library')
    package = tmp_path / 'package'
    replace = Path.replace

    def fail_metadata(source: Path, destination: Path) -> Path:
        if destination.name == '_metadata.json':
            raise error_type('metadata rename interrupted')
        return replace(source, destination)

    monkeypatch.setattr(Path, 'replace', fail_metadata)
    with pytest.raises(error_type, match='metadata rename interrupted'):
        publish(package, compiled, b'fresh metadata')
    assert not (package / '_metadata.json').exists()
    assert list((package / '_native').iterdir()) == []


@pytest.mark.skipif(shutil.which('cargo') is None, reason='Rust snapshot generation needs Cargo')
def test_rust_generated_snapshot_tracks_registry_defaults_and_calibration(tmp_path: Path) -> None:
    """Keep the checked snapshot fresh and derive every field from Rust definitions.

    A small real Cargo project compiles the actual build script. Rebuilding it
    after changing only Rust names/order/defaults/calibration proves rerun paths,
    exact float transport and JSON escaping. Importing a fresh Python package
    with that snapshot catches handwritten fallback copies in the wrappers.
    The historical calibration JSON is
    independent evidence and is deliberately absent from this runtime source.
    """
    project = tmp_path / 'project'
    native = project / 'native'
    (native / 'src').mkdir(parents=True)
    (project / 'Cargo.toml').write_text(
        '[package]\nname="metadata-probe"\nversion="0.0.0"\nedition="2021"\n'
        'build="native/build.rs"\n[lib]\npath="lib.rs"\n',
    )
    (project / 'lib.rs').write_text('')
    for relative in ('build.rs', 'src/catalog.rs', 'src/reference.rs'):
        shutil.copy2(ROOT / 'native' / relative, native / relative)
    environment = dict(os.environ, CARGO_TARGET_DIR=str(tmp_path / 'isolated-target-π-данные'))
    cargo = shutil.which('cargo')
    metadata = build(project, [cargo, 'build', '--offline'], environment)
    snapshot = json.loads(metadata)
    assert snapshot == json.loads((ROOT / 'benchset/_metadata.json').read_text(encoding='utf-8'))
    catalog = native / 'src/catalog.rs'
    catalog.write_text(catalog.read_text().replace(
        '[Benchmark::SingleCore, Benchmark::MultiCore]', '[Benchmark::MultiCore, Benchmark::SingleCore]',
    ).replace('"single_core"', '"renamed"').replace('120.0', '1.2345678901234567').replace('60.0', '9.876543210987654'))
    metadata = build(project, [cargo, 'build', '--offline'], environment)
    changed = json.loads(metadata)
    assert changed['benchmarks'] == [{'id': 2, 'name': 'multi_core'}, {'id': 1, 'name': 'renamed'}]
    assert changed['default_warmup_seconds'] == 1.2345678901234567
    assert changed['default_measurement_seconds'] == 9.876543210987654
    assert changed['reference_machine'] == snapshot['reference_machine']
    assert changed['reference_run'] == snapshot['reference_run']
    reference = native / 'src/reference.rs'
    reference.write_text(reference.read_text(encoding='utf-8').replace(
        '"Raspberry Pi 5 Model B Rev 1.0"', '"quote\\\" slash\\\\ line\\n π\\u{0001}"',
    ).replace('47231.227029007096', '0.12345678901234567').replace('1248.0', '0.9876543210987654')
        .replace('8 * 1024 * 1024 * 1024', 'u64::MAX'), encoding='utf-8')
    metadata = build(project, [cargo, 'build', '--offline'], environment)
    changed = json.loads(metadata)
    assert changed['reference_machine']['name'] == 'quote" slash\\ line\n π\x01'
    assert changed['reference_run']['results'][0]['score'] == 0.12345678901234567
    assert changed['reference_run']['warmup_seconds'] == 0.9876543210987654
    assert changed['reference_machine']['memory_bytes'] == 2**64 - 1
    source = project / 'python-source'
    shutil.copytree(ROOT / 'benchset', source / 'benchset', ignore=shutil.ignore_patterns('_native', '__pycache__'))
    (source / 'benchset/_metadata.json').write_bytes(metadata)
    program = '''
import ctypes
import inspect
import json
def forbidden(*args, **kwargs):
    raise AssertionError('metadata inspection must remain native-free')
ctypes.CDLL = forbidden
import benchset
from benchset.cli import Settings
assert benchset.BENCHMARKS == ('multi_core', 'renamed')
assert benchset.DEFAULT_WARMUP_SECONDS == 1.2345678901234567
assert benchset.DEFAULT_MEASUREMENT_SECONDS == 9.876543210987654
for function in (benchset.run, benchset.run_all, benchset.BenchmarkRunner.run):
    parameters = inspect.signature(function).parameters
    assert parameters['warmup_seconds'].default == benchset.DEFAULT_WARMUP_SECONDS
    assert parameters['measurement_seconds'].default == benchset.DEFAULT_MEASUREMENT_SECONDS
configuration = Settings(_sources=[])
assert configuration.warmup == benchset.DEFAULT_WARMUP_SECONDS
assert configuration.duration == benchset.DEFAULT_MEASUREMENT_SECONDS
result = benchset.BenchmarkResult('renamed')
assert result.warmup_seconds == benchset.DEFAULT_WARMUP_SECONDS
assert result.measurement_seconds == benchset.DEFAULT_MEASUREMENT_SECONDS
assert result.reference_machine is benchset.PI_REFERENCE_MACHINE
assert result.reference_run is benchset.PI_REFERENCE_RUN
assert benchset.PI_REFERENCE_RUN.results[0].score == 0.12345678901234567
assert benchset.PI_REFERENCE_RUN.warmup_seconds == 0.9876543210987654
assert 'π' in benchset.PI_REFERENCE_MACHINE.name
assert benchset.PI_REFERENCE_MACHINE.memory_bytes == 2**64 - 1
print('Rust metadata reaches Python')
'''
    consumer_environment = dict(os.environ, PYTHONPATH=str(source))
    # This deliberately changed independent package is not checkout runtime
    # coverage; inheriting its startup hook would count it as a second library.
    for variable in ('COVERAGE_PROCESS_START', 'COVERAGE_FILE'):
        consumer_environment.pop(variable, None)
    consumer = subprocess.run([sys.executable, '-c', program], cwd=source,
                              env=consumer_environment, capture_output=True, text=True, check=False)
    assert consumer.returncode == 0, consumer.stderr
    assert consumer.stdout == 'Rust metadata reaches Python\n'
    assert not consumer.stderr
    original = reference.read_text(encoding='utf-8')
    for literal, expected in [('1.0', 1.0), ('-0.0', -0.0), ('5e-324', 5e-324), ('1.7976931348623157e308', sys.float_info.max)]:
        reference.write_text(original.replace('0.12345678901234567', literal), encoding='utf-8')
        score = json.loads(build(project, [cargo, 'build', '--offline'], environment))['reference_run']['results'][0]['score']
        assert type(score) is float
        assert score == expected
        assert math.copysign(1, score) == math.copysign(1, expected)
