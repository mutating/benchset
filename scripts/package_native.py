"""Package native libraries; optionally build a universal macOS Swift package.

No Xcode project or xcodebuild is required: this XCFramework contains a static
C library. A macOS SDK, lipo and both Rust macOS targets are required.
Every package is a fresh snapshot, including when generated outputs already exist.
"""
import argparse
import plistlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.cargo import target_directory
from scripts.version import sync_version

ROOT = Path(__file__).resolve().parents[1]


def archive(source, destination):
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as output:
        for path in sorted(source.rglob('*')):
            if path.is_file():
                output.write(path, path.relative_to(source))


def fresh_directory(directory):
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', required=True)
    parser.add_argument('--apple', action='store_true')
    args = parser.parse_args()
    sync_version(ROOT)
    subprocess.run(['cargo', 'build', '--release', '--locked', '--target', args.target], cwd=ROOT, check=True)
    staging = ROOT / 'release-artifacts' / args.target
    fresh_directory(staging)
    cargo_output = target_directory(ROOT)
    source = cargo_output / args.target / 'release'
    (staging / 'lib').mkdir(exist_ok=True)
    for filename in ('libbenchset.a', 'benchset.lib', 'libbenchset.dylib', 'libbenchset.so',
                     'benchset.dll', 'benchset.dll.lib'):
        if (source / filename).exists():
            destination = staging / 'lib' / filename
            shutil.copy2(source / filename, destination)
            if filename.endswith('.dylib'):
                subprocess.run(['install_name_tool', '-id', '@rpath/' + filename, str(destination)], check=True)
                # Ship a valid ad-hoc signature for both Apple architectures.
                subprocess.run(['codesign', '--force', '--sign', '-', str(destination)], check=True)
    shutil.copytree(ROOT / 'include', staging / 'include', dirs_exist_ok=True)
    for filename in ('README.md', 'LICENSE'):
        shutil.copy2(ROOT / filename, staging / filename)
    (staging / 'native').mkdir(exist_ok=True)
    shutil.copy2(ROOT / 'native/reference-calibration.json', staging / 'native/reference-calibration.json')
    archive(staging, staging.parent / ('benchset-' + args.target + '.zip'))
    if not args.apple:
        return
    if sys.platform != 'darwin':
        parser.error('--apple requires macOS')
    for target in ('aarch64-apple-darwin', 'x86_64-apple-darwin'):
        subprocess.run(['cargo', 'build', '--release', '--locked', '--target', target], cwd=ROOT, check=True)
    framework = ROOT / 'Artifacts/Benchset.xcframework'
    fresh_directory(framework)
    library = framework / 'macos-arm64_x86_64'
    library.mkdir(parents=True, exist_ok=True)
    subprocess.run(['lipo', '-create',
                    str(cargo_output / 'aarch64-apple-darwin/release/libbenchset.a'),
                    str(cargo_output / 'x86_64-apple-darwin/release/libbenchset.a'),
                    '-output', str(library / 'libbenchset.a')], check=True)
    shutil.copytree(ROOT / 'include', library / 'Headers', dirs_exist_ok=True)
    metadata = {
        'CFBundlePackageType': 'XFWK', 'XCFrameworkFormatVersion': '1.0',
        'AvailableLibraries': [{
            'LibraryIdentifier': 'macos-arm64_x86_64', 'LibraryPath': 'libbenchset.a',
            'HeadersPath': 'Headers', 'SupportedArchitectures': ['arm64', 'x86_64'],
            'SupportedPlatform': 'macos',
        }],
    }
    with (framework / 'Info.plist').open('wb') as output:
        plistlib.dump(metadata, output)
    swift_package = ROOT / 'release-artifacts/Benchset-Swift'
    fresh_directory(swift_package)
    shutil.copy2(ROOT / 'Package.swift', swift_package / 'Package.swift')
    shutil.copytree(ROOT / 'Sources', swift_package / 'Sources', dirs_exist_ok=True)
    shutil.copytree(ROOT / 'tests/swift', swift_package / 'tests/swift', dirs_exist_ok=True)
    shutil.copytree(ROOT / 'Artifacts', swift_package / 'Artifacts', dirs_exist_ok=True)
    shutil.copy2(ROOT / 'README.md', swift_package / 'README.md')
    shutil.copy2(ROOT / 'LICENSE', swift_package / 'LICENSE')
    (swift_package / 'native').mkdir(exist_ok=True)
    shutil.copy2(ROOT / 'native/reference-calibration.json', swift_package / 'native/reference-calibration.json')
    archive(swift_package, swift_package.parent / 'Benchset-Swift.zip')
    archive(ROOT / 'Artifacts', swift_package.parent / 'Benchset.xcframework.zip')


if __name__ == '__main__':
    main()
