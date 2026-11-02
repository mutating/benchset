"""Preserve caller-selected SIGINT policies across streamed CLI suite output."""

import io
import signal
from threading import enumerate as threads
from types import FrameType
from typing import Callable, List, Optional, Union

import pytest

from benchset import BenchmarkResult, BenchmarkRunner, cli
from benchset._native import Native
from tests.helpers import assert_runner_reusable, mock_suite

_Handler = Union[int, Callable[[int, Optional[FrameType]], None], None]


def _policy(name: str, custom: Callable[[int, Optional[FrameType]], None]) -> _Handler:
    if name == 'custom':
        return custom
    return signal.SIG_IGN if name == 'ignored' else signal.default_int_handler


class _Output(io.StringIO):
    def __init__(self, handler: _Handler, boundary: str, error: Optional[Exception] = None) -> None:
        super().__init__()
        self.handler, self.boundary, self.error = handler, boundary, error
        self.first_result = self.selected = False

    def _select(self) -> None:
        self.selected = True
        signal.signal(signal.SIGINT, self.handler)
        if self.error is not None:
            raise self.error

    def write(self, value: str) -> int:
        if value.startswith('Benchmark') and 'single_core' in value:
            self.first_result = True
            if self.boundary == 'write':
                self._select()
        return super().write(value)

    def flush(self) -> None:
        if self.first_result and not self.selected and self.boundary == 'flush':
            self._select()
        super().flush()


@pytest.mark.parametrize('initial', ['default', 'custom', 'ignored'])
@pytest.mark.parametrize('selected', ['default', 'custom', 'ignored'])
@pytest.mark.parametrize('boundary', ['write', 'flush'])
def test_cli_retains_a_policy_selected_during_intermediate_result_output(
    monkeypatch: pytest.MonkeyPatch, initial: str, selected: str, boundary: str,
) -> None:
    """A caller may replace SIGINT while writing or flushing the first result.

    The old CLI restored its handler before rendering and captured the caller's
    new policy for the next child. Streaming through one Rust suite must retain
    that selected policy after its temporary cleanup guard has been released.
    Emit another SIGINT after return to verify the handler's actual behavior,
    including custom and ignored policies, rather than only its identity.
    """
    mock_suite(monkeypatch)
    monkeypatch.setattr(BenchmarkRunner, 'run', lambda _self, name, **_options:
                        BenchmarkResult(name, status='completed', score=1, pi_equivalent=1))
    calls: List[str] = []

    def initial_custom(_signum: int, _frame: Optional[FrameType]) -> None:
        calls.append('initial')

    def selected_custom(_signum: int, _frame: Optional[FrameType]) -> None:
        calls.append('selected')

    handler = _policy(selected, selected_custom)
    output = _Output(handler, boundary)
    monkeypatch.setattr(cli.sys, 'stdout', output)
    monkeypatch.setattr(cli.sys, 'stderr', io.StringIO())
    previous = signal.signal(signal.SIGINT, _policy(initial, initial_custom))
    try:
        assert cli.main(['--warmup=0', '--duration=.01']) == 0
        assert output.selected
        assert 'multi_core' in output.getvalue()
        assert signal.getsignal(signal.SIGINT) is handler
        if selected == 'default':
            with pytest.raises(KeyboardInterrupt):
                signal.raise_signal(signal.SIGINT)
        else:
            signal.raise_signal(signal.SIGINT)
        assert calls == (['selected'] if selected == 'custom' else [])
    finally:
        signal.signal(signal.SIGINT, previous)


@pytest.mark.parametrize('selected', ['default', 'custom', 'ignored'])
@pytest.mark.parametrize('boundary', ['write', 'flush'])
def test_cli_output_error_restores_the_selected_policy_after_native_cleanup(
    monkeypatch: pytest.MonkeyPatch, selected: str, boundary: str,
) -> None:
    """An output error must retain its exact object and the newly selected policy.

    Use real native execution and track every handle through free. The first
    intermediate output fails while the shared suite's observer is waiting for
    acknowledgement; teardown must unblock and join it before returning the
    caller's exception and restoring the caller-selected signal handler.
    """
    calls: List[int] = []

    def custom(signum: int, _frame: Optional[FrameType]) -> None:
        calls.append(signum)

    handler = _policy(selected, custom)
    original = ValueError('first result output failed')
    output = _Output(handler, boundary, original)
    monkeypatch.setattr(cli.sys, 'stdout', output)
    monkeypatch.setattr(cli.sys, 'stderr', io.StringIO())
    handles = set()
    create, free = Native.create, Native.free

    def track_create(self: Native, suite: bool = False) -> int:
        handle = create(self, suite)
        handles.add((id(self), handle, suite))
        return handle

    def track_free(self: Native, handle: int, suite: bool = False) -> None:
        free(self, handle, suite)
        handles.remove((id(self), handle, suite))

    monkeypatch.setattr(Native, 'create', track_create)
    monkeypatch.setattr(Native, 'free', track_free)
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(ValueError, match='first result output failed') as caught:
            cli.main(['--warmup=0', '--duration=.05'])
        assert caught.value is original
        assert output.selected
        assert not handles
        assert not any(thread.name == 'benchset-cli' for thread in threads())
        assert signal.getsignal(signal.SIGINT) is handler
        assert not calls
        assert_runner_reusable(BenchmarkRunner())
        assert not handles
        assert signal.getsignal(signal.SIGINT) is handler
    finally:
        signal.signal(signal.SIGINT, previous)
