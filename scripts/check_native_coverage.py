"""Run native-language tests and enforce complete production-source coverage.

Rust requires the llvm-tools-preview component of its exact compiler toolchain.
Swift tests require Swift 6 / Swift Testing; the shipped wrapper still builds
with the Package.swift deployment target. Reports contain no test-source files.
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.version import sync_version

ROOT = Path(__file__).resolve().parents[1]


def output(command, env=None):
    return subprocess.check_output(command, cwd=ROOT, env=env, text=True).strip()


def enforce(document, files, destination):
    entries = document['data'][0]['files']
    expected = {str(path.resolve()) for path in files}
    actual = {str(Path(entry['filename']).resolve()) for entry in entries}
    if actual != expected:
        raise RuntimeError('coverage source inventory mismatch: ' + str(expected ^ actual))
    summaries = {}
    failures = []
    for entry in entries:
        name = str(Path(entry['filename']).relative_to(ROOT))
        summary = entry['summary']
        summaries[name] = summary
        for metric in ('lines', 'regions', 'functions', 'instantiations', 'branches'):
            count, covered = summary[metric]['count'], summary[metric]['covered']
            if count:
                print(f'{name}: {metric} {covered}/{count} ({covered / count:.2%})')
                if count != covered:
                    failures.append(f'{name}: {metric} {count - covered} uncovered')
    (destination / 'summary.json').write_text(json.dumps(summaries, indent=2) + '\n')
    if failures:
        raise RuntimeError('\n'.join(failures))
    print('Branch counters are unavailable when LLVM reports count=0; line/region coverage is not branch coverage.')


def export(llvm, executable, profile, files, destination):
    suffix = '.exe' if os.name == 'nt' else ''
    arguments = [str(executable), '-instr-profile=' + str(profile), *map(str, files)]
    document = json.loads(output([str(llvm / ('llvm-cov' + suffix)), 'export', *arguments]))
    (destination / 'coverage.json').write_text(json.dumps(document) + '\n')
    with (destination / 'coverage.txt').open('w') as stream:
        subprocess.run([str(llvm / ('llvm-cov' + suffix)), 'show', *arguments, '-show-regions'],
                       cwd=ROOT, stdout=stream, check=True)
    enforce(document, files, destination)


def rust(destination, tools_path):
    sysroot = Path(output(['rustc', '--print', 'sysroot']))
    host = next(line.split(': ', 1)[1] for line in output(['rustc', '-vV']).splitlines() if line.startswith('host:'))
    llvm = tools_path.resolve() if tools_path else sysroot / 'lib/rustlib' / host / 'bin'
    suffix = '.exe' if os.name == 'nt' else ''
    if not (llvm / ('llvm-cov' + suffix)).is_file():
        raise RuntimeError('Install llvm-tools-preview for the pinned Rust toolchain, or pass --llvm-tools DIR')
    for profile in destination.glob('*.profraw'):
        profile.unlink()
    env = dict(os.environ, CARGO_INCREMENTAL='0', CARGO_TARGET_DIR=str(destination / 'build'),
               RUSTFLAGS='-C instrument-coverage', LLVM_PROFILE_FILE=str(destination / '%p-%m.profraw'))
    # Building without running lets us find the exact test binary without globs
    # that might accidentally collect executables from older builds.
    build = subprocess.run(['cargo', 'test', '--lib', '--locked', '--no-run', '--message-format=json'],
                           cwd=ROOT, env=env, capture_output=True, text=True, check=False)
    if build.returncode:
        raise RuntimeError(build.stderr)
    artifacts = [json.loads(line) for line in build.stdout.splitlines()]
    executables = [item['executable'] for item in artifacts if item.get('executable')]
    if len(executables) != 1:
        raise RuntimeError('expected exactly one Rust library test executable')
    executable = executables[0]
    subprocess.run([executable, '--test-threads=1'], cwd=ROOT, env=env, check=True)
    profile = destination / 'coverage.profdata'
    subprocess.run([str(llvm / ('llvm-profdata' + suffix)), 'merge', '-sparse',
                    *map(str, destination.glob('*.profraw')), '-o', str(profile)], check=True)
    files = sorted(path for path in (ROOT / 'native/src').rglob('*.rs') if not path.name.endswith('_tests.rs'))
    export(llvm, executable, profile, files, destination)


def swift(destination):
    if platform.system() != 'Darwin':
        raise RuntimeError('the Swift wrapper currently targets macOS')
    scratch = destination / 'build'
    for old_profiles in scratch.glob('**/codecov'):
        shutil.rmtree(old_profiles)
    architecture = 'arm64' if platform.machine() == 'arm64' else 'x86_64'
    # Swift Testing needs a modern OS target for its test executable; this does
    # not modify the library's macOS 10.12 deployment target in Package.swift.
    arguments = ['--scratch-path', str(scratch), '--disable-xctest', '--enable-swift-testing',
                 '--enable-code-coverage', '-Xswiftc', '-target', '-Xswiftc', architecture + '-apple-macosx13.0']
    developer = Path(output(['xcode-select', '-p']))
    frameworks = developer / 'Library/Developer/Frameworks'
    if (frameworks / 'Testing.framework').is_dir():
        arguments += ['-Xswiftc', '-F', '-Xswiftc', str(frameworks),
                      '-Xlinker', '-F', '-Xlinker', str(frameworks)]
    subprocess.run(['swift', 'test', *arguments], cwd=ROOT, check=True)
    report = Path(output(['swift', 'test', '--scratch-path', str(scratch), '--show-codecov-path']))
    executable = report.parent.parent / 'BenchsetPackageTests.xctest/Contents/MacOS/BenchsetPackageTests'
    llvm = Path(output(['xcrun', '--find', 'llvm-cov'])).parent
    export(llvm, executable, report.parent / 'default.profdata',
           sorted((ROOT / 'Sources').rglob('*.swift')), destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('language', choices=['rust', 'swift'])
    parser.add_argument('--llvm-tools', type=Path, help='Rust LLVM tools directory (must match rustc LLVM version)')
    args = parser.parse_args()
    sync_version(ROOT)
    destination = ROOT / 'target/coverage' / args.language
    destination.mkdir(parents=True, exist_ok=True)
    if args.language == 'rust':
        rust(destination, args.llvm_tools)
    else:
        swift(destination)


if __name__ == '__main__':
    main()
