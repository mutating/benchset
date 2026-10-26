from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List

import pytest

from scripts import release

REPOSITORY = 'mutating/benchset'
VERSION = '0.0.1'


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
    directory = root / 'target/incoming'
    directory.mkdir(parents=True)
    for target in release.NATIVE_TARGETS:
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
    release.git(source, 'checkout', '--detach', source_sha)
    release.checkout(source, snapshot, VERSION, REPOSITORY, source_sha)
    assert release.git(source, 'rev-parse', 'HEAD') == metadata['release_sha']


def test_freeze_requires_consistent_versions_and_clean_source(source: Path) -> None:
    (source / 'Cargo.toml').write_text('[package]\nversion = "0.0.2"\n')
    with pytest.raises(ValueError, match='versions must match'):
        release.version(source)
    with pytest.raises(ValueError, match='clean Git checkout'):
        release.prepare(source, artifacts(source), source / 'target/prepared', REPOSITORY, '123')


def test_incomplete_platform_matrix_is_rejected(source: Path) -> None:
    incoming = artifacts(source)
    (incoming / 'benchset-0.0.1-py3-none-win_amd64.whl').unlink()
    with pytest.raises(ValueError, match='five version-matched'):
        release.prepare(source, incoming, source / 'target/prepared', REPOSITORY, '123')


def test_stale_lockfile_is_rejected(source: Path) -> None:
    (source / 'Cargo.lock').write_text('[[package]]\nname = "benchset"\nversion = "0.0.0"\n')
    with pytest.raises(ValueError, match=r'python scripts/version\.py'):
        release.version(source)


def test_checksum_manifest_tampering_is_rejected(source: Path) -> None:
    directory = assembled(source)
    metadata = release.read_metadata(directory)
    (directory / 'SHA256SUMS').write_text('wrong inventory\n')
    with pytest.raises(ValueError, match='checksum manifest mismatch'):
        release.verify(directory, VERSION, REPOSITORY, metadata['source_sha'])


def test_duplicate_platform_cannot_replace_a_missing_one(source: Path) -> None:
    incoming = artifacts(source)
    (incoming / 'benchset-0.0.1-py3-none-win_amd64.whl').unlink()
    (incoming / 'benchset-0.0.1-py3-none-macosx_12_0_arm64.whl').write_bytes(b'duplicate')
    with pytest.raises(ValueError, match='platform matrix is incomplete'):
        release.prepare(source, incoming, source / 'target/prepared', REPOSITORY, '123')


def test_tampering_and_wrong_release_are_rejected(source: Path) -> None:
    directory = assembled(source)
    metadata = release.read_metadata(directory)
    with pytest.raises(ValueError, match='do not match'):
        release.verify(directory, '0.0.2', REPOSITORY, metadata['source_sha'])
    with pytest.raises(ValueError, match='do not match'):
        release.verify(directory, VERSION, 'other/repo', metadata['source_sha'])
    (directory / 'Benchset.xcframework.zip').write_bytes(b'changed after freezing')
    with pytest.raises(ValueError, match='checksum mismatch'):
        release.verify(directory, VERSION, REPOSITORY, metadata['source_sha'])


def test_plan_resumes_original_artifact_run_and_skips_completed_version(
    source: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_unmanaged_tag_is_never_replaced(source: Path) -> None:
    release.git(source, 'tag', 'v0.0.1')
    with pytest.raises(ValueError, match='not a managed'):
        release.plan(source, REPOSITORY, '999', True)


def test_partial_registry_uploads_are_checked_by_content(
    source: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = assembled(source)
    metadata = release.read_metadata(directory)
    python_files = {name: checksum for name, checksum in metadata['files'].items()
                    if name.endswith(('.whl', '.tar.gz'))}
    first = next(iter(python_files))
    partial = {'urls': [{'filename': first, 'digests': {'sha256': python_files[first]}}]}
    crate = {'version': {'checksum': metadata['files']['benchset-0.0.1.crate']}}
    monkeypatch.setattr(release, 'get_json', lambda url: partial if 'pypi.org' in url else crate)
    assert release.registry_status(directory) == {'pypi': 'true', 'crate': 'false'}
    complete = {'urls': [{'filename': name, 'digests': {'sha256': checksum}}
                         for name, checksum in python_files.items()]}
    monkeypatch.setattr(release, 'get_json', lambda url: complete if 'pypi.org' in url else crate)
    assert release.registry_status(directory) == {'pypi': 'false', 'crate': 'false'}
    partial['urls'][0]['digests']['sha256'] = 'different bytes'
    monkeypatch.setattr(release, 'get_json', lambda url: partial if 'pypi.org' in url else crate)
    with pytest.raises(ValueError, match='PyPI already contains a different'):
        release.registry_status(directory)
    monkeypatch.setattr(release, 'get_json', lambda url: None if 'pypi.org' in url else {'version': {'checksum': 'different'}})
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
    directory = assembled(source)
    origin = tmp_path / 'origin.git'
    subprocess.run(['git', 'init', '--bare', str(origin)], check=True, capture_output=True)
    release.git(source, 'remote', 'add', 'origin', str(origin))
    calls: List[List[str]] = []
    original = subprocess.run

    def run(arguments: List[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if arguments[0] == 'gh':
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
    assert all(not argument.endswith('source.bundle') for argument in calls[1])
    assert '--clobber' in calls[1]
    calls.clear()
    monkeypatch.setattr(release, 'github_release', lambda _repository, _tag: {'draft': True})
    release.stage(source, directory)
    assert len(calls) == 1
    assert calls[0][:3] == ['gh', 'release', 'upload']
    monkeypatch.setattr(release, 'github_release', lambda _repository, _tag: {'draft': False})
    with pytest.raises(ValueError, match='published GitHub Release'):
        release.stage(source, directory)


def test_manifest_rejects_missing_binary_target() -> None:
    with pytest.raises(ValueError, match='exactly one'):
        release.swift_manifest('import PackageDescription\n', REPOSITORY, VERSION, '0' * 64)


def test_assembled_manifest_contains_all_public_files(source: Path) -> None:
    directory = assembled(source)
    metadata: Dict[str, Any] = json.loads((directory / release.MANIFEST).read_text())
    assert len(metadata['files']) == 14
    assert 'source.bundle' not in metadata['files']
    assert 'benchset-0.0.1.crate' in metadata['files']
    assert 'benchset-0.0.1.tar.gz' in metadata['files']
    assert release.MANIFEST in (directory / 'SHA256SUMS').read_text()
