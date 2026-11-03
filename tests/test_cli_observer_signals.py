"""Keep process-directed SIGINT eligible for the CLI's blocked output thread."""

import io
import queue
import signal
import threading
from typing import Callable, List, Optional, Set

import pytest

from benchset import (
    AggregatedScores,
    BenchmarkResult,
    BenchmarkRunner,
    Progress,
    SuiteResult,
    cli,
)


@pytest.mark.parametrize('available', [True, False])
@pytest.mark.parametrize('already_blocked', [True, False])
@pytest.mark.parametrize('fail_at', ['none', 'queue', 'wait', 'clear'])
def test_cli_acknowledgement_preserves_worker_signal_mask_on_every_exit(  # noqa: PLR0915 — Keep worker-mask and fault witnesses within one lifetime.
    monkeypatch: pytest.MonkeyPatch, available: bool, already_blocked: bool, fail_at: str,
) -> None:
    """Only the parked observer blocks SIGINT, retaining its complete prior mask.

    A fake suite publishes a cancelled snapshot without timed benchmark work.
    Publication, waiting or acknowledgement clearing may raise once; the suite
    records the mask again when publication returns or raises. Emulated OS
    masks exercise both POSIX and platforms without pthread_sigmask on every
    host, including a caller that already blocked SIGINT and another signal.
    Real CLI threads and acknowledgement events retain the output/join boundary.
    """
    initial = {int(signal.SIGTERM)} | ({int(signal.SIGINT)} if already_blocked else set())
    current = initial.copy()
    parked: List[Set[int]] = []
    after: List[Set[int]] = []
    published: List[BenchmarkResult] = []
    thread_errors: List[BaseException] = []
    failed = False
    block, restore = getattr(signal, 'SIG_BLOCK', 0), getattr(signal, 'SIG_SETMASK', 2)
    monkeypatch.setattr(signal, 'SIG_BLOCK', block, raising=False)
    monkeypatch.setattr(signal, 'SIG_SETMASK', restore, raising=False)

    def mask(how: int, selected: Set[int]) -> Set[int]:
        nonlocal current
        assert threading.current_thread() is not threading.main_thread()
        previous = current.copy()
        current = current | set(selected) if how == block else set(selected)
        return previous

    if available:
        monkeypatch.setattr(signal, 'pthread_sigmask', mask, raising=False)
    else:
        monkeypatch.delattr(signal, 'pthread_sigmask', raising=False)

    def checkpoint(stage: str) -> None:
        nonlocal failed
        if fail_at == stage and not failed:
            failed = True
            raise RuntimeError('acknowledgement failed')

    class Messages(queue.Queue):
        def put(self, item: object, block: bool = True, timeout: Optional[float] = None) -> None:
            assert isinstance(item, BenchmarkResult)
            published.append(item)
            checkpoint('queue')
            super().put(item, block, timeout)

    class Acknowledgement(threading.Event):
        def wait(self, timeout: Optional[float] = None) -> bool:
            parked.append(current.copy())
            checkpoint('wait')
            return super().wait(timeout)

        def clear(self) -> None:
            checkpoint('clear')
            super().clear()

    def suite(
        _runner: BenchmarkRunner, _warmup: float, _measurement: float,
        _progress: Optional[Callable[[Progress], None]],
        on_result: Optional[Callable[[BenchmarkResult], bool]] = None,
    ) -> SuiteResult:
        result = BenchmarkResult('single_core', status='cancelled')
        assert on_result is not None
        try:
            on_result(result)
        finally:
            after.append(current.copy())
        return SuiteResult((result,), AggregatedScores())

    monkeypatch.setattr(BenchmarkRunner, '_run_all', suite)
    monkeypatch.setattr(cli, 'ResultEvent', Acknowledgement)
    monkeypatch.setattr(cli, 'Queue', Messages)
    monkeypatch.setattr(threading, 'excepthook', lambda event: thread_errors.append(event.exc_value))
    output, errors = io.StringIO(), io.StringIO()
    monkeypatch.setattr(cli.sys, 'stdout', output)
    monkeypatch.setattr(cli.sys, 'stderr', errors)
    assert cli.main(['--warmup=0', '--duration=.1']) in (1, 130)
    assert not thread_errors
    assert published[-1].reason == ('acknowledgement failed' if fail_at != 'none' else None)
    assert not output.getvalue()
    assert len(parked) == (2 if fail_at in ('wait', 'clear') else 1)
    expected = initial | {int(signal.SIGINT)} if available else initial
    assert all(value == expected for value in parked)
    assert after == [initial]
    assert current == initial
    assert not any(worker.name == 'benchset-cli' for worker in threading.enumerate())
