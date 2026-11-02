"""Restore the CLI entry signal policy after changes during native work."""

import signal
import sys
from threading import Event, Thread
from types import FrameType
from typing import Any, Callable, List, Optional, TextIO, Tuple, Union

import pytest

from benchset import BenchmarkResult, BenchmarkRunner, Progress, cli
from tests.helpers import assert_runner_reusable

_Handler = Union[int, Callable[[int, Optional[FrameType]], None], None]
_POLICIES = ('default', 'custom', 'ignored')


def _handler(policy: str, custom: Callable[[int, Optional[FrameType]], None]) -> _Handler:
    return signal.default_int_handler if policy == 'default' else signal.SIG_IGN if policy == 'ignored' else custom


@pytest.mark.skipif(sys.platform == 'win32', reason='SIGUSR1 coordinates caller policy changes on the main thread')
@pytest.mark.parametrize(('benchmark', 'changed_child'), [
    ('single_core', 'single_core'), ('all', 'single_core'), ('all', 'multi_core'),
])
@pytest.mark.parametrize('initial', _POLICIES)
@pytest.mark.parametrize('selected', _POLICIES)
@pytest.mark.parametrize('caller_error', [False, True])
def test_cli_restores_its_entry_policy_after_a_caller_changes_sigint_during_native_work(  # noqa: PLR0913, PLR0915 — One owner spans policy changes, cleanup and reuse.
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    benchmark: str, changed_child: str, initial: str, selected: str, caller_error: bool,
) -> None:
    """Native-work changes do not become output-phase caller policy selections.

    A real progress callback raises SIGUSR1 and waits until its main-thread
    handler selects another SIGINT policy. No SIGINT or output callback changes
    that policy. HEAD restored its entry handler before rendering each result;
    the shared-suite cleanup must retain that scope while preserving the
    separate, approved policy changes made during intermediate output. A caller
    error after the selection also exercises cleanup while an error unwinds.
    """
    runner = BenchmarkRunner()
    changed = Event()
    sent = False
    signals: List[str] = []
    rendering: List[Tuple[str, _Handler]] = []
    workers: List[Thread] = []
    original = RuntimeError('caller control signal failed')

    def entry_custom(_signum: int, _frame: Optional[FrameType]) -> None:
        signals.append('entry')

    def selected_custom(_signum: int, _frame: Optional[FrameType]) -> None:
        signals.append('selected')

    entry_handler = _handler(initial, entry_custom)
    selected_handler = _handler(selected, selected_custom)

    def select_policy(_signum: int, _frame: Optional[FrameType]) -> None:
        signal.signal(signal.SIGINT, selected_handler)
        changed.set()
        if caller_error:
            raise original

    class Observer(cli._Progress):
        def __call__(self, value: Progress) -> None:
            nonlocal sent
            if value.benchmark == changed_child and not sent:
                sent = True
                signal.raise_signal(signal.SIGUSR1)
                assert changed.wait(5), 'main thread did not select the caller policy'
            super().__call__(value)

    class Worker(Thread):
        def __init__(self, *arguments: Any, **options: Any) -> None:
            super().__init__(*arguments, **options)
            workers.append(self)

    render = cli.render

    def observe_render(result: BenchmarkResult, stream: TextIO) -> None:
        rendering.append((result.benchmark, signal.getsignal(signal.SIGINT)))
        render(result, stream)

    monkeypatch.setattr(cli, 'BenchmarkRunner', lambda: runner)
    monkeypatch.setattr(cli, '_Progress', Observer)
    monkeypatch.setattr(cli, 'Thread', Worker)
    monkeypatch.setattr(cli, 'render', observe_render)
    previous = signal.signal(signal.SIGINT, entry_handler)
    previous_usr = signal.signal(signal.SIGUSR1, select_policy)
    try:
        arguments = ['--bench=' + benchmark, '--warmup=.01', '--duration=.1']
        if caller_error:
            with pytest.raises(RuntimeError, match='caller control signal failed') as caught:
                cli.main(arguments)
            assert caught.value is original
        else:
            assert cli.main(arguments) == 0
        output, errors = capsys.readouterr()
        assert not errors
        assert changed.is_set()
        assert sent
        assert not signals
        assert signal.getsignal(signal.SIGINT) is entry_handler
        expected = ['single_core', 'multi_core'] if benchmark == 'all' else ['single_core']
        if caller_error:
            expected = ['single_core'] if changed_child == 'multi_core' else []
        assert [name for name, _handler in rendering] == expected
        for index, (_name, actual) in enumerate(rendering):
            if initial == 'default' and benchmark == 'all' and index == 0:
                # The temporary default-output guard is an approved CLI policy.
                assert actual is signal.default_int_handler or getattr(actual, '__name__', '') == 'interrupt_output'
            else:
                assert actual is entry_handler
        assert output.count('Status                     : completed') == len(rendering)
        if not caller_error:
            assert_runner_reusable(runner)
    finally:
        # Old CLI could return a non-SIGINT error while its worker was finishing.
        # This policy test joins it explicitly rather than pinning that old bug.
        runner.cancel()
        for worker in workers:
            if worker.ident is not None:
                worker.join(timeout=5)
                assert not worker.is_alive()
        signal.signal(signal.SIGUSR1, previous_usr)
        signal.signal(signal.SIGINT, previous)
    if caller_error:
        assert_runner_reusable(runner)
