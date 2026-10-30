"""Prepare an immutable release snapshot and resume publication without rebuilding.

Only the ``stage`` command writes to GitHub. Other commands work locally or
read registry metadata. CI checks out a clean, detached source commit before
preparing the snapshot; the release commit freezes generated versions and Package.swift.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.cargo import target_directory
from scripts.version import read_version, sync_version

NATIVE_TARGETS = (
    'aarch64-apple-darwin', 'x86_64-apple-darwin',
    'aarch64-unknown-linux-gnu', 'x86_64-unknown-linux-gnu',
    'x86_64-pc-windows-msvc',
)
BUNDLE_REF = 'refs/heads/benchset-prepared'
MANIFEST = 'release-manifest.json'
PREPARED = 'release-plan.json'
VERSION_FILES = ('Cargo.toml', 'Cargo.lock', 'README.md')


def git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(['git', *arguments], cwd=root, text=True).strip()


def version(root: Path) -> str:
    return read_version(root)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def repository_name(value: str) -> str:
    if re.fullmatch(r'[\w.-]+/[\w.-]+', value) is None:
        raise ValueError('Expected an owner/repository name')
    return value


def get_json(url: str, *, token: str = '') -> Any:
    headers = {'User-Agent': 'benchset-release', 'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise RuntimeError('Registry request failed with HTTP ' + str(error.code)) from None


def github_release(repository: str, tag: str) -> Any:
    return get_json(
        'https://api.github.com/repos/' + repository_name(repository) + '/releases/tags/' + tag,
        token=os.environ.get('GH_TOKEN', ''),
    )


def output(values: dict[str, str]) -> None:
    for key, value in values.items():
        print(key + '=' + value)
    filename = os.environ.get('GITHUB_OUTPUT')
    if filename:
        with Path(filename).open('a') as destination:
            for key, value in values.items():
                destination.write(key + '=' + value + '\n')


def plan(root: Path, repository: str, run_id: str, publish: bool) -> dict[str, str]:
    package_version = version(root)
    tag = 'v' + package_version
    values = {
        'version': package_version, 'tag': tag,
        'source_sha': git(root, 'rev-parse', 'HEAD'),
        'artifact_run_id': run_id, 'build': 'true',
        'publish': str(publish).lower(),
    }
    if not publish:
        return values
    existing = subprocess.run(
        ['git', 'rev-parse', '--verify', 'refs/tags/' + tag + '^{commit}'],
        cwd=root, capture_output=True, text=True, check=False,
    )
    if existing.returncode:
        return values
    release_sha = existing.stdout.strip()
    message = git(root, 'show', '-s', '--format=%B', release_sha)
    source = re.search(r'^Benchset-Source: ([a-f0-9]{40})$', message, re.M)
    original_run = re.search(r'^Benchset-Run: (\d+)$', message, re.M)
    if source is None or original_run is None:
        raise ValueError('Existing tag ' + tag + ' is not a managed benchset release')
    if git(root, 'rev-parse', release_sha + '^') != source.group(1):
        raise ValueError('Release tag has an unexpected source parent')
    release = github_release(repository, tag)
    values.update(build='false', source_sha=source.group(1), artifact_run_id=original_run.group(1))
    if release is not None and not release['draft']:
        values['publish'] = 'false'
    return values


def swift_manifest(source: str, repository: str, package_version: str, checksum: str) -> str:
    url = ('https://github.com/' + repository_name(repository) + '/releases/download/v'
           + package_version + '/Benchset.xcframework.zip')
    replacement = (
        '.binaryTarget(name: "CBenchset",\n'
        '                      url: "' + url + '",\n'
        '                      checksum: "' + checksum + '")'
    )
    result, count = re.subn(
        r'\.binaryTarget\(name:\s*"CBenchset",[^)]*\)', replacement, source,
    )
    if count != 1:
        raise ValueError('Expected exactly one CBenchset binary target')
    return result


def collect(source: Path, destination: Path, suffixes: tuple[str, ...]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob('*')):
        if path.is_file() and path.name.endswith(suffixes):
            target = destination / path.name
            if target.exists():
                raise ValueError('Duplicate release artifact: ' + path.name)
            shutil.copy2(path, target)


def validate_native_files(directory: Path, package_version: str) -> None:
    expected = {'benchset-' + target + '.zip' for target in NATIVE_TARGETS}
    expected.update(('Benchset-Swift.zip', 'Benchset.xcframework.zip'))
    if {path.name for path in directory.glob('*.zip')} != expected:
        raise ValueError('Expected all five native archives and both Apple packages')
    wheels = list(directory.glob('*.whl'))
    if len(wheels) != 5 or any(
        not path.name.startswith('benchset-' + package_version + '-py3-none-') for path in wheels
    ):
        raise ValueError('Expected five version-matched, interpreter-independent wheels')
    platforms = set()
    for path in wheels:
        platform = path.name[:-4].split('-')[-1]
        if platform.startswith('macosx_'):
            for architecture in ('arm64', 'x86_64'):
                if platform.endswith('_' + architecture):
                    platforms.add('macos-' + architecture)
        elif platform.startswith('manylinux'):
            for architecture in ('aarch64', 'x86_64'):
                if platform.endswith('_' + architecture):
                    platforms.add('linux-' + architecture)
        elif platform == 'win_amd64':
            platforms.add('windows-x86_64')
    if platforms != {'macos-arm64', 'macos-x86_64', 'linux-aarch64', 'linux-x86_64', 'windows-x86_64'}:
        raise ValueError('Wheel platform matrix is incomplete')


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def prepare(root: Path, artifacts: Path, destination: Path, repository: str, run_id: str) -> None:
    if not run_id.isdecimal():
        raise ValueError('Expected a numeric workflow run ID')
    if git(root, 'status', '--porcelain', '--untracked-files=no'):
        raise ValueError('Release preparation requires a clean Git checkout')
    package_version = version(root)
    source_sha = git(root, 'rev-parse', 'HEAD')
    collect(artifacts, destination, ('.whl', '.zip'))
    validate_native_files(destination, package_version)
    checksum = digest(destination / 'Benchset.xcframework.zip')
    git(root, 'checkout', '--detach', source_sha)
    sync_version(root)
    manifest = root / 'Package.swift'
    manifest.write_text(swift_manifest(manifest.read_text(), repository, package_version, checksum))
    release_files = ['Package.swift', *(name for name in VERSION_FILES if (root / name).exists())]
    git(root, 'add', *release_files)
    message = ('Prepare benchset ' + package_version + '\n\nBenchset-Source: ' + source_sha
               + '\nBenchset-Run: ' + run_id)
    git(root, '-c', 'user.name=github-actions[bot]', '-c',
        'user.email=41898282+github-actions[bot]@users.noreply.github.com',
        '-c', 'commit.gpgsign=false', 'commit', '-m', message, '--', *release_files)
    release_sha = git(root, 'rev-parse', 'HEAD')
    git(root, 'update-ref', BUNDLE_REF, release_sha)
    git(root, 'bundle', 'create', str((destination / 'source.bundle').resolve()), BUNDLE_REF, '^' + source_sha)
    save(destination / PREPARED, {
        'version': package_version, 'tag': 'v' + package_version,
        'repository': repository_name(repository), 'source_sha': source_sha,
        'release_sha': release_sha, 'artifact_run_id': run_id,
        'xcframework_sha256': checksum, 'source_bundle_sha256': digest(destination / 'source.bundle'),
        'files': {path.name: digest(path) for path in sorted(destination.iterdir()) if path.suffix in ('.whl', '.zip')},
    })
    output({'release_sha': release_sha})


def read_metadata(directory: Path) -> dict[str, Any]:
    filename = MANIFEST if (directory / MANIFEST).exists() else PREPARED
    return json.loads((directory / filename).read_text())


def verify(directory: Path, expected_version: str, repository: str, source_sha: str) -> dict[str, Any]:
    metadata = read_metadata(directory)
    if (metadata['version'] != expected_version or metadata['repository'] != repository
            or metadata['source_sha'] != source_sha or metadata['tag'] != 'v' + expected_version):
        raise ValueError('Frozen artifacts do not match this release')
    for name, checksum in metadata['files'].items():
        if Path(name).name != name or digest(directory / name) != checksum:
            raise ValueError('Release artifact checksum mismatch: ' + name)
    if digest(directory / 'source.bundle') != metadata['source_bundle_sha256']:
        raise ValueError('Release source bundle checksum mismatch')
    if digest(directory / 'Benchset.xcframework.zip') != metadata['xcframework_sha256']:
        raise ValueError('XCFramework checksum mismatch')
    validate_native_files(directory, expected_version)
    actual = {path.name for path in directory.iterdir()
              if path.is_file() and path.name not in (PREPARED, MANIFEST, 'source.bundle', 'SHA256SUMS')}
    if actual != set(metadata['files']):
        raise ValueError('Release artifact inventory mismatch')
    if (directory / MANIFEST).exists():
        names = sorted([*metadata['files'], MANIFEST])
        checksums = ''.join(digest(directory / name) + '  ' + name + '\n' for name in names)
        if (directory / 'SHA256SUMS').read_text() != checksums:
            raise ValueError('Release checksum manifest mismatch')
    return metadata


def checkout(root: Path, directory: Path, expected_version: str, repository: str, source_sha: str) -> None:
    metadata = verify(directory, expected_version, repository, source_sha)
    git(root, 'fetch', str((directory / 'source.bundle').resolve()), BUNDLE_REF)
    release_sha = metadata['release_sha']
    if git(root, 'rev-parse', 'FETCH_HEAD') != release_sha:
        raise ValueError('Unexpected release commit in the source bundle')
    if git(root, 'rev-parse', release_sha + '^') != source_sha:
        raise ValueError('Unexpected release source parent')
    source_files = set(git(root, 'ls-tree', '-r', '--name-only', source_sha).splitlines())
    generated_files = [name for name in VERSION_FILES if name in source_files]
    changes = set(git(root, 'diff', '--name-only', source_sha, release_sha).splitlines())
    allowed = {'Package.swift', *generated_files}
    if (not changes <= allowed
            or git(root, 'diff', '--summary', source_sha, release_sha, '--', *sorted(allowed))):
        raise ValueError('Release commit contains unexpected source changes')
    # Recompute the allowed edits from the immutable source, rather than trusting
    # arbitrary changes anywhere in a generated manifest or its documentation.
    with tempfile.TemporaryDirectory(prefix='benchset-release-versions-') as temporary:
        original = Path(temporary)
        for name in ('pyproject.toml', *generated_files):
            text = subprocess.check_output(['git', 'show', source_sha + ':' + name], cwd=root, encoding='utf-8')
            (original / name).write_text(text, encoding='utf-8')
        sync_version(original)
        for name in generated_files:
            text = subprocess.check_output(['git', 'show', release_sha + ':' + name], cwd=root, encoding='utf-8')
            if text != (original / name).read_text(encoding='utf-8'):
                raise ValueError('Release commit contains unexpected source changes: ' + name)
    git(root, 'checkout', '--detach', release_sha)
    source_manifest = git(root, 'show', source_sha + ':Package.swift') + '\n'
    if (root / 'Package.swift').read_text() != swift_manifest(
        source_manifest, repository, expected_version, metadata['xcframework_sha256'],
    ):
        raise ValueError('Release Swift manifest does not match its artifact')
    if version(root) != expected_version:
        raise ValueError('Release source version mismatch')
    sync_version(root, check=True)


def assemble(prepared: Path, sources: Path, destination: Path) -> None:
    metadata = read_metadata(prepared)
    verify(prepared, metadata['version'], metadata['repository'], metadata['source_sha'])
    collect(prepared, destination, ('.whl', '.zip', '.bundle'))
    collect(sources, destination, ('.tar.gz', '.crate'))
    required = {'benchset-' + metadata['version'] + '.tar.gz', 'benchset-' + metadata['version'] + '.crate'}
    if {path.name for path in destination.iterdir() if path.name.endswith(('.tar.gz', '.crate'))} != required:
        raise ValueError('Expected one version-matched sdist and Rust crate')
    metadata['files'] = {
        path.name: digest(path) for path in sorted(destination.iterdir()) if path.name != 'source.bundle'
    }
    save(destination / MANIFEST, metadata)
    names = sorted([*metadata['files'], MANIFEST])
    (destination / 'SHA256SUMS').write_text(''.join(digest(destination / name) + '  ' + name + '\n' for name in names))


def registry_status(directory: Path) -> dict[str, str]:
    metadata = read_metadata(directory)
    package_version = metadata['version']
    expected = {name: checksum for name, checksum in metadata['files'].items() if name.endswith(('.whl', '.tar.gz'))}
    pypi = get_json('https://pypi.org/pypi/benchset/' + package_version + '/json')
    uploaded = {}
    if pypi is not None:
        uploaded = {item['filename']: item['digests']['sha256'] for item in pypi['urls']}
        for name, checksum in uploaded.items():
            if expected.get(name) != checksum:
                raise ValueError('PyPI already contains a different artifact: ' + name)
    crate = get_json('https://crates.io/api/v1/crates/benchset/' + package_version)
    publish_crate = crate is None
    if crate is not None and crate['version']['checksum'] != metadata['files']['benchset-' + package_version + '.crate']:
        raise ValueError('crates.io already contains a different crate')
    return {'pypi': str(uploaded != expected).lower(), 'crate': str(publish_crate).lower()}


def stage(root: Path, directory: Path) -> None:
    metadata = read_metadata(directory)
    checkout(root, directory, metadata['version'], metadata['repository'], metadata['source_sha'])
    tag = metadata['tag']
    existing = subprocess.run(['git', 'rev-parse', '--verify', 'refs/tags/' + tag + '^{commit}'],
                              cwd=root, capture_output=True, text=True, check=False)
    if existing.returncode == 0 and existing.stdout.strip() != metadata['release_sha']:
        raise ValueError('Refusing to replace an existing release tag')
    if existing.returncode:
        git(root, 'push', 'origin', metadata['release_sha'] + ':refs/tags/' + tag)
    release = github_release(metadata['repository'], tag)
    if release is None:
        subprocess.run(['gh', 'release', 'create', tag, '--repo', metadata['repository'],
                        '--draft', '--verify-tag', '--title', tag, '--generate-notes'], check=True)
    elif not release['draft']:
        raise ValueError('Refusing to modify a published GitHub Release')
    files = [str((directory / name).resolve()) for name in sorted(metadata['files'])]
    files.extend(str((directory / name).resolve()) for name in (MANIFEST, 'SHA256SUMS'))
    subprocess.run(['gh', 'release', 'upload', tag, '--repo', metadata['repository'], '--clobber', *files], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'prepare', 'checkout', 'assemble', 'verify', 'registry-status', 'stage', 'check-crate'))
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY', 'mutating/benchset'))
    parser.add_argument('--run-id', default=os.environ.get('GITHUB_RUN_ID', '0'))
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--artifacts', type=Path)
    parser.add_argument('--sources', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--version')
    parser.add_argument('--source-sha')
    arguments = parser.parse_args()
    root = Path.cwd()
    if arguments.command == 'plan':
        output(plan(root, arguments.repository, arguments.run_id, arguments.publish))
    elif arguments.command == 'prepare':
        prepare(root, arguments.artifacts, arguments.output, arguments.repository, arguments.run_id)
    elif arguments.command == 'assemble':
        assemble(arguments.artifacts, arguments.sources, arguments.output)
    elif arguments.command in ('checkout', 'verify'):
        if arguments.command == 'checkout':
            checkout(root, arguments.directory, arguments.version, arguments.repository, arguments.source_sha)
        else:
            verify(arguments.directory, arguments.version, arguments.repository, arguments.source_sha)
    elif arguments.command == 'registry-status':
        output(registry_status(arguments.directory))
    elif arguments.command == 'stage':
        stage(root, arguments.directory)
    else:
        metadata = read_metadata(arguments.directory)
        filename = 'benchset-' + metadata['version'] + '.crate'
        if digest(target_directory(root) / 'package' / filename) != metadata['files'][filename]:
            raise ValueError('Cargo repackaged a different crate; refusing to publish')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print('Release failed: ' + str(error), file=sys.stderr)
        sys.exit(1)
