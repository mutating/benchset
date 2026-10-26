"""CLI settings are read by skelet, without environment/config sources."""

import argparse
import math
import os
import re
import shutil
import signal
import sys
from threading import Event, Thread, current_thread, main_thread
from types import FrameType
from typing import Dict, List, NoReturn, Optional, TextIO

from skelet import Field, FixedCLISource, Storage

from benchset.api import BenchmarkRunner
from benchset.models import (
    BENCHMARKS,
    DEFAULT_MEASUREMENT_SECONDS,
    DEFAULT_WARMUP_SECONDS,
    BenchmarkResult,
    Progress,
    __version__,
)


class Settings(Storage):
    bench: str = Field('all')
    warmup: float = Field(DEFAULT_WARMUP_SECONDS)
    duration: float = Field(DEFAULT_MEASUREMENT_SECONDS)


class _Arguments(FixedCLISource[object]):
    _arguments: Dict[str, str]

    def __getitem__(self, key: str) -> str:
        return self._arguments[key]


def _seconds(value: str) -> str:
    try:
        float(value)
    except ValueError:
        raise argparse.ArgumentTypeError('must be a number of seconds') from None
    return value


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        if sys.stderr is None:
            raise SystemExit(2)
        super().error(message)


def settings(argv: List[str]) -> Settings:
    source = _Arguments(named_arguments=['bench', 'warmup', 'duration'])
    source.parser = _Parser(
        prog='benchset', argument_default=argparse.SUPPRESS,
        description='Sustained scalar CPU throughput and speed relative to Raspberry Pi 5.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'Higher iterations/s is faster. Pi equivalent: 0.5 = half the Pi speed; 2 = twice.\n'
            'With no options, runs single_core then multi_core (about 6 minutes total).\n'
            'Ctrl+C cancels the run.\n'
            'Exit codes: 0 success, 1 failed, 2 invalid arguments, 130 cancelled.\n\n'
            'Examples:\n'
            '  benchset --bench single_core\n'
            '  benchset --bench multi_core --warmup 120 --duration 60\n'
            '  benchset --warmup 0 --duration 1  # quick smoke test'
        ),
    )
    # argparse's default misses scientific/trailing-dot negatives. Recognize
    # numeric-looking values here; _seconds and range checks still validate them.
    source.parser._negative_number_matcher = re.compile(r'^-(?:\d|\.\d|inf(?:inity)?$|nan$)', re.IGNORECASE)
    source.parser.add_argument('--bench', choices=('all', *BENCHMARKS),
                               help='Benchmark to run (default: all).')
    source.parser.add_argument('--warmup', type=_seconds, metavar='SECONDS',
                               help=f'Warmup excluded from the score (default: {DEFAULT_WARMUP_SECONDS:g}; zero allowed).')
    source.parser.add_argument('--duration', type=_seconds, metavar='SECONDS',
                               help=f'Measurement duration, must be positive (default: {DEFAULT_MEASUREMENT_SECONDS:g}).')
    source.parser.add_argument('--version', action='version', version=__version__)
    # Bind parsed arguments locally: FixedCLISource's normal getter reparses
    # global sys.argv and temporarily redirects the process's stderr.
    source._arguments = vars(source.parser.parse_args(argv))
    result = Settings(_sources=[source])
    if not math.isfinite(result.warmup) or result.warmup < 0:
        source.parser.error('--warmup must be finite and nonnegative')
    if not math.isfinite(result.duration) or result.duration <= 0:
        source.parser.error('--duration must be finite and positive')
    return result


def render(result: BenchmarkResult, stream: TextIO) -> None:
    values = [
        ('Benchmark', result.benchmark),
        ('Version', result.version),
        ('Status', result.status),
        ('Success', str(result.success)),
        ('Reason', result.reason or '—'),
        ('Score', '—' if result.score is None else f'{result.score:,.3f}'),
        ('Unit', result.unit),
        ('Reference machine', f'{result.reference_machine.name} ({result.reference_machine.short_name})'),
        (
            'Pi equivalent',
            '—' if result.pi_equivalent is None
            else f'{result.pi_equivalent:.12g} × {result.reference_machine.short_name}',  # noqa: RUF001 — multiplication sign.
        ),
        ('Workers', str(result.workers)),
        ('Warmup seconds', f'{result.warmup_seconds:g}'),
        ('Measurement seconds', f'{result.measurement_seconds:g}'),
        ('Actual measurement seconds', f'{result.elapsed_seconds:.6f}'),
        ('Iterations', str(result.iterations)),
        ('Checksum', str(result.checksum)),
        (
            'Variation percent',
            '—'
            if result.variation_percent is None
            else f'{result.variation_percent:.3f}',
        ),
        ('Unstable', str(result.unstable)),
    ]
    width = max(len(name) for name, _value in values)
    lines = [f'{name:<{width}} : {value}' for name, value in values]
    lines.append('Samples:')
    if result.samples:
        rows = [('elapsed s', 'duration s', 'iterations', 'iterations/s')]
        rows.extend((f'{sample.elapsed_seconds:.6f}', f'{sample.duration_seconds:.6f}',
                     str(sample.iterations), f'{sample.iterations_per_second:,.3f}')
                    for sample in result.samples)
        widths = [max(len(row[index]) for row in rows) for index in range(4)]
        lines.extend('  ' + ' | '.join(cell.rjust(widths[index]) for index, cell in enumerate(row))
                     for row in rows)
    else:
        lines.append('  —')
    text = '\n'.join(lines) + '\n'
    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = stream.encoding
        text = text.replace('—', '-').replace('\u00d7', 'x')
        stream.write(text.encode(encoding, errors='backslashreplace').decode(encoding))


class _Progress:
    def __init__(self) -> None:
        self.width = 0
        # A disconnected PTY can stop reporting itself as a terminal. Keep
        # writing progress so the I/O error cancels the native run promptly.
        self.terminal = sys.stdout.isatty()

    def __call__(self, value: Progress) -> None:
        if self.terminal:
            limit = max(1, shutil.get_terminal_size().columns - 1)
            percent = f'{value.fraction * 100:5.1f}%'
            label = f'{value.benchmark}: {value.phase}'
            if len(label) + len(percent) + 1 > limit:
                label = value.phase
            text = f'{label} {percent}'[-limit:]
            sys.stdout.write('\r' + text.ljust(min(self.width, limit)))
            self.width = len(text)
            sys.stdout.flush()

    def clear(self) -> None:
        if self.width:
            width = min(self.width, max(1, shutil.get_terminal_size().columns - 1))
            sys.stdout.write('\r' + ' ' * width + '\r')


def main(argv: Optional[List[str]] = None) -> int:
    try:
        if sys.stdout is None:
            raise OSError('standard output is unavailable')
        status = _main(argv)
        sys.stdout.flush()
        if sys.stderr is not None:
            sys.stderr.flush()
        return status
    except OSError as error:
        if not isinstance(error, BrokenPipeError) and sys.stderr is not None:
            try:
                sys.stderr.write(f'benchset: error: {error}\n')
            except OSError:
                pass  # Diagnostics can fail on the same full or disconnected destination.
        # Prevent another failed flush during interpreter shutdown, including
        # disconnected terminals and stderr piped or merged into stdout.
        with open(os.devnull, 'w') as devnull:
            for stream in (sys.stdout, sys.stderr):
                if stream is None:
                    continue
                try:
                    stream.flush()
                except OSError:
                    os.dup2(devnull.fileno(), stream.fileno())
        return 1
    except KeyboardInterrupt:
        return 130


def _main(argv: Optional[List[str]]) -> int:
    try:
        config = settings(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exit_status:
        return int(exit_status.code or 0)
    names = BENCHMARKS if config.bench == 'all' else (config.bench,)
    for name in names:
        runner = BenchmarkRunner()
        results: List[BenchmarkResult] = []
        done = Event()
        progress = _Progress()

        def execute(
            current: str = name,
            current_runner: BenchmarkRunner = runner,
            current_progress: _Progress = progress,
            output: List[BenchmarkResult] = results,
            completion: Event = done,
        ) -> None:
            try:
                output.append(
                    current_runner.run(
                        current,
                        warmup_seconds=config.warmup,
                        measurement_seconds=config.duration,
                        on_progress=current_progress,
                    ),
                )
            except Exception as error:  # noqa: BLE001 — return background failures to the console.
                output.append(BenchmarkResult(benchmark=current, reason=str(error)))
            finally:
                completion.set()

        worker = Thread(target=execute, name='benchset-cli')
        interrupted = False

        def interrupt(_signum: int, _frame: Optional[FrameType]) -> None:
            nonlocal interrupted
            # Raising during Thread.start()/join() can leave the worker running.
            # Set a flag only; cancellation happens outside the signal handler.
            interrupted = True

        handles_signals = current_thread() is main_thread()
        previous_handler = signal.getsignal(signal.SIGINT)
        if handles_signals:
            signal.signal(signal.SIGINT, interrupt)
        try:
            worker.start()
            while not done.is_set():
                try:
                    done.wait(0.05)
                except KeyboardInterrupt:
                    interrupted = True
                if interrupted:
                    runner.cancel()
            worker.join()
        finally:
            if handles_signals:
                signal.signal(signal.SIGINT, previous_handler)
        progress.clear()
        result = results[0]
        stream = sys.stdout if result.success and not interrupted else sys.stderr
        if stream is not None:
            render(result, stream)
            stream.flush()
        if interrupted or result.status == 'cancelled':
            return 130
        if not result.success:
            return 1
        if name != names[-1]:
            sys.stdout.write('\n')
    return 0
