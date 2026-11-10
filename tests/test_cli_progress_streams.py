"""Keep terminal presentation local to each CLI child while streaming one suite."""

import io
import os
from threading import enumerate as threads
from typing import Callable, List, Optional, TextIO, Tuple, Type

import pytest

from benchset import BenchmarkResult, BenchmarkRunner, Progress, cli
from benchset._native import Native
from tests.helpers import assert_runner_reusable


class _Output(io.StringIO):
    def __init__(self, terminal: bool) -> None:
        super().__init__()
        self.terminal = terminal
        self.writes: List[str] = []
        self.boundary = 'write'
        self.on_result: Optional[Callable[[], None]] = None
        self.first_result = False

    def isatty(self) -> bool:
        return self.terminal

    def _select(self) -> None:
        operation, self.on_result = self.on_result, None
        if operation is not None:
            operation()

    def write(self, text: str) -> int:
        self.writes.append(text)
        if text.startswith('Benchmark') and 'single_core' in text:
            self.first_result = True
            if self.boundary == 'write':
                self._select()
        if self.first_result and self.boundary == 'separator' and text == '\n':
            self._select()
        return super().write(text)

    def flush(self) -> None:
        if self.first_result and self.boundary == 'flush':
            self._select()
        super().flush()


@pytest.mark.parametrize('initial_terminal', [False, True])
@pytest.mark.parametrize('replace_stream', [False, True])
@pytest.mark.parametrize('boundary', ['write', 'flush', 'separator'])
@pytest.mark.parametrize('warmup', [0.0, 0.1])
def test_cli_uses_the_next_childs_current_output_stream_for_terminal_presentation(
    monkeypatch: pytest.MonkeyPatch, initial_terminal: bool, replace_stream: bool, boundary: str, warmup: float,
) -> None:
    """Recapture terminal status after result write/flush and separator output.

    Real native execution keeps the first child paused at its observer while
    the caller replaces stdout or changes that stream's isatty result. The next
    child must emit progress only for its own terminal, and a text destination
    must receive neither progress frames nor an earlier child's erase sequence.
    Inspect policy at the first callback, regardless of its phase: zero warmup
    or a descheduled coordinator can legitimately start with measurement.
    Leave enough measurement time for a delayed worker to reach both children.
    """
    initial = _Output(initial_terminal)
    destination = _Output(not initial_terminal) if replace_stream else initial
    initial.boundary = boundary
    selected = False
    first_callback: List[Tuple[Progress, bool, bool, int]] = []
    present = cli._Progress.__call__

    def observe(self: cli._Progress, value: Progress) -> None:
        if value.benchmark == 'multi_core' and not first_callback:
            first_callback.append((value, self.terminal, cli.sys.stdout is destination, self.width))
        present(self, value)

    def select() -> None:
        nonlocal selected
        selected = True
        if replace_stream:
            cli.sys.stdout = destination
        else:
            destination.terminal = not initial_terminal

    initial.on_result = select
    errors = io.StringIO()
    monkeypatch.setattr(cli.sys, 'stdout', initial)
    monkeypatch.setattr(cli.sys, 'stderr', errors)
    monkeypatch.setattr(cli.shutil, 'get_terminal_size', lambda *_args, **_kwargs: os.terminal_size((120, 24)))
    monkeypatch.setattr(cli._Progress, '__call__', observe)
    assert cli.main([f'--warmup={warmup}', '--duration=.5']) == 0, errors.getvalue()
    assert selected
    assert not errors.getvalue()
    value, terminal, current_stream, width = first_callback[0]
    assert terminal is not initial_terminal
    assert current_stream
    assert width == 0
    text = destination.getvalue()
    if not replace_stream:
        text = text[text.index('Benchmark'):]
    assert 'multi_core' in text
    if initial_terminal:
        assert '\r' not in text
    else:
        assert '\rmulti_core: ' in text
        first = next(frame for frame in destination.writes if frame.startswith('\rmulti_core: '))
        assert f': {value.phase} ' in first
    assert not any(thread.name == 'benchset-cli' for thread in threads())
    assert_runner_reusable(BenchmarkRunner())


def test_cli_next_child_has_no_previous_child_erase_width(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh child's first frame must not pad using its predecessor's width.

    Observe width before rendering the next child's first callback, even if
    scheduling skips its warmup. This catches stale state when measurement's
    wider label would otherwise conceal the unwanted erase width.
    """
    terminal = _Output(True)
    monkeypatch.setattr(cli.sys, 'stdout', terminal)
    monkeypatch.setattr(cli.sys, 'stderr', io.StringIO())
    monkeypatch.setattr(cli.shutil, 'get_terminal_size', lambda *_args, **_kwargs: os.terminal_size((120, 24)))
    widths: List[int] = []
    present = cli._Progress.__call__

    def observe(self: cli._Progress, value: Progress) -> None:
        if value.benchmark == 'multi_core' and not widths:
            widths.append(self.width)
        present(self, value)

    monkeypatch.setattr(cli._Progress, '__call__', observe)
    assert cli.main(['--warmup=0', '--duration=.2']) == 0
    assert widths == [0]
    first = next(frame for frame in terminal.writes if frame.startswith('\rmulti_core: '))
    assert first.endswith('%'), 'a new child must not erase a previous child frame'


def test_native_child_keeps_reporting_a_disconnect_after_isatty_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only child boundaries recapture isatty; a disconnected child must still fail.

    After the first real progress write, the terminal reports False from isatty
    and rejects subsequent progress writes. Rechecking on every callback would
    hide that I/O failure and let a disconnected benchmark report success.
    """
    class Terminal(_Output):
        progress_attempts = 0

        def write(self, text: str) -> int:
            if text.startswith('\r') and not text.strip('\r').isspace():
                self.progress_attempts += 1
                if not self.terminal:
                    raise OSError('terminal disconnected after first progress')
                self.terminal = False
            return super().write(text)

    terminal, errors = Terminal(True), io.StringIO()
    monkeypatch.setattr(cli.sys, 'stdout', terminal)
    monkeypatch.setattr(cli.sys, 'stderr', errors)
    assert cli.main(['--bench=single_core', '--warmup=.1', '--duration=.1']) == 1
    assert terminal.progress_attempts >= 2
    assert 'terminal disconnected after first progress' in errors.getvalue()
    assert not any(thread.name == 'benchset-cli' for thread in threads())
    assert_runner_reusable(BenchmarkRunner())


@pytest.mark.parametrize('error_type', [ValueError, OSError, KeyboardInterrupt])
def test_next_child_terminal_inspection_failure_releases_native_state(  # noqa: PLR0915 — Keep real retries, the boundary error and native cleanup in one lifetime.
    monkeypatch: pytest.MonkeyPatch, error_type: Type[BaseException],
) -> None:
    """A failing next-child isatty inspection must unblock and free the shared suite.

    The shared Rust observer waits for acknowledgement while output policy is
    inspected for the next child. Unblock it and release the native state;
    preserve ValueError identity and the existing I/O/interrupt exit codes while
    leaving no native handle or CLI worker behind. Retry only a well-formed
    first-child no-work result before that inspection; each retry must release
    its native state. The successful attempt must reach the failing check once.
    """
    original = error_type('next output cannot report its terminal status')

    class Output(_Output):
        failure_checks = 0

        def isatty(self) -> bool:
            if self.first_result:
                self.failure_checks += 1
                raise original
            return False

    handles = set()
    create, free = Native.create, Native.free
    present = cli._Progress.__call__
    benchmarks: List[str] = []
    results: List[BenchmarkResult] = []
    render = cli.render

    def record_result(result: BenchmarkResult, stream: TextIO) -> None:
        results.append(result)
        render(result, stream)

    def record_progress(self: cli._Progress, value: Progress) -> None:
        benchmarks.append(value.benchmark)
        present(self, value)

    def track_create(self: Native, suite: bool = False) -> int:
        handle = create(self, True) if suite else create(self)
        handles.add((id(self), handle, suite))
        return handle

    def track_free(self: Native, handle: int, suite: bool = False) -> None:
        free(self, handle, True) if suite else free(self, handle)
        handles.remove((id(self), handle, suite))

    monkeypatch.setattr(Native, 'create', track_create)
    monkeypatch.setattr(Native, 'free', track_free)
    monkeypatch.setattr(cli._Progress, '__call__', record_progress)
    monkeypatch.setattr(cli, 'render', record_result)
    for duration in (0.1, 0.3, 0.8):
        output, errors = Output(False), io.StringIO()
        results.clear()
        monkeypatch.setattr(cli.sys, 'stdout', output)
        monkeypatch.setattr(cli.sys, 'stderr', errors)
        caught: Optional[ValueError] = None
        try:
            status: Optional[int] = cli.main(['--warmup=.1', f'--duration={duration}'])
        except ValueError as error:
            caught, status = error, None
        assert not handles
        assert 'multi_core' not in benchmarks
        assert not any(thread.name == 'benchset-cli' for thread in threads())
        if output.failure_checks:
            assert output.failure_checks == 1
            if error_type is ValueError:
                assert caught is original
            else:
                assert caught is None
                assert status == (1 if error_type is OSError else 130)
                if error_type is OSError:
                    assert str(original) in errors.getvalue()
            break
        assert caught is None
        assert status == 1
        assert len(results) == 1
        result = results[0]
        assert result.benchmark == 'single_core'
        assert result.status == 'failed'
        assert result.reason == 'measurement window was too short to complete any work'
        assert result.iterations == 0 or result.elapsed_seconds == 0
        assert result.score is None
        assert result.pi_equivalent is None
        assert result.workers == 1
        assert result.warmup_seconds == 0.1
        assert result.measurement_seconds == duration
    else:
        pytest.fail(f'No next-child terminal inspection after three measurement windows: {results!r}')
    assert_runner_reusable(BenchmarkRunner())
    assert not handles
