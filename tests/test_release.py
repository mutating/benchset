from __future__ import annotations

import hashlib
import io
import json
import re
import shlex
import shutil
import subprocess
from dataclasses import replace
from email.message import Message
from pathlib import Path
from typing import Any, Dict, List

import pytest

from scripts import release

REPOSITORY = 'mutating/benchset'
VERSION = '0.0.1'


@pytest.mark.parametrize('token', ['', 'test-private-repository-token'])
@pytest.mark.parametrize('status', [200, 404, 401, 500])
def test_registry_transport_preserves_authentication_json_and_http_failures(
    monkeypatch: pytest.MonkeyPatch, token: str, status: int,
) -> None:
    """Exercise the real registry transport instead of mocking its parsed result.

    Private release lookup needs a complete URL and authorization header. Only
    a genuine 404 means absence; authentication/server failures must abort, and
    diagnostics must not disclose the credential.
    """
    expected = {'id': 123, 'draft': True, 'name': '\u03c0'}
    url = 'https://api.github.com/repos/mutating/benchset/releases/tags/v' + VERSION

    def fetch(request: Any, *, timeout: float) -> io.BytesIO:
        assert request.full_url == url
        assert request.get_method() == 'GET'
        assert request.get_header('User-agent')
        assert 'json' in request.get_header('Accept')
        assert request.get_header('Authorization') == ('Bearer ' + token if token else None)
        assert timeout > 0
        if status != 200:
            raise release.urllib.error.HTTPError(url, status, 'registry failure', Message(), None)
        return io.BytesIO(json.dumps(expected, ensure_ascii=False).encode('utf-8'))

    monkeypatch.setenv('GH_TOKEN', token)
    monkeypatch.setattr(release.urllib.request, 'urlopen', fetch)
    if status in (200, 404):
        assert release.github_release(REPOSITORY, 'v' + VERSION) == (expected if status == 200 else None)
    else:
        with pytest.raises(RuntimeError, match='HTTP ' + str(status)) as error:
            release.github_release(REPOSITORY, 'v' + VERSION)
        if token:
            assert token not in str(error.value)


@pytest.mark.parametrize('with_file', [False, True])
def test_github_step_outputs_append_without_losing_prior_step_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], with_file: bool,
) -> None:
    """Publish the same key/value records to stdout and GitHub's append-only output file.

    Preserving earlier records prevents a later release decision from erasing
    the version or source SHA that another workflow step already published.
    """
    destination = tmp_path / 'github-output'
    destination.write_text('previous=value\n', encoding='utf-8')
    if with_file:
        monkeypatch.setenv('GITHUB_OUTPUT', str(destination))
    else:
        monkeypatch.delenv('GITHUB_OUTPUT', raising=False)
    release.output({'version': VERSION, 'publish': 'false'})
    lines = 'version=' + VERSION + '\npublish=false\n'
    assert capsys.readouterr().out == lines
    assert destination.read_text(encoding='utf-8') == 'previous=value\n' + (lines if with_file else '')


@pytest.mark.parametrize('command', ['plan', 'prepare', 'checkout', 'assemble', 'verify', 'registry-status', 'stage', 'check-crate'])
@pytest.mark.parametrize('input_mode', ['flags', 'environment', 'fallback'])
@pytest.mark.parametrize('cargo_output', [None, 'relative-output', 'absolute-output'])
def test_release_cli_routes_explicit_inputs_and_checks_the_repacked_crate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, input_mode: str, cargo_output: str | None,
) -> None:
    """Exercise argument parsing and dispatch with all external actions replaced.

    The crate gate uses actual bytes and an independent SHA-256 oracle, so a
    wrong artifact name or omitted integrity check cannot authorize publishing
    a crate different from the one assembled for release.
    Workflow environment defaults and explicit overrides must select the same
    repository/run as the caller; otherwise resume can download another run.
    The frozen crate check must read Cargo's selected output directory.
    """
    root = tmp_path
    if cargo_output is None:
        monkeypatch.delenv('CARGO_TARGET_DIR', raising=False)
    else:
        monkeypatch.setenv('CARGO_TARGET_DIR', str(root / cargo_output) if cargo_output == 'absolute-output' else cargo_output)
    paths = {name: root / name for name in ('artifacts', 'sources', 'output', 'directory')}
    calls: list[tuple[str, tuple[Any, ...]]] = []
    for name in ('plan', 'prepare', 'checkout', 'assemble', 'verify', 'registry_status', 'stage', 'output'):
        def record(*args: Any, name: str = name) -> dict[str, str]:
            calls.append((name, args))
            return {'result': name}
        monkeypatch.setattr(release, name, record)
    crate = root / (cargo_output or 'target') / 'package' / ('benchset-' + VERSION + '.crate')
    crate.parent.mkdir(parents=True)
    payload = b'assembled crate bytes'
    crate.write_bytes(payload)
    monkeypatch.setattr(release, 'read_metadata', lambda directory: {
        'version': VERSION, 'files': {crate.name: hashlib.sha256(payload).hexdigest()},
    } if directory == paths['directory'] else pytest.fail('wrong manifest directory'))
    run_id = '0' if input_mode == 'fallback' else '123'
    arguments = ['release', command, '--publish', '--version', VERSION, '--source-sha', 'source-commit']
    if input_mode == 'fallback':
        monkeypatch.delenv('GITHUB_REPOSITORY', raising=False)
        monkeypatch.delenv('GITHUB_RUN_ID', raising=False)
    else:
        monkeypatch.setenv('GITHUB_REPOSITORY', REPOSITORY if input_mode == 'environment' else 'ignored/environment')
        monkeypatch.setenv('GITHUB_RUN_ID', '123' if input_mode == 'environment' else '456')
        if input_mode == 'flags':
            arguments.extend(['--repository', REPOSITORY, '--run-id', '123'])
    for name, path in paths.items():
        arguments.extend(['--' + name, str(path)])
    monkeypatch.setattr(release.sys, 'argv', arguments)
    monkeypatch.chdir(root)
    release.main()
    expected = {
        'plan': [('plan', (root, REPOSITORY, run_id, True)), ('output', ({'result': 'plan'},))],
        'prepare': [('prepare', (root, paths['artifacts'], paths['output'], REPOSITORY, run_id))],
        'checkout': [('checkout', (root, paths['directory'], VERSION, REPOSITORY, 'source-commit'))],
        'assemble': [('assemble', (paths['artifacts'], paths['sources'], paths['output']))],
        'verify': [('verify', (paths['directory'], VERSION, REPOSITORY, 'source-commit'))],
        'registry-status': [('registry_status', (paths['directory'],)), ('output', ({'result': 'registry_status'},))],
        'stage': [('stage', (root, paths['directory']))],
        'check-crate': [],
    }
    assert calls == expected[command]
    if command == 'check-crate':
        for changed in (b'', b'different crate bytes'):
            crate.write_bytes(changed)
            with pytest.raises(ValueError, match='different crate'):
                release.main()


def test_release_entrypoint_reports_a_failed_local_command_without_a_traceback(tmp_path: Path) -> None:
    """Turn an unavailable local manifest into a failed CLI command with a useful diagnostic.

    This exercises the real standalone entry point, including its error handler,
    while using only a missing temporary path and making no registry requests.
    """
    script = Path(__file__).resolve().parents[1] / 'scripts/release.py'
    result = subprocess.run([release.sys.executable, str(script), 'verify', '--directory', str(tmp_path / 'missing')],
                            cwd=tmp_path, capture_output=True, text=True, check=False, timeout=10)
    assert result.returncode == 1
    assert not result.stdout
    assert result.stderr.startswith('Release failed: ')
    assert 'Traceback' not in result.stderr
    assert 'missing' in result.stderr


@pytest.mark.parametrize('second_payload', [b'first build', b'another build'])
def test_release_collection_rejects_ambiguous_artifact_names(tmp_path: Path, second_payload: bytes) -> None:
    """Reject duplicate basenames across CI jobs instead of choosing an arbitrary build.

    Identical bytes are still ambiguous inputs; differing bytes must never
    overwrite the first artifact before its manifest and checksums are frozen.
    """
    source, destination = tmp_path / 'inputs', tmp_path / 'collected'
    for job, payload in [('first', b'first build'), ('second', second_payload)]:
        directory = source / job
        directory.mkdir(parents=True)
        (directory / 'benchset.whl').write_bytes(payload)
    with pytest.raises(ValueError, match=r'Duplicate release artifact: benchset\.whl'):
        release.collect(source, destination, ('.whl',))
    assert (destination / 'benchset.whl').read_bytes() == b'first build'


@pytest.mark.parametrize('dependency', ['pytest', 'mypy', 'build', 'filelock'])
def test_release_checks_install_executable_test_dependencies(dependency: str) -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / '.github/workflows/release.yml').read_text(encoding='utf-8')
    checks = re.search(r'^  checks:\n(.*?)(?=^  \w+:)', workflow, re.MULTILINE | re.DOTALL)
    assert checks is not None
    installation = re.search(r'^\s+- run: (python -m pip install .+)$', checks.group(1), re.MULTILINE)
    assert installation is not None
    command = shlex.split(installation.group(1))
    requirements = (root / 'requirements_dev.txt').read_text(encoding='utf-8').splitlines()
    pin = next(requirement for requirement in requirements if requirement.startswith(dependency + '=='))
    assert pin in command[4:], f'release checks must install {pin} before running all tests'


@pytest.mark.parametrize('outcome', ['completed', 'no_work', 'unexpected_failure', 'wrong_reference', 'wrong_run'])
def test_release_wheel_gate_checks_result_references_without_requiring_cpu_time(
    monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    import benchset  # noqa: PLC0415

    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/release.yml').read_text()
    match = re.search(r'CIBW_TEST_COMMAND: >-\n((?:            .+\n)+)', workflow)
    assert match is not None
    command = shlex.split(' '.join(line.strip() for line in match.group(1).splitlines()))
    assert command[:2] == ['python', '-c']
    assert len(command) == 3
    scores = {entry.benchmark: entry.score for entry in benchset.PI_REFERENCE_RUN.results}

    def run(name: str, **_options: float) -> benchset.BenchmarkResult:
        if outcome in ('completed', 'wrong_reference', 'wrong_run'):
            result = benchset.BenchmarkResult(name, status='completed', score=scores[name], pi_equivalent=1.0)
            if outcome == 'wrong_reference':
                result = replace(result, reference_machine=replace(result.reference_machine))
            elif outcome == 'wrong_run':
                result = replace(result, reference_run=replace(result.reference_run))
            return result
        return benchset.BenchmarkResult(name, reason=(
            'measurement window was too short to complete any work' if outcome == 'no_work' else 'worker failed'
        ))

    def run_all(**options: float) -> List[benchset.BenchmarkResult]:
        first = run('single_core', **options)
        return [first, run('multi_core', **options)] if first.success else [first]

    monkeypatch.setattr(benchset, 'run', run)
    monkeypatch.setattr(benchset, 'run_all', run_all)
    if outcome in ('unexpected_failure', 'wrong_reference', 'wrong_run'):
        with pytest.raises(AssertionError):
            exec(command[2], {})
    else:
        exec(command[2], {})


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Temporary repositories must not inherit signing or hooks from the host.
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(tmp_path / 'empty-gitconfig'))
    monkeypatch.delenv('GIT_CONFIG_COUNT', raising=False)
    monkeypatch.delenv('GIT_CONFIG_PARAMETERS', raising=False)
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'Cargo.toml').write_text('[package]\nname = "benchset"\nversion = "0.0.1"\n')
    (root / 'pyproject.toml').write_text('[project]\nversion = "0.0.1"\n')
    (root / '.gitignore').write_text('target/\n')
    manifest = Path(__file__).resolve().parents[1] / 'Package.swift'
    (root / 'Package.swift').write_text(manifest.read_text())
    release.git(root, 'init', '--initial-branch=main')
    release.git(root, 'config', 'user.name', 'Test')
    release.git(root, 'config', 'user.email', 'test@example.com')
    release.git(root, 'add', '.')
    release.git(root, 'commit', '-m', 'Source')
    return root


def artifacts(root: Path) -> Path:
    """Model the independent CI matrix rather than deriving expected targets from the validator."""
    directory = root / 'target/incoming'
    directory.mkdir(parents=True)
    for target in ('aarch64-apple-darwin', 'x86_64-apple-darwin',
                   'aarch64-unknown-linux-gnu', 'x86_64-unknown-linux-gnu', 'x86_64-pc-windows-msvc'):
        (directory / ('benchset-' + target + '.zip')).write_bytes(target.encode())
    for name in ('Benchset-Swift.zip', 'Benchset.xcframework.zip'):
        (directory / name).write_bytes(name.encode())
    for platform in ('macosx_11_0_arm64', 'macosx_10_12_x86_64',
                     'manylinux2014_x86_64', 'manylinux2014_aarch64', 'win_amd64'):
        (directory / ('benchset-0.0.1-py3-none-' + platform + '.whl')).write_bytes(platform.encode())
    return directory


def prepared(root: Path) -> Path:
    destination = root / 'target/prepared'
    release.prepare(root, artifacts(root), destination, REPOSITORY, '123')
    return destination


def assembled(root: Path) -> Path:
    snapshot = prepared(root)
    sources = root / 'target/sources'
    sources.mkdir()
    (sources / 'benchset-0.0.1.tar.gz').write_bytes(b'Python source')
    (sources / 'benchset-0.0.1.crate').write_bytes(b'Rust source')
    destination = root / 'target/release'
    release.assemble(snapshot, sources, destination)
    return destination


def test_release_snapshot_preserves_main_and_can_be_checked_out(source: Path) -> None:
    """Freeze an unchanged source parent and a usable remote Swift manifest.

    When Swift is installed, its real manifest parser checks syntax and target
    metadata without resolving or downloading the remote XCFramework.
    """
    source_sha = release.git(source, 'rev-parse', 'HEAD')
    snapshot = prepared(source)
    metadata = release.read_metadata(snapshot)
    assert release.git(source, 'rev-parse', 'main') == source_sha
    assert metadata['source_sha'] == source_sha
    assert release.git(source, 'diff', '--name-only', source_sha, metadata['release_sha']) == 'Package.swift'
    manifest = (source / 'Package.swift').read_text()
    assert '/mutating/benchset/releases/download/v0.0.1/Benchset.xcframework.zip' in manifest
    assert release.digest(snapshot / 'Benchset.xcframework.zip') in manifest
    assert 'path: "Artifacts/' not in manifest
    swift = shutil.which('swift')
    if swift:
        parsed = subprocess.run([swift, 'package', '--package-path', str(source), 'dump-package'],
                                capture_output=True, text=True, check=False, timeout=60)
        assert parsed.returncode == 0, parsed.stderr
        targets = {target['name']: target for target in json.loads(parsed.stdout)['targets']}
        assert targets['CBenchset']['url'] == (
            'https://github.com/mutating/benchset/releases/download/v0.0.1/Benchset.xcframework.zip'
        )
        assert targets['CBenchset']['checksum'] == release.digest(snapshot / 'Benchset.xcframework.zip')
    release.git(source, 'checkout', '--detach', source_sha)
    release.checkout(source, snapshot, VERSION, REPOSITORY, source_sha)
    assert release.git(source, 'rev-parse', 'HEAD') == metadata['release_sha']


def test_release_planning_uses_only_canonical_version_and_preparation_requires_clean_source(source: Path) -> None:
    (source / 'Cargo.toml').write_text('[package]\nversion = "0.0.2"\n')
    assert release.version(source) == VERSION
    with pytest.raises(ValueError, match='clean Git checkout'):
        release.prepare(source, artifacts(source), source / 'target/prepared', REPOSITORY, '123')


def test_incomplete_platform_matrix_is_rejected(source: Path) -> None:
    """Reject incomplete native inputs both before and after preparing a snapshot.

    A self-consistent edited manifest must not authorize a release with a lost
    platform: checksums/inventory equality alone cannot enforce matrix coverage.
    """
    incoming = artifacts(source)
    snapshot = source / 'target/prepared'
    release.prepare(source, incoming, snapshot, REPOSITORY, '123')
    metadata = release.read_metadata(snapshot)
    wheel = 'benchset-0.0.1-py3-none-win_amd64.whl'
    (snapshot / wheel).unlink()
    del metadata['files'][wheel]
    release.save(snapshot / release.PREPARED, metadata)
    with pytest.raises(ValueError, match='five version-matched'):
        release.verify(snapshot, VERSION, REPOSITORY, metadata['source_sha'])
    (incoming / 'benchset-0.0.1-py3-none-win_amd64.whl').unlink()
    with pytest.raises(ValueError, match='five version-matched'):
        release.prepare(source, incoming, source / 'target/reprepared', REPOSITORY, '123')


def test_planning_does_not_require_a_manually_synchronized_lockfile(source: Path) -> None:
    (source / 'Cargo.lock').write_text('[[package]]\nname = "benchset"\nversion = "0.0.0"\n')
    before = (source / 'Cargo.lock').read_bytes()
    assert release.version(source) == VERSION
    assert (source / 'Cargo.lock').read_bytes() == before


@pytest.fixture
def prepared_bump(source: Path) -> Path:
    (source / 'Cargo.lock').write_text('version = 3\n[[package]]\nname = "benchset"\nversion = "0.0.1"\n')
    (source / 'README.md').write_text('benchset = "0.0.1"\n.package(url: "example", .exact("0.0.1"))\n')
    release.git(source, 'add', 'Cargo.lock', 'README.md')
    release.git(source, 'commit', '-m', 'Add generated metadata')
    (source / 'pyproject.toml').write_text('[project]\nversion = "0.0.2"\n')
    release.git(source, 'add', 'pyproject.toml')
    release.git(source, 'commit', '-m', 'Change only canonical project version')
    incoming = artifacts(source)
    for wheel in incoming.glob('*.whl'):
        wheel.rename(wheel.with_name(wheel.name.replace('0.0.1', '0.0.2')))
    destination = source / 'target/prepared'
    release.prepare(source, incoming, destination, REPOSITORY, '123')
    return destination


def test_release_snapshot_generates_versions_from_a_pyproject_only_commit(source: Path, prepared_bump: Path) -> None:
    metadata = release.read_metadata(prepared_bump)
    source_sha = metadata['source_sha']
    assert metadata['version'] == '0.0.2'
    assert release.git(source, 'rev-parse', 'main') == source_sha
    assert set(release.git(source, 'diff', '--name-only', source_sha, metadata['release_sha']).splitlines()) == {
        'Cargo.toml', 'Cargo.lock', 'README.md', 'Package.swift',
    }
    assert 'version = "0.0.1"' in release.git(source, 'show', source_sha + ':Cargo.toml')
    release.git(source, 'checkout', '--detach', source_sha)
    release.checkout(source, prepared_bump, '0.0.2', REPOSITORY, source_sha)
    assert 'version = "0.0.2"' in (source / 'Cargo.toml').read_text()
    assert 'version = "0.0.2"' in (source / 'Cargo.lock').read_text()
    assert 'benchset = "0.0.2"' in (source / 'README.md').read_text()
    assert not release.git(source, 'status', '--porcelain', '--untracked-files=no')


@pytest.mark.parametrize(('filename', 'mode_only'), [
    ('Cargo.toml', False), ('Cargo.lock', False), ('README.md', False), ('unexpected.txt', False),
    ('Cargo.toml', True),
])
def test_release_snapshot_rejects_changes_beyond_exact_generated_values(
    source: Path, prepared_bump: Path, filename: str, mode_only: bool,
) -> None:
    metadata = release.read_metadata(prepared_bump)
    if mode_only:
        release.git(source, 'update-index', '--chmod=+x', '--', filename)
    else:
        path = source / filename
        path.write_text((path.read_text() if path.exists() else '') + '\n# unrelated change\n')
        release.git(source, 'add', filename)
    release.git(source, 'commit', '--amend', '--no-edit')
    metadata['release_sha'] = release.git(source, 'rev-parse', 'HEAD')
    release.git(source, 'update-ref', release.BUNDLE_REF, metadata['release_sha'])
    bundle = prepared_bump / 'source.bundle'
    bundle.unlink()
    release.git(source, 'bundle', 'create', str(bundle), release.BUNDLE_REF, '^' + metadata['source_sha'])
    metadata['source_bundle_sha256'] = release.digest(bundle)
    release.save(prepared_bump / release.PREPARED, metadata)
    with pytest.raises(ValueError, match='unexpected source changes'):
        release.checkout(source, prepared_bump, '0.0.2', REPOSITORY, metadata['source_sha'])


def test_checksum_manifest_tampering_is_rejected(source: Path) -> None:
    directory = assembled(source)
    metadata = release.read_metadata(directory)
    (directory / 'SHA256SUMS').write_text('wrong inventory\n')
    with pytest.raises(ValueError, match='checksum manifest mismatch'):
        release.verify(directory, VERSION, REPOSITORY, metadata['source_sha'])


@pytest.mark.parametrize(('missing', 'replacement'), [
    ('win_amd64', 'macosx_12_0_arm64'),
    ('macosx_11_0_arm64', 'macosx_11_0_notarm64'),
    ('macosx_10_12_x86_64', 'macosx_10_12_notx86_64'),
    ('manylinux2014_x86_64', 'manylinux2014_ppc64le'),
    ('manylinux2014_x86_64', 'manylinux2014_s390x'),
    ('manylinux2014_x86_64', 'manylinux2014_armv7l'),
    ('manylinux2014_x86_64', 'manylinux2014_notx86_64'),
    ('manylinux2014_aarch64', 'manylinux2014_notaarch64'),
])
def test_invalid_platform_cannot_replace_a_missing_one(
    source: Path, missing: str, replacement: str,
) -> None:
    """Require every supported platform, rejecting duplicates and CPU lookalikes.

    Matching an architecture suffix without its separator can misclassify an
    unsupported CPU as a required wheel. Unknown Linux CPUs must not fall back
    to x86_64 and approve a release missing that platform.
    """
    incoming = artifacts(source)
    (incoming / ('benchset-0.0.1-py3-none-' + missing + '.whl')).unlink()
    (incoming / ('benchset-0.0.1-py3-none-' + replacement + '.whl')).write_bytes(b'wrong platform')
    with pytest.raises(ValueError, match='platform matrix is incomplete'):
        release.prepare(source, incoming, source / 'target/prepared', REPOSITORY, '123')


def test_tampering_and_wrong_release_are_rejected(source: Path) -> None:
    """Reject independent identity and digest mismatches in either ordering direction.

    Hashes and release identifiers require equality, never lexical ordering.
    Check each diagnostic separately so a later manifest error cannot conceal
    an omitted early identity, bundle, or XCFramework integrity check.
    """
    directory = assembled(source)
    metadata = release.read_metadata(directory)
    for expected_version, repository, source_sha in [
        ('0.0.0', REPOSITORY, metadata['source_sha']), ('0.0.2', REPOSITORY, metadata['source_sha']),
        (VERSION, 'aaa/repo', metadata['source_sha']), (VERSION, 'zzz/repo', metadata['source_sha']),
        (VERSION, REPOSITORY, '0' * 40), (VERSION, REPOSITORY, 'f' * 40),
    ]:
        with pytest.raises(ValueError, match='do not match'):
            release.verify(directory, expected_version, repository, source_sha)
    for key, bad_values, diagnostic in [
        ('tag', ['u0.0.1', 'w0.0.1'], 'do not match'),
        ('source_bundle_sha256', ['0' * 64, 'f' * 64], 'source bundle checksum mismatch'),
        ('xcframework_sha256', ['0' * 64, 'f' * 64], 'XCFramework checksum mismatch'),
    ]:
        for value in bad_values:
            release.save(directory / release.MANIFEST, {**metadata, key: value})
            with pytest.raises(ValueError, match=diagnostic):
                release.verify(directory, VERSION, REPOSITORY, metadata['source_sha'])
    for checksum in ('0' * 64, 'f' * 64):
        changed = {**metadata, 'files': {**metadata['files'], 'Benchset.xcframework.zip': checksum}}
        release.save(directory / release.MANIFEST, changed)
        with pytest.raises(ValueError, match='artifact checksum mismatch'):
            release.verify(directory, VERSION, REPOSITORY, metadata['source_sha'])
    release.save(directory / release.MANIFEST, metadata)
    (directory / 'Benchset.xcframework.zip').write_bytes(b'changed after freezing')
    with pytest.raises(ValueError, match='checksum mismatch'):
        release.verify(directory, VERSION, REPOSITORY, metadata['source_sha'])
    with pytest.raises(ValueError, match='checksum mismatch'):
        release.assemble(directory, source / 'target/sources', source / 'target/reassembled')
    assert not (source / 'target/reassembled').exists()


@pytest.mark.parametrize('path_kind', ['parent', 'absolute'])
def test_release_manifest_rejects_foreign_paths_before_reading_their_contents(
    source: Path, monkeypatch: pytest.MonkeyPatch, path_kind: str,
) -> None:
    """Reject a non-flat artifact name before hashing anything outside its snapshot.

    A later inventory error cannot undo an unintended file read. The digest
    probe fails on any such access rather than providing a real foreign file.
    """
    snapshot = prepared(source)
    metadata = release.read_metadata(snapshot)
    name = '../outside.whl' if path_kind == 'parent' else str(source.parent / 'outside.whl')
    metadata['files'] = {name: '0' * 64}
    release.save(snapshot / release.PREPARED, metadata)

    def forbidden(path: Path) -> str:
        pytest.fail('verification must reject a foreign artifact before reading ' + str(path))

    monkeypatch.setattr(release, 'digest', forbidden)
    with pytest.raises(ValueError, match='artifact checksum mismatch: ' + re.escape(name)):
        release.verify(snapshot, VERSION, REPOSITORY, metadata['source_sha'])


def test_plan_resumes_original_artifact_run_and_skips_completed_version(
    source: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan a first publication, then resume its immutable original run or skip completion.

    Fresh and resumed plans must both preserve the version/tag/source identity;
    checking only the resume branch can conceal a broken first-release result.
    """
    original_source = release.git(source, 'rev-parse', 'HEAD')
    monkeypatch.setattr(release, 'github_release', lambda *_args: pytest.fail('fresh tag must not need registry lookup'))
    assert release.plan(source, REPOSITORY, '999', True) == {
        'version': VERSION, 'tag': 'v0.0.1', 'source_sha': original_source,
        'artifact_run_id': '999', 'build': 'true', 'publish': 'true',
    }
    directory = assembled(source)
    metadata = release.read_metadata(directory)
    release.git(source, 'tag', 'v0.0.1', metadata['release_sha'])
    release.git(source, 'checkout', 'main')
    monkeypatch.setattr(release, 'github_release', lambda _repository, _tag: None)
    resumed = release.plan(source, REPOSITORY, '999', True)
    assert resumed['build'] == 'false'
    assert resumed['publish'] == 'true'
    assert resumed['artifact_run_id'] == '123'
    assert resumed['source_sha'] == metadata['source_sha']
    monkeypatch.setattr(release, 'github_release', lambda _repository, _tag: {'draft': False})
    completed = release.plan(source, REPOSITORY, '999', True)
    assert completed['build'] == 'false'
    assert completed['publish'] == 'false'
    dry_run = release.plan(source, REPOSITORY, '999', False)
    assert dry_run['build'] == 'true'
    assert dry_run['publish'] == 'false'


@pytest.mark.parametrize('missing', ['both', 'source', 'run'])
def test_unmanaged_tag_is_never_replaced(
    source: Path, monkeypatch: pytest.MonkeyPatch, missing: str,
) -> None:
    """Reject incomplete release provenance locally without replacing the tag.

    One valid trailer must not hide the other missing trailer, trigger a
    registry request, or turn a useful validation error into AttributeError.
    A real child commit keeps its source-parent relationship valid so that
    validation of the missing run ID cannot be masked by an unrelated error.
    """
    parent = release.git(source, 'rev-parse', 'HEAD')
    trailers = []
    if missing not in ('both', 'source'):
        trailers.append('Benchset-Source: ' + parent)
    if missing not in ('both', 'run'):
        trailers.append('Benchset-Run: 123')
    release.git(source, 'commit', '--allow-empty', '-m', 'Foreign release\n\n' + '\n'.join(trailers))
    release.git(source, 'tag', 'v0.0.1')
    tagged = release.git(source, 'rev-parse', 'v0.0.1')
    monkeypatch.setattr(release, 'github_release',
                        lambda *_args: pytest.fail('incomplete provenance must fail before registry access'))
    with pytest.raises(ValueError, match='not a managed'):
        release.plan(source, REPOSITORY, '999', True)
    assert release.git(source, 'rev-parse', 'v0.0.1') == tagged
    assert release.git(source, 'rev-parse', 'HEAD') == tagged


def test_partial_registry_uploads_are_checked_by_content(
    source: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Request the version-specific registries and compare uploaded bytes before resuming."""
    directory = assembled(source)
    metadata = release.read_metadata(directory)
    python_files = {name: checksum for name, checksum in metadata['files'].items()
                    if name.endswith(('.whl', '.tar.gz'))}
    first = next(iter(python_files))
    partial = {'urls': [{'filename': first, 'digests': {'sha256': python_files[first]}}]}
    crate = {'version': {'checksum': metadata['files']['benchset-0.0.1.crate']}}
    pypi_url = 'https://pypi.org/pypi/benchset/' + VERSION + '/json'
    crate_url = 'https://crates.io/api/v1/crates/benchset/' + VERSION
    documents = {pypi_url: partial, crate_url: crate}

    def lookup(url: str) -> Any:
        assert url in documents
        return documents[url]

    monkeypatch.setattr(release, 'get_json', lookup)
    assert release.registry_status(directory) == {'pypi': 'true', 'crate': 'false'}
    complete = {'urls': [{'filename': name, 'digests': {'sha256': checksum}}
                         for name, checksum in python_files.items()]}
    documents[pypi_url] = complete
    assert release.registry_status(directory) == {'pypi': 'false', 'crate': 'false'}
    partial['urls'][0]['digests']['sha256'] = 'different bytes'
    documents[pypi_url] = partial
    with pytest.raises(ValueError, match='PyPI already contains a different'):
        release.registry_status(directory)
    documents.update({pypi_url: None, crate_url: {'version': {'checksum': 'different'}}})
    with pytest.raises(ValueError, match='different crate'):
        release.registry_status(directory)


def test_first_registry_publication_is_requested(
    source: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = assembled(source)
    monkeypatch.setattr(release, 'get_json', lambda _url: None)
    assert release.registry_status(directory) == {'pypi': 'true', 'crate': 'true'}


def test_stage_pushes_only_the_prepared_tag_and_preserves_assets_on_resume(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publish the verified snapshot to its requested repository, preserving immutable tags.

    Uploads include their manifest/checksums, and a corrupt snapshot or conflicting
    local tag must be rejected before any GitHub command can modify a release.
    Creation must verify the existing tag and supply a valid title/notes command;
    a permissive subprocess fake must not conceal broken GitHub CLI arguments.
    """
    directory = assembled(source)
    origin = tmp_path / 'origin.git'
    subprocess.run(['git', 'init', '--bare', str(origin)], check=True, capture_output=True)
    release.git(source, 'remote', 'add', 'origin', str(origin))
    calls: List[List[str]] = []
    original = subprocess.run

    def run(arguments: List[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if arguments[0] == 'gh':
            assert kwargs == {'check': True}
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 0)
        return original(arguments, **kwargs)

    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(release, 'github_release', lambda _repository, _tag: None)
    release.stage(source, directory)
    metadata = release.read_metadata(directory)
    assert release.git(origin, 'rev-parse', 'refs/tags/v0.0.1') == metadata['release_sha']
    assert release.git(origin, 'for-each-ref', '--format=%(refname)') == 'refs/tags/v0.0.1'
    assert calls[0][:4] == ['gh', 'release', 'create', 'v0.0.1']
    assert '--draft' in calls[0]
    assert '--verify-tag' in calls[0]
    assert '--generate-notes' in calls[0]
    assert calls[0][calls[0].index('--title') + 1] == 'v0.0.1'
    assert all(not argument.endswith('source.bundle') for argument in calls[1])
    assert '--clobber' in calls[1]
    for arguments in calls:
        assert arguments[arguments.index('--repo') + 1] == REPOSITORY
    uploaded = {Path(name).name for name in calls[1] if not name.startswith('--')}
    assert set(metadata['files']) | {release.MANIFEST, 'SHA256SUMS'} <= uploaded
    calls.clear()
    monkeypatch.setattr(release, 'github_release', lambda _repository, _tag: {'draft': True})
    release.stage(source, directory)
    assert len(calls) == 1
    assert calls[0][:3] == ['gh', 'release', 'upload']
    monkeypatch.setattr(release, 'github_release', lambda _repository, _tag: {'draft': False})
    with pytest.raises(ValueError, match='published GitHub Release'):
        release.stage(source, directory)
    calls.clear()
    release.git(source, 'tag', 'v0.0.1', metadata['source_sha'])
    with pytest.raises(ValueError, match='replace an existing release tag'):
        release.stage(source, directory)
    assert not calls
    (directory / 'Benchset.xcframework.zip').write_bytes(b'corrupt frozen artifact')
    with pytest.raises(ValueError, match='checksum mismatch'):
        release.stage(source, directory)
    assert not calls


@pytest.mark.parametrize('count', [0, 2])
def test_manifest_requires_exactly_one_binary_target(count: int) -> None:
    """Require one unambiguous binary target before rewriting a release manifest."""
    manifest = 'import PackageDescription\n' + '.binaryTarget(name: "CBenchset", path: "Artifacts/A.xcframework")\n' * count
    with pytest.raises(ValueError, match='exactly one'):
        release.swift_manifest(manifest, REPOSITORY, VERSION, '0' * 64)


def test_assembled_manifest_contains_all_public_files(source: Path) -> None:
    directory = assembled(source)
    metadata: Dict[str, Any] = json.loads((directory / release.MANIFEST).read_text())
    assert len(metadata['files']) == 14
    assert 'source.bundle' not in metadata['files']
    assert 'benchset-0.0.1.crate' in metadata['files']
    assert 'benchset-0.0.1.tar.gz' in metadata['files']
    assert release.MANIFEST in (directory / 'SHA256SUMS').read_text()
