"""Negative controls for the coverage gate itself, independent of LLVM installation."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import check_native_coverage as coverage


@pytest.mark.parametrize('defect', ['none', 'missing', 'extra', 'lines', 'regions', 'functions', 'instantiations', 'branches'])
def test_coverage_gate_rejects_incomplete_inventory_and_every_reported_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str,
) -> None:
    """Reject omitted source files and partial counters, retaining a useful report.

    Zero branch counters mean LLVM did not supply that metric; they must not
    disguise an uncovered line, region, function, or generic instantiation.
    """
    monkeypatch.setattr(coverage, 'ROOT', tmp_path)
    source = tmp_path / 'production.rs'
    summary = {metric: {'count': 2, 'covered': 2} for metric in
               ('lines', 'regions', 'functions', 'instantiations', 'branches')}
    if defect == 'none':
        summary['branches'] = {'count': 0, 'covered': 0}
    elif defect in summary:
        summary[defect]['covered'] = 1
    entries = [{'filename': str(source), 'summary': summary}]
    if defect == 'missing':
        entries.clear()
    elif defect == 'extra':
        entries.append({'filename': str(tmp_path / 'test.rs'), 'summary': summary})
    document = {'data': [{'files': entries}]}
    if defect == 'none':
        coverage.enforce(document, [source], tmp_path)
    else:
        with pytest.raises(RuntimeError, match='inventory mismatch' if defect in ('missing', 'extra') else defect):
            coverage.enforce(document, [source], tmp_path)
    if defect not in ('missing', 'extra'):
        assert json.loads((tmp_path / 'summary.json').read_text()) == {'production.rs': summary}


@pytest.mark.parametrize('windows', [False, True])
def test_coverage_export_uses_matching_binary_profile_and_production_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: bool,
) -> None:
    """Keep exported JSON, readable regions, and enforcement on the same inputs.

    A valid report from a stale binary/profile would otherwise produce a false
    green gate even though the current production files were never exercised.
    """
    source, binary, profile = (tmp_path / name for name in ('source.swift', 'tests', 'fresh.profdata'))
    monkeypatch.setattr(coverage, 'os', SimpleNamespace(name='nt' if windows else 'posix'))
    document: dict[str, Any] = {'data': [{'files': []}]}
    calls = []

    def output(arguments: list[str]) -> str:
        calls.append(arguments)
        return json.dumps(document)

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        assert kwargs['check'] is True
        assert kwargs['cwd'] == coverage.ROOT
        kwargs['stdout'].write('region report')
        return subprocess.CompletedProcess(arguments, 0)

    enforced = []
    monkeypatch.setattr(coverage, 'output', output)
    monkeypatch.setattr(coverage.subprocess, 'run', run)
    monkeypatch.setattr(coverage, 'enforce', lambda *args: enforced.append(args))
    coverage.export(tmp_path, binary, profile, [source], tmp_path)
    for arguments, operation in zip(calls, ('export', 'show')):
        assert arguments[1:5] == [operation, str(binary), '-instr-profile=' + str(profile), str(source)]
        assert Path(arguments[0]).stem == 'llvm-cov'
    assert calls[1][-1] == '-show-regions'
    assert json.loads((tmp_path / 'coverage.json').read_text()) == document
    assert (tmp_path / 'coverage.txt').read_text() == 'region report'
    assert enforced == [(document, [source], tmp_path)]


@pytest.mark.parametrize('artifacts', [0, 1, 2])
@pytest.mark.parametrize('build_failure', [False, True])
@pytest.mark.parametrize('configuration', [(explicit, windows) for explicit in (False, True) for windows in (False, True)])
def test_rust_coverage_runs_only_the_fresh_instrumented_library_executable(  # noqa: PLR0915 - one build/merge protocol with failure controls
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifacts: int, build_failure: bool, configuration: tuple[bool, bool],
) -> None:
    """Use Cargo's executable record and fresh profiles rather than stale globs.

    Build failures and ambiguous executables must abort before any old test
    binary can be run or its counters accepted as current coverage.
    Match the complete profile path using the host's path separators.
    """
    explicit_tools, windows = configuration
    suffix = '.exe' if windows else ''
    monkeypatch.setattr(coverage, 'os', SimpleNamespace(name='nt' if windows else 'posix', environ=os.environ))
    monkeypatch.setattr(coverage, 'ROOT', tmp_path)
    llvm = tmp_path / 'llvm' if explicit_tools else tmp_path / 'lib/rustlib/test-host/bin'
    llvm.mkdir(parents=True)
    (llvm / ('llvm-cov' + suffix)).touch()
    native = tmp_path / 'native/src'
    native.mkdir(parents=True)
    production, tests = native / 'lib.rs', native / 'suite_tests.rs'
    production.touch()
    tests.touch()
    example = tmp_path / 'examples/consumer.rs'
    example.parent.mkdir()
    example.touch()
    stale = tmp_path / 'stale.profraw'
    stale.touch()
    executable = str(tmp_path / 'fresh-tests')
    calls, exported = [], []
    def output(arguments: list[str]) -> str:
        if arguments == ['rustc', '--print', 'sysroot']:
            return str(tmp_path)
        assert arguments == ['rustc', '-vV']
        return 'rustc 1.94.1\nbinary: rustc\nhost: test-host\nrelease: 1.94.1\nLLVM version: 22'

    monkeypatch.setattr(coverage, 'output', output)

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        if arguments[0] == 'cargo':
            assert arguments[1] == 'test'
            assert '--lib' in arguments
            assert '--locked' in arguments
            assert '--no-run' in arguments
            assert '--message-format=json' in arguments
            assert kwargs['capture_output'] is True
            assert kwargs['text'] is True
            assert kwargs['check'] is False
            assert kwargs['env']['RUSTFLAGS'] == '-C instrument-coverage'
            assert kwargs['env']['CARGO_INCREMENTAL'] == '0'
            assert kwargs['env']['CARGO_TARGET_DIR'] == str(tmp_path / 'build')
            assert not stale.exists()
            return subprocess.CompletedProcess(arguments, int(build_failure),
                                               '\n'.join(json.dumps({'executable': executable}) for _ in range(artifacts)),
                                               'compiler failed')
        assert kwargs['check'] is True
        if arguments[0] == executable:
            assert arguments[1:] == ['--test-threads=1']
            assert kwargs['env']['LLVM_PROFILE_FILE'] == str(tmp_path / '%p-%m.profraw')
            (tmp_path / 'fresh.profraw').touch()
        else:
            assert arguments[0] == str(llvm / ('llvm-profdata' + suffix))
            assert arguments[1:3] == ['merge', '-sparse']
            assert str(tmp_path / 'fresh.profraw') in arguments
            assert str(stale) not in arguments
            assert arguments[-2:] == ['-o', str(tmp_path / 'coverage.profdata')]
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(coverage.subprocess, 'run', run)
    monkeypatch.setattr(coverage, 'export', lambda *args: exported.append(args))
    if build_failure or artifacts != 1:
        with pytest.raises(RuntimeError, match='compiler failed' if build_failure else 'exactly one'):
            coverage.rust(tmp_path, llvm if explicit_tools else None)
        assert len(calls) == 1
        assert not exported
    else:
        coverage.rust(tmp_path, llvm if explicit_tools else None)
        assert len(calls) == 3
        assert exported == [(llvm, executable, tmp_path / 'coverage.profdata', [production], tmp_path)]


@pytest.mark.parametrize('architecture', ['arm64', 'x86_64'])
@pytest.mark.parametrize('framework_available', [False, True])
def test_swift_coverage_discards_stale_profiles_and_keeps_test_target_instrumented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, architecture: str, framework_available: bool,
) -> None:
    """Collect Swift counters from this test build and the matching executable.

    Old profiles, missing instrumentation, a wrong target architecture or testing
    framework path can otherwise skip tests or report unrelated coverage.
    """
    monkeypatch.setattr(coverage, 'ROOT', tmp_path)
    monkeypatch.setattr(coverage.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(coverage.platform, 'machine', lambda: architecture)
    developer = tmp_path / 'developer'
    frameworks = developer / 'Library/Developer/Frameworks'
    frameworks.mkdir(parents=True)
    if framework_available:
        (frameworks / 'Testing.framework').mkdir(parents=True)
    scratch = tmp_path / 'build'
    stale = scratch / 'old/debug/codecov'
    stale.mkdir(parents=True)
    (stale / 'default.profdata').write_bytes(b'old profile')
    report = scratch / 'fresh/debug/codecov/report.json'
    llvm = tmp_path / 'llvm'
    source = tmp_path / 'Sources/Benchset.swift'
    source.parent.mkdir()
    source.touch()
    test_source = tmp_path / 'tests/Test.swift'
    test_source.parent.mkdir()
    test_source.touch()
    calls, exported = [], []

    def output(arguments: list[str]) -> str:
        if arguments == ['xcode-select', '-p']:
            return str(developer)
        if arguments == ['xcrun', '--find', 'llvm-cov']:
            return str(llvm / 'llvm-cov')
        assert arguments == ['swift', 'test', '--scratch-path', str(scratch), '--show-codecov-path']
        return str(report)

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert not stale.exists()
        calls.append(arguments)
        assert kwargs == {'cwd': tmp_path, 'check': True}
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(coverage, 'output', output)
    monkeypatch.setattr(coverage.subprocess, 'run', run)
    monkeypatch.setattr(coverage, 'export', lambda *args: exported.append(args))
    coverage.swift(tmp_path)
    assert len(calls) == 1
    arguments = calls[0]
    assert arguments[:4] == ['swift', 'test', '--scratch-path', str(scratch)]
    assert '--disable-xctest' in arguments
    assert '--enable-swift-testing' in arguments
    assert '--enable-code-coverage' in arguments
    assert arguments[arguments.index('-target') - 1:arguments.index('-target') + 3] == [
        '-Xswiftc', '-target', '-Xswiftc', architecture + '-apple-macosx13.0',
    ]
    assert (str(frameworks) in arguments) is framework_available
    if framework_available:
        assert arguments[-8:] == ['-Xswiftc', '-F', '-Xswiftc', str(frameworks),
                                  '-Xlinker', '-F', '-Xlinker', str(frameworks)]
    executable = report.parent.parent / 'BenchsetPackageTests.xctest/Contents/MacOS/BenchsetPackageTests'
    assert exported == [(llvm, executable, report.parent / 'default.profdata', [source], tmp_path)]


@pytest.mark.parametrize('language', ['rust', 'swift'])
def test_coverage_command_synchronizes_version_and_dispatches_requested_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, language: str,
) -> None:
    """Dispatch the requested gate and permit reruns in the same output directory.

    Coverage profiles and reports survive an earlier invocation. Their existing
    destination must not prevent the next run from reaching its selected gate.
    """
    monkeypatch.setattr(coverage, 'ROOT', tmp_path)
    monkeypatch.setattr(coverage.sys, 'argv', ['coverage', language, '--llvm-tools', str(tmp_path / 'llvm')])
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(coverage, 'sync_version', lambda root: calls.append(('version', root)))

    def rust(destination: Path, tools: Path) -> None:
        assert destination.is_dir()
        calls.append(('rust', destination, tools))

    def swift(destination: Path) -> None:
        assert destination.is_dir()
        calls.append(('swift', destination))

    monkeypatch.setattr(coverage, 'rust', rust)
    monkeypatch.setattr(coverage, 'swift', swift)
    coverage.main()
    coverage.main()
    expected = (language, tmp_path / 'target/coverage' / language)
    assert calls == [('version', tmp_path), (*expected, tmp_path / 'llvm') if language == 'rust' else expected] * 2


def test_coverage_tool_output_keeps_project_directory_environment_and_text_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the actual output wrapper: compiler answers become paths, not raw bytes.

    The child observes the chosen directory/environment; surrounding whitespace
    is intentionally emitted to protect filesystem lookup from compiler newlines.
    """
    monkeypatch.setattr(coverage, 'ROOT', tmp_path)
    program = 'import os; print("  " + os.getcwd() + "|" + os.environ["BENCHSET_TEST_VALUE"] + "  ")'
    environment = dict(os.environ, BENCHSET_TEST_VALUE='private-child-value')
    result = coverage.output([coverage.sys.executable, '-c', program], env=environment)
    assert result == str(tmp_path) + '|private-child-value'
