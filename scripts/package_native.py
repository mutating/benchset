"""Package native libraries; optionally build a universal macOS Swift package.

No Xcode project or xcodebuild is required: this XCFramework contains a static
C library. A macOS SDK, lipo and both Rust macOS targets are required.
"""
import argparse
import plistlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def archive(source, destination):
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as output:
        for path in sorted(source.rglob('*')):
            if path.is_file():
                output.write(path, path.relative_to(source))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', required=True)
    parser.add_argument('--apple', action='store_true')
    args = parser.parse_args()
    subprocess.run(['cargo', 'build', '--release', '--locked', '--target', args.target], cwd=ROOT, check=True)
    staging = ROOT / 'release-artifacts' / args.target
    staging.mkdir(parents=True, exist_ok=True)
    source = ROOT / 'target' / args.target / 'release'
    (staging / 'lib').mkdir(exist_ok=True)
    for filename in ('libbenchset.a', 'benchset.lib', 'libbenchset.dylib', 'libbenchset.so', 'benchset.dll'):
        if (source / filename).exists():
            shutil.copy2(source / filename, staging / 'lib' / filename)
    shutil.copytree(ROOT / 'include', staging / 'include', dirs_exist_ok=True)
    for filename in ('README.md', 'LICENSE'):
        shutil.copy2(ROOT / filename, staging / filename)
    archive(staging, staging.parent / ('benchset-' + args.target + '.zip'))
    if not args.apple:
        return
    if sys.platform != 'darwin':
        parser.error('--apple requires macOS')
    for target in ('aarch64-apple-darwin', 'x86_64-apple-darwin'):
        subprocess.run(['cargo', 'build', '--release', '--locked', '--target', target], cwd=ROOT, check=True)
    framework = ROOT / 'Artifacts/Benchset.xcframework'
    library = framework / 'macos-arm64_x86_64'
    library.mkdir(parents=True, exist_ok=True)
    subprocess.run(['lipo', '-create',
                    str(ROOT / 'target/aarch64-apple-darwin/release/libbenchset.a'),
                    str(ROOT / 'target/x86_64-apple-darwin/release/libbenchset.a'),
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
    swift_package.mkdir(exist_ok=True)
    shutil.copy2(ROOT / 'Package.swift', swift_package / 'Package.swift')
    shutil.copytree(ROOT / 'Sources', swift_package / 'Sources', dirs_exist_ok=True)
    shutil.copytree(ROOT / 'tests/swift', swift_package / 'tests/swift', dirs_exist_ok=True)
    shutil.copytree(ROOT / 'Artifacts', swift_package / 'Artifacts', dirs_exist_ok=True)
    shutil.copy2(ROOT / 'README.md', swift_package / 'README.md')
    shutil.copy2(ROOT / 'LICENSE', swift_package / 'LICENSE')
    archive(swift_package, swift_package.parent / 'Benchset-Swift.zip')
    archive(ROOT / 'Artifacts', swift_package.parent / 'Benchset.xcframework.zip')


if __name__ == '__main__':
    main()
