"""Keep argument diagnostics ahead of terminal-query errors in the CLI."""

import io
import signal
from threading import enumerate as threads
from typing import Any, List, Tuple, Type

import pytest

from benchset import BenchmarkRunner, cli
from benchset._native import Native
from tests.helpers import assert_runner_reusable

_ERRORS = (OSError, ValueError, RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit)


class _Output(io.StringIO):
    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    def isatty(self) -> bool:
        raise self.error


def _audit_handles(monkeypatch: pytest.MonkeyPatch) -> Tuple[List[Tuple[int, Tuple[Any, ...]]],
                                                          List[Tuple[int, Tuple[Any, ...]]]]:
    created: List[Tuple[int, Tuple[Any, ...]]] = []
    freed: List[Tuple[int, Tuple[Any, ...]]] = []
    original_create, original_free = Native.create, Native.free

    def create(native: Native, *arguments: Any) -> int:
        handle = original_create(native, *arguments)
        created.append((handle, arguments))
        return handle

    def free(native: Native, handle: int, *arguments: Any) -> None:
        freed.append((handle, arguments))
        original_free(native, handle, *arguments)

    monkeypatch.setattr(Native, 'create', create)
    monkeypatch.setattr(Native, 'free', free)
    return created, freed


@pytest.mark.parametrize('benchmark', ['single_core', 'all'])
@pytest.mark.parametrize(('window', 'value', 'diagnostic'), [
    ('warmup', '-1', '--warmup must be finite and nonnegative'),
    ('duration', '0', '--duration must be finite and positive'),
])
@pytest.mark.parametrize('error_type', _ERRORS)
def test_invalid_cli_ranges_keep_priority_over_terminal_query_failures(  # noqa: PLR0913 — Independent range, selection, and stream-failure axes.
    monkeypatch: pytest.MonkeyPatch, benchmark: str, window: str, value: str,
    diagnostic: str, error_type: Type[BaseException],
) -> None:
    """A bad duration must retain argparse exit 2 when Rust is available.

    The original CLI rejected these ranges before asking stdout whether it was
    a terminal. Moving Rust validation into the worker must not let an ordinary
    terminal-query failure or a stream-raised control exception replace that
    diagnostic. Native is real; handle auditing and runner reuse check that the
    rejection leaves neither an active worker nor a partially initialized run.
    """
    runner = BenchmarkRunner()
    created, freed = _audit_handles(monkeypatch)
    output, errors = _Output(error_type('terminal query failed')), io.StringIO()
    monkeypatch.setattr(cli, 'BenchmarkRunner', lambda: runner)
    previous = signal.getsignal(signal.SIGINT)
    arguments = ['--bench=' + benchmark, '--warmup=0', '--duration=.01']
    arguments[1 if window == 'warmup' else 2] = '--' + window + '=' + value
    with monkeypatch.context() as streams:
        streams.setattr(cli.sys, 'stdout', output)
        streams.setattr(cli.sys, 'stderr', errors)
        assert cli.main(arguments) == 2
    assert not output.getvalue()
    assert errors.getvalue().startswith('usage: benchset ')
    assert errors.getvalue().splitlines()[-1] == 'benchset: error: ' + diagnostic
    assert 'terminal query failed' not in errors.getvalue()
    assert 'Traceback' not in errors.getvalue()
    assert signal.getsignal(signal.SIGINT) is previous
    assert not any(worker.name == 'benchset-cli' for worker in threads())
    assert not runner._active
    assert runner._native is None
    assert runner._handle is None
    assert created == freed == []
    assert_runner_reusable(runner)
    assert created == freed


@pytest.mark.parametrize('benchmark', ['single_core', 'all'])
@pytest.mark.parametrize('error_type', _ERRORS)
def test_valid_cli_ranges_preserve_the_original_terminal_query_error_boundary(
    monkeypatch: pytest.MonkeyPatch, benchmark: str, error_type: Type[BaseException],
) -> None:
    """Validation fallback must not swallow a real stream error for valid inputs.

    OSError remains a CLI diagnostic with exit 1; KeyboardInterrupt remains exit
    130. Other ordinary/control exceptions retain their exact original object.
    No benchmark may start just to decide which error should be reported.
    """
    runner = BenchmarkRunner()
    created, freed = _audit_handles(monkeypatch)
    original = error_type('terminal query failed')
    output, errors = _Output(original), io.StringIO()
    monkeypatch.setattr(cli, 'BenchmarkRunner', lambda: runner)
    previous = signal.getsignal(signal.SIGINT)
    with monkeypatch.context() as streams:
        streams.setattr(cli.sys, 'stdout', output)
        streams.setattr(cli.sys, 'stderr', errors)
        arguments = ['--bench=' + benchmark, '--warmup=0', '--duration=.01']
        if error_type is OSError:
            assert cli.main(arguments) == 1
        elif error_type is KeyboardInterrupt:
            assert cli.main(arguments) == 130
        else:
            with pytest.raises(error_type, match='terminal query failed') as caught:
                cli.main(arguments)
            assert caught.value is original
    assert not output.getvalue()
    assert errors.getvalue() == ('benchset: error: terminal query failed\n' if error_type is OSError else '')
    assert signal.getsignal(signal.SIGINT) is previous
    assert not any(worker.name == 'benchset-cli' for worker in threads())
    assert not runner._active
    assert runner._native is None
    assert runner._handle is None
    assert created == freed == []
    assert_runner_reusable(runner)
    assert created == freed
