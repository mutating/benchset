"""CLI settings are read by skelet, without environment/config sources."""

import math
import sys
from threading import Event, Thread
from typing import List, Optional, TextIO

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


def settings(argv: List[str]) -> Settings:
    source: FixedCLISource[object] = FixedCLISource(named_arguments=['bench', 'warmup', 'duration'])
    source.parser.prog = 'benchset'
    source.parser.description = 'Sustained scalar CPU throughput (iterations/s).'
    source.parser.add_argument('-h', '--help', action='help')
    source.parser.add_argument('--version', action='version', version=__version__)
    # Validate syntax once: FixedCLISource intentionally masks argparse failures.
    parsed = source.parser.parse_args(argv)
    for key in ('bench', 'warmup', 'duration'):
        if getattr(parsed, key) is None:
            source.parser.error('--' + key + ' requires a value')
    previous = sys.argv
    try:
        sys.argv = ['benchset', *argv]
        result = Settings(_sources=[source])
        if result.bench not in ('all', *BENCHMARKS):
            source.parser.error('--bench must be single_core or multi_core')
        if not math.isfinite(result.warmup) or result.warmup < 0:
            source.parser.error('--warmup must be finite and nonnegative')
        if not math.isfinite(result.duration) or result.duration <= 0:
            source.parser.error('--duration must be finite and positive')
        return result
    except (TypeError, ValueError) as error:
        source.parser.error(str(error))
    finally:
        sys.argv = previous


def render(result: BenchmarkResult, stream: TextIO) -> None:
    values = [
        ('Benchmark', result.benchmark),
        ('Version', result.version),
        ('Status', result.status),
        ('Success', str(result.success)),
        ('Reason', result.reason or '—'),
        ('Score', '—' if result.score is None else f'{result.score:,.3f}'),
        ('Unit', result.unit),
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
    stream.write(
        '\n'.join(f'{name:<{width}} : {value}' for name, value in values) + '\n',
    )
    stream.write('Samples (elapsed s | duration s | iterations | iterations/s):\n')
    for sample in result.samples:
        stream.write(
            f'  {sample.elapsed_seconds:10.6f} | {sample.duration_seconds:10.6f} | '
            f'{sample.iterations:10d} | {sample.iterations_per_second:,.3f}\n',
        )
    if not result.samples:
        stream.write('  —\n')


def main(argv: Optional[List[str]] = None) -> int:
    try:
        config = settings(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exit_status:
        return int(exit_status.code or 0)
    names = BENCHMARKS if config.bench == 'all' else (config.bench,)
    for name in names:
        runner = BenchmarkRunner()
        results: List[BenchmarkResult] = []
        done = Event()

        def progress(value: Progress) -> None:
            if sys.stdout.isatty():
                sys.stdout.write(
                    f'\r{value.benchmark}: {value.phase} {value.fraction * 100:5.1f}%',
                )
                sys.stdout.flush()

        def execute(
            current: str = name,
            current_runner: BenchmarkRunner = runner,
            output: List[BenchmarkResult] = results,
            completion: Event = done,
        ) -> None:
            try:
                output.append(
                    current_runner.run(
                        current,
                        warmup_seconds=config.warmup,
                        measurement_seconds=config.duration,
                        on_progress=progress,
                    ),
                )
            except Exception as error:  # noqa: BLE001 — return background failures to the console.
                output.append(BenchmarkResult(benchmark=current, reason=str(error)))
            finally:
                completion.set()

        worker = Thread(target=execute, name='benchset-cli')
        worker.start()
        interrupted = False
        while not done.is_set():
            try:
                done.wait(0.05)
            except KeyboardInterrupt:
                interrupted = True
                runner.cancel()
            if interrupted:
                runner.cancel()
        worker.join()
        if sys.stdout.isatty():
            sys.stdout.write('\r' + ' ' * 80 + '\r')
        result = results[0]
        render(result, sys.stdout if result.success and not interrupted else sys.stderr)
        if interrupted or result.status == 'cancelled':
            return 130
        if not result.success:
            return 1
        if name != names[-1]:
            sys.stdout.write('\n')
    return 0
