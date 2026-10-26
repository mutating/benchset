"""CLI syntax, terminal presentation and real module/console entry points."""

import argparse
import io
import locale
import os
import signal
import subprocess
import sys
import sysconfig
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event, current_thread
from typing import Callable, Dict, List, Optional, Tuple

import pytest

from benchset import (
    PI_REFERENCE_MACHINE,
    BenchmarkResult,
    BenchmarkRunner,
    Progress,
    Sample,
    __version__,
    cli,
)


def assert_short_cli_completed_or_no_work(
    result: subprocess.CompletedProcess, names: List[str], warmup: float, encoding: str = 'utf-8',
) -> None:
    output = result.stdout.decode(encoding) if isinstance(result.stdout, bytes) else result.stdout
    errors = result.stderr.decode(encoding) if isinstance(result.stderr, bytes) else result.stderr
    assert '\r' not in output + errors
    assert '\x1b' not in output + errors
    completed = output.rstrip().split('\n\n') if output else []
    failed = errors.rstrip().split('\n\n') if errors else []
    if failed:
        assert result.returncode == 1
        assert len(failed) == 1
        assert len(completed) < len(names)
    else:
        assert result.returncode == 0, errors
        assert len(completed) == len(names)
    for index, section in enumerate(completed + failed):
        table, samples = section.split('Samples:\n')
        fields = {key.strip(): value.strip() for key, value in (
            line.split(' : ', 1) for line in table.splitlines()
        )}
        assert len(fields) == len(table.splitlines())
        assert fields['Benchmark'] == names[index]
        assert fields['Version'] == __version__
        assert fields['Reference machine'] == f'{PI_REFERENCE_MACHINE.name} ({PI_REFERENCE_MACHINE.short_name})'
        assert float(fields['Warmup seconds']) == warmup
        assert float(fields['Measurement seconds']) == 0.03
        assert fields['Unit'] == 'iterations/s'
        if index < len(completed):
            assert fields['Status'] == 'completed'
            number, unit = fields['Pi equivalent'].split(' \u00d7 ' if encoding == 'utf-8' else ' x ')
            assert float(number) > 0
            assert unit == 'pi'
        else:
            # A delayed worker may miss the short measurement window entirely.
            unavailable = fields['Score']
            assert unavailable in ('—', '-', '\\u2014')
            assert fields['Status'] == 'failed'
            assert fields['Reason'] == 'measurement window was too short to complete any work'
            assert fields['Success'] == fields['Unstable'] == 'False'
            assert fields['Iterations'] == '0'
            assert fields['Score'] == fields['Pi equivalent'] == fields['Variation percent'] == unavailable
            if warmup == 0:
                assert fields['Checksum'] == '0'
            if samples != '  ' + unavailable:
                rows = [[cell.strip() for cell in row.split('|')] for row in samples.splitlines()]
                assert rows[0] == ['elapsed s', 'duration s', 'iterations', 'iterations/s']
                for elapsed, duration, iterations, rate in rows[1:]:
                    assert float(elapsed) >= 0
                    assert float(duration) > 0
                    assert int(iterations) == float(rate) == 0


@pytest.mark.parametrize('encoding', ['ascii', 'cp1251', 'utf-8'])
@pytest.mark.parametrize('errors', ['strict', 'backslashreplace'])
def test_short_cli_no_work_check_uses_actual_stream_fallbacks(encoding: str, errors: str) -> None:
    failed = BenchmarkResult(
        'single_core', status='failed', reason='measurement window was too short to complete any work',
        warmup_seconds=0, measurement_seconds=0.03,
    )
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding=encoding, errors=errors)
    cli.render(failed, stream)
    stream.flush()
    encoded_errors = buffer.getvalue()
    stream.detach()
    assert_short_cli_completed_or_no_work(subprocess.CompletedProcess([], 1, b'', encoded_errors),
                                         ['single_core'], 0, encoding)


def test_concurrent_settings_keep_arguments_and_process_streams_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    arguments, errors = ['benchset'], sys.stderr
    monkeypatch.setattr(sys, 'argv', arguments)
    first_entered, second_entered, first_done = Event(), Event(), Event()
    original_settings = cli.Settings
    original_parse = argparse.ArgumentParser.parse_args

    def parse(parser: argparse.ArgumentParser, args: Optional[List[str]] = None,
              namespace: Optional[argparse.Namespace] = None) -> argparse.Namespace:
        assert sys.stderr is errors, 'argument lookup must not redirect another thread\'s errors'
        return original_parse(parser, args, namespace)

    def construct(**kwargs: object) -> cli.Settings:
        if current_thread().name.endswith('_0'):
            first_entered.set()
            assert second_entered.wait(5)
        else:
            second_entered.set()
            assert first_done.wait(5)
        return original_settings(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, 'Settings', construct)
    monkeypatch.setattr(argparse.ArgumentParser, 'parse_args', parse)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='settings') as executor:
        first = executor.submit(cli.settings, ['--bench=single_core', '--duration=1'])
        try:
            assert first_entered.wait(5)
            second = executor.submit(cli.settings, ['--bench=multi_core', '--duration=2'])
            one = first.result(timeout=5)
        finally:
            first_done.set()
            second_entered.set()
        two = second.result(timeout=5)
    assert (one.bench, one.duration) == ('single_core', 1)
    assert (two.bench, two.duration) == ('multi_core', 2)
    assert sys.argv is arguments
    assert sys.stderr is errors


@pytest.fixture(params=['module', 'console'])
def invocation(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Tuple[List[str], Dict[str, str]]:
    # PTY progress markers and help wrapping must not inherit the host's width.
    environment = dict(os.environ, PYTHONIOENCODING='utf-8', COLUMNS='80')
    # Require explicit decoding of the child's UTF-8 even on UTF-8-mode hosts.
    monkeypatch.setattr(subprocess, '_text_encoding', lambda: 'ascii', raising=False)
    monkeypatch.setattr(locale, 'getpreferredencoding', lambda *_arguments: 'ascii')
    if request.param == 'module':
        command = [sys.executable, '-m', 'benchset']
    else:
        script = Path(sysconfig.get_path('scripts')) / ('benchset.exe' if os.name == 'nt' else 'benchset')
        assert script.is_file(), 'Install the package before testing its console entry point'
        command = [str(script)]
        # This black-box consumer uses the installed copy. Its separate source
        # paths must not enter the checkout's runtime coverage inventory.
        environment.pop('COVERAGE_PROCESS_START', None)
        environment.pop('COVERAGE_FILE', None)
    return command, environment


@pytest.mark.parametrize('option', ['-h', '--help', '--version'])
def test_information_is_complete_and_has_no_stderr(
    invocation: Tuple[List[str], Dict[str, str]], option: str,
) -> None:
    command, environment = invocation
    result = subprocess.run([*command, option], env=environment, capture_output=True,
                            encoding='utf-8', check=False, timeout=10)
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    if option == '--version':
        assert result.stdout == __version__ + '\n'
    else:
        text = ' '.join(result.stdout.split())
        for value in ('all', 'single_core', 'multi_core', '--warmup SECONDS', '--duration SECONDS',
                      'default: 120', 'default: 60', 'zero allowed', 'positive',
                      'Pi equivalent', '0.5', 'twice', '6 minutes', 'Ctrl+C', '130', 'Examples:'):
            assert value in text
        assert '[WARMUP]' not in text  # A supplied option requires its value.


@pytest.mark.parametrize(('arguments', 'diagnostic'), [
    (['--unknown'], '--unknown'), (['single_core'], 'single_core'), (['--'], '--'),
    (['--bench'], '--bench'), (['--warmup'], '--warmup'), (['--duration'], '--duration'),
    (['--warmup', '--duration', '.03'], '--warmup'),
    (['--bench', 'missing'], 'all'), (['--bench=single-core'], 'single_core'),
    (['--bench='], 'invalid choice'), (['--bench=ALL'], 'invalid choice'),
    (['--warmup='], '--warmup'), (['--duration='], '--duration'),
    (['--warmup', 'oops'], '--warmup'), (['--duration', 'oops'], '--duration'),
    (['--duration', '0,03'], 'number of seconds'),
    (['--warmup', '-1'], 'nonnegative'), (['--duration', '-1'], 'positive'),
    (['--duration', '0'], 'positive'), (['--duration=-0.0'], 'positive'),
    (['--warmup=nan'], 'finite'), (['--duration=nan'], 'finite'),
    (['--warmup=inf'], 'finite'), (['--duration=-inf'], 'finite'),
    (['--warmup=1e9999'], 'finite'), (['--duration=1e9999'], 'finite'),
])
def test_invalid_arguments_are_actionable_without_a_traceback(
    invocation: Tuple[List[str], Dict[str, str]], arguments: List[str], diagnostic: str,
) -> None:
    command, environment = invocation
    result = subprocess.run([*command, *arguments], env=environment, capture_output=True,
                            encoding='utf-8', check=False, timeout=10)
    assert result.returncode == 2
    assert not result.stdout
    assert 'usage: benchset' in result.stderr
    assert 'benchset: error:' in result.stderr
    assert diagnostic in result.stderr
    assert 'Traceback' not in result.stderr


@pytest.mark.parametrize('benchmark', [None, 'all', 'single_core', 'multi_core'])
@pytest.mark.parametrize('equals', [False, True])
@pytest.mark.parametrize('warmup', ['0', '2e-2'])
def test_real_entrypoints_accept_selection_order_numeric_forms_and_redirected_output(
    invocation: Tuple[List[str], Dict[str, str]], benchmark: Optional[str], equals: bool, warmup: str,
) -> None:
    command, environment = invocation
    options = [('duration', '3e-2'), ('warmup', warmup)]
    if benchmark is not None:
        options.append(('bench', benchmark))
    arguments = [part for name, value in options for part in (
        [f'--{name}={value}'] if equals else ['--' + name, value]
    )]
    result = subprocess.run([*command, *arguments], env=environment, capture_output=True,
                            check=False, timeout=10)
    names = ['single_core', 'multi_core'] if benchmark is None or benchmark == 'all' else [benchmark]
    assert_short_cli_completed_or_no_work(result, names, float(warmup))


def test_defaults_repeated_options_and_environment_independence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Tuple[str, float, float]] = []

    def execute(_self: BenchmarkRunner, benchmark: str, *, warmup_seconds: float,
                measurement_seconds: float, **_kwargs: object) -> BenchmarkResult:
        calls.append((benchmark, warmup_seconds, measurement_seconds))
        return BenchmarkResult(benchmark, status='completed', score=1, pi_equivalent=1)

    monkeypatch.setattr(BenchmarkRunner, 'run', execute)
    monkeypatch.setenv('BENCHSET_WARMUP', '0')
    monkeypatch.setenv('BENCHSET_DURATION', '0')
    assert cli.main([]) == 0
    assert calls == [('single_core', 120, 60), ('multi_core', 120, 60)]
    calls.clear()
    assert cli.main(['--bench=all', '--bench', 'multi_core', '--warmup=0.5', '--warmup=-0.0',
                     '--duration=2', '--duration', '+.03']) == 0
    assert calls == [('multi_core', -0.0, 0.03)]


@pytest.mark.parametrize('arguments', [
    ['--warmup', '.0', '--duration', ' .03 '],
    ['--war=0', '--dur=3e-2', '--ben=single_core'],
    ['--bench', 'single_core', '--warmup=+0', '--duration=0.03'],
])
def test_equivalent_numeric_spellings_and_standard_argparse_forms(arguments: List[str]) -> None:
    settings = cli.settings(arguments)
    assert settings.warmup == 0
    assert settings.duration == 0.03


@pytest.mark.parametrize('value', ['-0e0', '-0E-5', '-0.', '-0.e+12', '-0_0e0'])
@pytest.mark.parametrize('equals', [False, True])
def test_negative_zero_has_equivalent_cli_spellings(
    invocation: Tuple[List[str], Dict[str, str]], value: str, equals: bool,
) -> None:
    command, environment = invocation
    option = ['--warmup=' + value] if equals else ['--warmup', value]
    result = subprocess.run([*command, '--bench=single_core', '--duration=.03', *option],
                            env=environment, capture_output=True, encoding='utf-8', check=False, timeout=10)
    assert_short_cli_completed_or_no_work(result, ['single_core'], float(value))


@pytest.mark.parametrize(('option', 'value', 'diagnostic'), [
    ('--warmup', '-1e-3', 'nonnegative'), ('--duration', '-1e-3', 'positive'),
    ('--warmup', '-Inf', 'finite'), ('--duration', '-NaN', 'finite'),
])
def test_negative_numeric_values_reach_their_option_validator(
    invocation: Tuple[List[str], Dict[str, str]], option: str, value: str, diagnostic: str,
) -> None:
    command, environment = invocation
    result = subprocess.run([*command, option, value], env=environment, capture_output=True,
                            encoding='utf-8', check=False, timeout=10)
    assert result.returncode == 2
    assert not result.stdout
    assert option in result.stderr
    assert diagnostic in result.stderr


@pytest.mark.parametrize('encoding', ['ascii', 'cp1251'])
def test_real_redirected_output_supports_non_unicode_encodings(
    invocation: Tuple[List[str], Dict[str, str]], encoding: str,
) -> None:
    command, environment = invocation
    environment['PYTHONIOENCODING'] = encoding + ':strict'
    result = subprocess.run([*command, '--bench=single_core', '--warmup=0', '--duration=.03'],
                            env=environment, capture_output=True, check=False, timeout=10)
    assert_short_cli_completed_or_no_work(result, ['single_core'], 0, encoding)


@pytest.mark.parametrize(('option', 'value'), [
    ('--warmup', '1e300'), ('--duration', '1e300'), ('--duration', '5e-324'),
])
def test_native_duration_limits_report_failure_and_stop_the_suite(
    invocation: Tuple[List[str], Dict[str, str]], option: str, value: str,
) -> None:
    command, environment = invocation
    result = subprocess.run([*command, '--warmup=0', '--duration=.03', option, value],
                            env=environment, capture_output=True, encoding='utf-8', check=False, timeout=10)
    assert result.returncode == 1
    assert not result.stdout
    assert 'failed' in result.stderr
    assert 'Reason' in result.stderr
    assert 'multi_core' not in result.stderr
    assert 'Traceback' not in result.stderr


@pytest.mark.parametrize('encoding', ['ascii', 'cp1251', 'utf-8'])
def test_render_handles_real_stream_encodings_without_losing_values(encoding: str) -> None:
    result = BenchmarkResult('single_core', status='completed', reason='Причина',
                             score=123.5, pi_equivalent=0.5,
                             samples=(Sample(1.0, 1.0, 123, 123.5),))
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding=encoding)
    cli.render(result, stream)
    cli.render(replace(result, samples=(), score=None, pi_equivalent=None), stream)
    stream.flush()
    text = buffer.getvalue().decode(encoding)
    assert '123.500' in text
    assert ('0.5 \u00d7 pi' if encoding == 'utf-8' else '0.5 x pi') in text
    assert ('Причина' if encoding != 'ascii' else '\\u041f') in text
    assert ('  —\n' if encoding != 'ascii' else '  -\n') in text
    stream.detach()


def test_sample_columns_and_result_labels_align_even_with_large_values() -> None:
    result = BenchmarkResult('multi_core', status='completed', score=123.5, pi_equivalent=2,
                             samples=(Sample(1, 1, 1, 1),
                                      Sample(123456.123456, 123455.123456, 2**64 - 1, 123456789.123)))
    output = io.StringIO()
    cli.render(result, output)
    fields, samples = output.getvalue().split('Samples:\n')
    assert len({line.index(' : ') for line in fields.splitlines()}) == 1
    rows = samples.splitlines()
    assert len(rows) == 3
    assert len({tuple(index for index, character in enumerate(row) if character == '|') for row in rows}) == 1
    assert str(2**64 - 1) in rows[-1]
    assert '123,456,789.123' in rows[-1]


@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled'])
def test_result_calibration_links_keep_cli_output_compact(status: str) -> None:
    result = BenchmarkResult('single_core', status=status)
    output = io.StringIO()
    cli.render(result, output)
    text = output.getvalue()
    assert text.count('Reference machine') == 1
    assert f'{result.reference_machine.name} ({result.reference_machine.short_name})' in text
    assert 'Pi equivalent' in text
    for nested_metadata in ('reference_machine', 'reference_run', 'ReferenceMachine(', 'ReferenceRun(',
                            'started_at_unix_seconds', 'start_temperature_c', 'warmup_seconds=1248'):
        assert nested_metadata not in text


def test_redirected_result_is_flushed_before_the_next_benchmark_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    class Output(io.StringIO):
        published = ''

        def flush(self) -> None:
            self.published = self.getvalue()

    output = Output()
    monkeypatch.setattr(cli.sys, 'stdout', output)

    def execute(_self: BenchmarkRunner, name: str, **_kwargs: object) -> BenchmarkResult:
        if name == 'multi_core':
            assert 'single_core' in output.published
            assert 'Pi equivalent' in output.published
        return BenchmarkResult(name, status='completed', score=1, pi_equivalent=1)

    monkeypatch.setattr(BenchmarkRunner, 'run', execute)
    assert cli.main(['--warmup=0', '--duration=.03']) == 0
    assert 'multi_core' in output.published


@pytest.mark.parametrize('columns', [1, 8, 20, 40, 80, 120])
def test_terminal_progress_fits_and_erases_previous_longer_phases(
    monkeypatch: pytest.MonkeyPatch, columns: int,
) -> None:
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    terminal = Terminal()
    monkeypatch.setattr(cli.sys, 'stdout', terminal)
    monkeypatch.setattr(cli.shutil, 'get_terminal_size', lambda: os.terminal_size((columns, 24)))

    def execute(_self: BenchmarkRunner, benchmark: str, *,
                on_progress: Callable[[Progress], None], **_kwargs: object) -> BenchmarkResult:
        for phase, fraction in [('warmup', 0), ('measurement', 0.5), ('completed', 1)]:
            on_progress(Progress(benchmark, phase, 1, fraction, 0, 1, 8))
        return BenchmarkResult(benchmark, status='completed', score=1, pi_equivalent=1)

    monkeypatch.setattr(BenchmarkRunner, 'run', execute)
    assert cli.main(['--bench', 'single_core']) == 0
    progress, result = terminal.getvalue().split('Benchmark', 1)
    fragments = [part for part in progress.split('\r') if part]
    assert len(fragments) == 4
    assert all(len(part) <= max(1, columns - 1) for part in fragments)
    assert fragments[-1].isspace()
    if columns >= 20:
        assert 'measurement' in progress
        assert '100.0%' in progress
    assert 'Pi equivalent' in result


def test_terminal_shrink_before_progress_cleanup_does_not_wrap(monkeypatch: pytest.MonkeyPatch) -> None:
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    terminal = Terminal()
    columns = 80
    monkeypatch.setattr(cli.sys, 'stdout', terminal)
    monkeypatch.setattr(cli.shutil, 'get_terminal_size', lambda: os.terminal_size((columns, 24)))

    def execute(_self: BenchmarkRunner, benchmark: str, *,
                on_progress: Callable[[Progress], None], **_kwargs: object) -> BenchmarkResult:
        nonlocal columns
        on_progress(Progress(benchmark, 'completed', 1, 1, 1, 1, 8))
        columns = 10  # A resize can happen after the final callback, before cleanup.
        return BenchmarkResult(benchmark, status='completed', score=1, pi_equivalent=1)

    monkeypatch.setattr(BenchmarkRunner, 'run', execute)
    assert cli.main(['--bench=single_core']) == 0
    progress = terminal.getvalue().split('Benchmark', 1)[0]
    frames = [part for part in progress.split('\r') if part]
    assert frames[-1].isspace()
    assert len(frames[-1]) <= columns - 1


def test_persistent_terminal_write_failure_is_reported_without_escaping(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    class DisconnectedTerminal(io.StringIO):
        writes = 0

        def isatty(self) -> bool:
            return True

        def write(self, text: str) -> int:
            self.writes += 1
            if self.writes > 1:
                raise OSError('terminal disconnected')
            return super().write(text)

    terminal = DisconnectedTerminal()
    monkeypatch.setattr(cli.sys, 'stdout', terminal)
    assert cli.main(['--bench=single_core', '--warmup=0', '--duration=.03']) == 1
    assert terminal.writes > 1
    assert 'terminal disconnected' in capsys.readouterr().err


@pytest.mark.parametrize('before_first_frame', [False, True])
def test_progress_reports_a_terminal_disconnect_even_when_isatty_becomes_false(
    monkeypatch: pytest.MonkeyPatch, before_first_frame: bool,
) -> None:
    class Terminal(io.StringIO):
        connected = True

        def isatty(self) -> bool:
            return self.connected

        def write(self, text: str) -> int:
            if not self.connected:
                raise OSError('terminal disconnected')
            return super().write(text)

    terminal = Terminal()
    monkeypatch.setattr(cli.sys, 'stdout', terminal)
    progress = cli._Progress()
    frame = Progress('single_core', 'measurement', 1, 0.5, 1, 2, 8)
    if not before_first_frame:
        progress(frame)
    terminal.connected = False
    with pytest.raises(OSError, match='terminal disconnected'):
        progress(frame)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX PTY disconnection')
def test_real_terminal_disconnect_has_execution_failure_exit_without_traceback(
    invocation: Tuple[List[str], Dict[str, str]],
) -> None:
    import pty  # noqa: PLC0415
    import select  # noqa: PLC0415
    import time  # noqa: PLC0415

    command, environment = invocation
    master, slave = pty.openpty()
    process = subprocess.Popen([*command, '--bench=single_core', '--warmup=0', '--duration=20'],
                               env=environment, stdout=slave, stderr=subprocess.PIPE, encoding='utf-8')
    os.close(slave)
    try:
        observed = b''
        deadline = time.monotonic() + 10
        while b'\rsingle_core: measurement' not in observed and time.monotonic() < deadline:
            readable, _writeable, _errors = select.select([master], [], [], 0.1)
            if readable:
                observed += os.read(master, 4096)
        assert b'\rsingle_core: measurement' in observed
        os.close(master)
        master = -1
        _output, errors = process.communicate(timeout=5)
        assert process.returncode == 1, errors
        assert errors
        assert 'Traceback' not in errors
        assert 'Exception ignored' not in errors
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if master >= 0:
            os.close(master)


def test_interrupt_during_final_render_is_quiet_and_restores_signal_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = signal.getsignal(signal.SIGINT)
    monkeypatch.setattr(BenchmarkRunner, 'run', lambda _self, name, **_kwargs:
                        BenchmarkResult(name, status='completed', score=1))

    def interrupt(_result: BenchmarkResult, _stream: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, 'render', interrupt)
    assert cli.main(['--bench', 'single_core']) == 130
    assert signal.getsignal(signal.SIGINT) is previous


@pytest.mark.parametrize('destination', ['stdout', 'stderr', 'merged'])
def test_closed_output_pipe_exits_without_traceback_or_shutdown_warning(
    invocation: Tuple[List[str], Dict[str, str]], destination: str,
) -> None:
    command, environment = invocation
    reader, writer = os.pipe()
    os.close(reader)
    try:
        option = '--help' if destination == 'stdout' else '--unknown'
        result = subprocess.run([*command, option], env=environment,
                                stdout=writer if destination != 'stderr' else subprocess.PIPE,
                                stderr=writer if destination == 'stderr' else (
                                    subprocess.STDOUT if destination == 'merged' else subprocess.PIPE
                                ), check=False, timeout=10)
    finally:
        os.close(writer)
    assert result.returncode == 1
    assert not result.stdout
    assert not result.stderr


@pytest.mark.skipif(os.name == 'nt', reason='POSIX file descriptor inheritance')
@pytest.mark.parametrize(('closed', 'arguments', 'status'), [
    ((1,), ['--version'], 1), ((1, 2), ['--version'], 1),
    ((2,), ['--version'], 0), ((2,), ['--help'], 0),
    ((2,), ['--bench=single_core', '--warmup=0', '--duration=.03'], 0),
    ((2,), ['--duration=-1'], 2), ((2,), ['--duration=1e-12'], 1),
])
def test_closed_standard_descriptors_do_not_crash(
    invocation: Tuple[List[str], Dict[str, str]], closed: Tuple[int, ...],
    arguments: List[str], status: int,
) -> None:
    command, environment = invocation

    def close_descriptors() -> None:
        for descriptor in closed:
            os.close(descriptor)

    completed_fixture = closed == (2,) and arguments[0].startswith('--bench=')
    if completed_fixture:
        # Exercise the successful stdout path with Python's real missing stderr,
        # independently of whether a short native window gets scheduled work.
        program = '''
import runpy
import sys
from benchset import BenchmarkResult, BenchmarkRunner
assert sys.stderr is None
def completed(_self, name, *, warmup_seconds, measurement_seconds, **_options):
    return BenchmarkResult(name, status='completed', score=2000, pi_equivalent=1,
                           warmup_seconds=warmup_seconds, measurement_seconds=measurement_seconds)
BenchmarkRunner.run = completed
entrypoint = sys.argv[1]
sys.argv = ['benchset', *sys.argv[2:]]
if entrypoint == 'module':
    runpy.run_module('benchset', run_name='__main__', alter_sys=True)
else:
    runpy.run_path(entrypoint, run_name='__main__')
'''
        command = [sys.executable, '-c', program, 'module' if len(command) > 1 else command[0]]
    result = subprocess.run([*command, *arguments], env=environment, capture_output=True,
                            encoding='utf-8', preexec_fn=close_descriptors, check=False, timeout=10)
    assert result.returncode == status, result.stderr
    assert 'Traceback' not in result.stderr
    assert 'Exception ignored' not in result.stderr
    if 1 in closed and 2 not in closed:
        assert 'standard output' in result.stderr
    if closed == (2,) and arguments == ['--version']:
        assert result.stdout == __version__ + '\n'
    if completed_fixture:
        assert 'single_core' in result.stdout
        assert 'completed' in result.stdout
        assert '1 \u00d7 pi' in result.stdout
        assert not result.stderr


@pytest.mark.skipif(os.name == 'nt', reason='POSIX file size limit')
@pytest.mark.parametrize('destination', ['stdout', 'stderr', 'merged'])
def test_file_output_failure_cannot_crash_error_reporting(
    tmp_path: Path, destination: str,
) -> None:
    environment = dict(os.environ)
    # The subprocess cannot write coverage files under this limit, either.
    environment.pop('COVERAGE_PROCESS_START', None)
    environment.pop('COVERAGE_FILE', None)

    # Import first: dependency initialization may itself need temporary files.
    # The failure under test is writing results/diagnostics after startup.
    code = '''
import resource
import signal
import sys
from benchset.cli import main
resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
raise SystemExit(main(sys.argv[1:]))
'''

    with (tmp_path / 'full-output').open('wb') as sink:
        result = subprocess.run([sys.executable, '-c', code,
                                 '--unknown' if destination == 'stderr' else '--version'],
                                env=environment, stdout=sink if destination != 'stderr' else subprocess.PIPE,
                                stderr=sink if destination != 'stdout' else subprocess.PIPE,
                                text=True, check=False, timeout=10)
    assert result.returncode == 1
    assert 'Traceback' not in (result.stderr or '')
    assert 'Exception ignored' not in (result.stderr or '')


def test_error_report_write_failure_does_not_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    class Unavailable(io.StringIO):
        def write(self, _text: str) -> int:
            raise OSError('destination is unavailable')

    def fail(_argv: Optional[List[str]]) -> int:
        raise OSError('output failed')

    monkeypatch.setattr(cli, '_main', fail)
    monkeypatch.setattr(cli.sys, 'stderr', Unavailable())
    assert cli.main([]) == 1


@pytest.mark.skipif(os.name == 'nt', reason='POSIX terminal signals; Windows cancellation has direct tests')
@pytest.mark.parametrize('phase', ['warmup', 'measurement'])
def test_real_ctrl_c_cancels_each_phase_and_stops_the_suite(
    invocation: Tuple[List[str], Dict[str, str]], phase: str,
) -> None:
    import pty  # noqa: PLC0415
    import select  # noqa: PLC0415
    import time  # noqa: PLC0415

    command, environment = invocation
    master, slave = pty.openpty()
    process = subprocess.Popen([*command, '--warmup', '20' if phase == 'warmup' else '0',
                                '--duration', '20'], env=environment, stdout=slave,
                               stderr=subprocess.PIPE, encoding='utf-8')
    os.close(slave)
    try:
        observed = b''
        deadline = time.monotonic() + 10
        while phase.encode() not in observed and time.monotonic() < deadline:
            readable, _writeable, _errors = select.select([master], [], [], 0.1)
            if readable:
                observed += os.read(master, 4096)
        assert phase.encode() in observed
        process.send_signal(signal.SIGINT)
        _output, errors = process.communicate(timeout=5)
        assert process.returncode == 130
        assert 'cancelled' in errors
        assert 'Traceback' not in errors
        assert 'multi_core' not in errors
        assert errors.count('Pi equivalent') == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
