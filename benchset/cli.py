"""CLI settings are read by skelet, without environment/config sources."""

import argparse
import os
import re
import shutil
import signal
import sys
from queue import Empty, Queue
from threading import Event, Event as ResultEvent, Thread, current_thread, main_thread
from types import FrameType
from typing import Dict, List, NoReturn, Optional, TextIO, Tuple

from skelet import Field, FixedCLISource, Storage

from benchset._native import Native, _raise_original
from benchset.api import BenchmarkRunner
from benchset.models import (
    BENCHMARKS,
    DEFAULT_MEASUREMENT_SECONDS,
    DEFAULT_WARMUP_SECONDS,
    AggregatedScores,
    BenchmarkResult,
    Progress,
    SuiteResult,
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


def _settings(argv: List[str]) -> Tuple[Settings, argparse.ArgumentParser]:
    source = _Arguments(named_arguments=[])
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
    return Settings(_sources=[source]), source.parser


def _duration_error(config: Settings, native: Optional[Native] = None) -> Optional[str]:
    try:
        return (Native() if native is None else native).duration_error(config.warmup, config.duration)
    except Exception:  # noqa: BLE001 — Let the worker preserve the CLI failure boundary.
        # Execution reports loader errors as ordinary failed results.
        return None


def settings(argv: List[str]) -> Settings:
    result, parser = _settings(argv)
    reason = _duration_error(result)
    if reason is not None:
        parser.error(reason)
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
    _write(stream, '\n'.join(lines) + '\n')


def render_suite(result: SuiteResult, stream: TextIO) -> None:
    equivalent = result.aggregated_scores.pi_equivalent
    value = '—' if equivalent is None else f'{equivalent:.12g} × pi'  # noqa: RUF001
    _write(stream, f'Full Pi equivalent : {value}\n')


def _write(stream: TextIO, text: str) -> None:
    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = stream.encoding
        text = text.replace('—', '-').replace('\u00d7', 'x')
        stream.write(text.encode(encoding, errors='backslashreplace').decode(encoding))


class _Progress:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Capture the current child's output policy without retaining prior frames."""
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


def _main(argv: Optional[List[str]]) -> int:  # noqa: PLR0915 — Keep signal/worker ownership and cleanup together.
    try:
        config, parser = _settings(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exit_status:
        return int(exit_status.code or 0)
    runner = BenchmarkRunner()
    done = Event()
    try:
        progress = _Progress()
    except BaseException:
        # Argument errors preceded terminal inspection before native validation.
        reason = _duration_error(config)
        if reason is None:
            raise
        try:
            parser.error(reason)
        except SystemExit as exit_status:
            return int(exit_status.code or 0)
    messages: Queue[BenchmarkResult] = Queue()
    resume = ResultEvent()
    suites: List[SuiteResult] = []
    interrupted = False
    aborted = False
    pending: Optional[BenchmarkResult] = None
    argument_errors: List[str] = []

    def validate(native: Native) -> None:
        # Load under runner ownership so startup Ctrl+C remains cancellable.
        reason = _duration_error(config, native)
        if reason is not None:
            argument_errors.append(reason)

    runner._on_native = validate

    def publish(result: BenchmarkResult) -> bool:
        if aborted or (argument_errors and not interrupted):
            return True
        # A parked observer must not consume the signal interrupting main's I/O.
        mask = getattr(signal, 'pthread_sigmask', None)
        previous_mask = mask(signal.SIG_BLOCK, {signal.SIGINT}) if mask is not None else None
        try:
            messages.put(result)
            resume.wait()
            resume.clear()
        finally:
            if mask is not None:
                mask(signal.SIG_SETMASK, previous_mask)
        return interrupted or aborted

    def execute() -> None:
        try:
            if config.bench == 'all':
                suite = runner._run_all(config.warmup, config.duration, progress, publish)
                suites.append(suite)
            else:
                result = runner.run(config.bench, warmup_seconds=config.warmup,
                                    measurement_seconds=config.duration, on_progress=progress)
                publish(result)
        except Exception as error:  # noqa: BLE001 — Bring background errors to the console.
            publish(BenchmarkResult(benchmark=config.bench if config.bench != 'all' else BENCHMARKS[0], reason=str(error)))
        finally:
            done.set()

    def interrupt(_signum: int, _frame: Optional[FrameType]) -> None:
        nonlocal interrupted
        # Defer cancellation and exception handling until outside signal hooks.
        interrupted = True

    def interrupt_output(signum: int, frame: Optional[FrameType]) -> None:
        # Break blocked output once, then defer further signals through cleanup.
        if not interrupted:
            interrupt(signum, frame)
            raise KeyboardInterrupt

    def show(result: BenchmarkResult) -> None:
        progress.clear()
        stream = sys.stdout if result.success and not interrupted else sys.stderr
        if stream is not None:
            render(result, stream)
            stream.flush()

    worker = Thread(target=execute, name='benchset-cli')
    handles_signals = current_thread() is main_thread()
    previous_handler = signal.getsignal(signal.SIGINT)
    output_handler = previous_handler

    def restore_interrupt(unwinding: bool, output: bool = False) -> None:
        nonlocal previous_handler
        if handles_signals:
            while True:
                try:
                    if output:
                        selected = signal.getsignal(signal.SIGINT)
                        if selected is not interrupt and selected is not output_handler:
                            previous_handler = selected
                    signal.signal(signal.SIGINT, interrupt)
                    return
                except BaseException:  # Caller signals cannot replace an error during cleanup.
                    if not unwinding:
                        raise

    if handles_signals:
        signal.signal(signal.SIGINT, interrupt)
    joined = False
    guard_errors: List[BaseException] = []
    try:
        worker.start()
        while not done.is_set():
            try:
                done.wait(0.05)
            except KeyboardInterrupt:
                interrupted = True
            if interrupted:
                runner.cancel()
            try:
                result = messages.get_nowait()
            except Empty:
                continue
            # Intermediate results must be flushed before native may continue;
            # final results wait for join so late interrupts retain precedence.
            if config.bench == 'all' and result.success and result.benchmark != BENCHMARKS[-1] and not interrupted:
                # Output may block; restore the caller's interrupt behavior until acknowledgement.
                output_handler = interrupt_output if previous_handler is signal.default_int_handler else previous_handler
                try:
                    if handles_signals:
                        signal.signal(signal.SIGINT, output_handler)
                    if interrupted:
                        pending = result
                    else:
                        show(result)
                        sys.stdout.write('\n')
                        progress.reset()
                except BaseException:
                    restore_interrupt(True, True)
                    raise
                else:
                    restore_interrupt(False, True)
            else:
                pending = result
            resume.set()
        worker.join()
        joined = True
    finally:
        try:
            restore_interrupt(not joined)
        except BaseException as error:  # noqa: BLE001 — Retain a new guard error until native cleanup finishes.
            guard_errors.append(error)
            restore_interrupt(True)
        try:
            if worker.ident is not None and worker.is_alive():
                aborted = True
                runner.cancel()
                resume.set()
                worker.join()
            if handles_signals:
                retry = False
                while True:
                    try:
                        if retry:
                            selected = signal.getsignal(signal.SIGINT)
                            if selected is not interrupt:
                                previous_handler = selected
                        retry = True
                        signal.signal(signal.SIGINT, previous_handler)
                        break
                    except BaseException as error:  # noqa: BLE001 — Caller signals cannot abandon handler restoration.
                        if joined and not guard_errors:
                            guard_errors.append(error)
                        if previous_handler is None and isinstance(error, TypeError):
                            break
        finally:
            if guard_errors:
                _raise_original(guard_errors.pop())  # pragma: no branch — This NoReturn call has no normal successor (Python 3.8).
    if argument_errors and not interrupted:
        try:
            parser.error(argument_errors[0])
        except SystemExit as exit_status:
            return int(exit_status.code or 0)
    if pending is None:
        if interrupted:
            return 130
        raise RuntimeError('native execution did not return a result')
    show(pending)
    exit_code = 130 if interrupted or pending.status == 'cancelled' else (0 if pending.success else 1)
    if config.bench == 'all':
        suite = suites[0] if suites and not exit_code else SuiteResult((), AggregatedScores())
        stream = sys.stdout if not exit_code else sys.stderr
        if stream is not None:
            if not exit_code:
                stream.write('\n')
            render_suite(suite, stream)
    return exit_code
