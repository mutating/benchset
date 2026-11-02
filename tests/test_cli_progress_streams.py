"""Keep terminal presentation local to each CLI child while streaming one suite."""

import io
import os
from threading import enumerate as threads
from typing import Callable, List, Optional, Type

import pytest

from benchset import BenchmarkRunner, Progress, cli
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
def test_cli_uses_the_next_childs_current_output_stream_for_terminal_presentation(
    monkeypatch: pytest.MonkeyPatch, initial_terminal: bool, replace_stream: bool, boundary: str,
) -> None:
    """Recapture terminal status after result write/flush and separator output.

    Real native execution keeps the first child paused at its observer while
    the caller replaces stdout or changes that stream's isatty result. The next
    child must emit progress only for its own terminal, and a text destination
    must receive neither progress frames nor an earlier child's erase sequence.
    """
    initial = _Output(initial_terminal)
    destination = _Output(not initial_terminal) if replace_stream else initial
    initial.boundary = boundary
    selected = False

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
    monkeypatch.setattr(cli.shutil, 'get_terminal_size', lambda: os.terminal_size((120, 24)))
    assert cli.main(['--warmup=.01', '--duration=.03']) == 0
    assert selected
    assert not errors.getvalue()
    text = destination.getvalue()
    if not replace_stream:
        text = text[text.index('Benchmark'):]
    assert 'multi_core' in text
    if initial_terminal:
        assert '\r' not in text
    else:
        assert '\rmulti_core: ' in text
        first = next(frame for frame in destination.writes if frame.startswith('\rmulti_core: '))
        assert ': warmup ' in first, 'refresh must precede the next child\'s first frame'
    assert not any(thread.name == 'benchset-cli' for thread in threads())
    assert_runner_reusable(BenchmarkRunner())


def test_cli_next_child_has_no_previous_child_erase_width(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh child's first frame must not pad using its predecessor's width.

    A completed single-core label is wider than the next multicore warmup
    label. Real warmup produces that shorter first frame, so a retained width
    becomes observable trailing spaces rather than a timing-sensitive counter.
    """
    terminal = _Output(True)
    monkeypatch.setattr(cli.sys, 'stdout', terminal)
    monkeypatch.setattr(cli.sys, 'stderr', io.StringIO())
    monkeypatch.setattr(cli.shutil, 'get_terminal_size', lambda: os.terminal_size((120, 24)))
    assert cli.main(['--warmup=.01', '--duration=.03']) == 0
    first = next(frame for frame in terminal.writes if frame.startswith('\rmulti_core: '))
    assert ': warmup ' in first
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
    assert cli.main(['--bench=single_core', '--warmup=.01', '--duration=.03']) == 1
    assert terminal.progress_attempts >= 2
    assert 'terminal disconnected after first progress' in errors.getvalue()
    assert not any(thread.name == 'benchset-cli' for thread in threads())
    assert_runner_reusable(BenchmarkRunner())


@pytest.mark.parametrize('error_type', [ValueError, OSError, KeyboardInterrupt])
def test_next_child_terminal_inspection_failure_releases_native_state(
    monkeypatch: pytest.MonkeyPatch, error_type: Type[BaseException],
) -> None:
    """A failing next-child isatty inspection must unblock and free the shared suite.

    HEAD had already freed the first child before inspecting the next output.
    The shared Rust observer is still waiting for acknowledgement at this point;
    preserve ValueError identity and the existing I/O/interrupt exit codes while
    leaving no native handle or CLI worker behind.
    """
    original = error_type('next output cannot report its terminal status')

    class Output(_Output):
        def isatty(self) -> bool:
            if self.first_result:
                raise original
            return False

    handles = set()
    create, free = Native.create, Native.free
    present = cli._Progress.__call__
    benchmarks: List[str] = []

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
    monkeypatch.setattr(cli.sys, 'stdout', Output(False))
    monkeypatch.setattr(cli.sys, 'stderr', io.StringIO())
    if error_type is ValueError:
        with pytest.raises(ValueError, match='next output cannot report') as caught:
            cli.main(['--warmup=.01', '--duration=.03'])
        assert caught.value is original
    else:
        assert cli.main(['--warmup=.01', '--duration=.03']) == (1 if error_type is OSError else 130)
    assert not handles
    assert 'multi_core' not in benchmarks
    assert not any(thread.name == 'benchset-cli' for thread in threads())
    assert_runner_reusable(BenchmarkRunner())
    assert not handles
