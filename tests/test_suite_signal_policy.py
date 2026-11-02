"""Recheck caller SIGINT policy at each legacy benchmark execution boundary."""

import signal
from types import FrameType
from typing import Callable, Dict, List, Optional, Type, Union

import pytest

from benchset import BenchmarkRunner, Progress, SuiteResult, api, run_all
from benchset._native import Native
from tests.helpers import assert_runner_reusable

_Handler = Union[int, Callable[[int, Optional[FrameType]], None], None]
_POLICIES = ('default', 'custom', 'ignored')


def _handler(policy: str, custom: Callable[[int, Optional[FrameType]], None]) -> _Handler:
    handlers: Dict[str, _Handler] = {'default': signal.default_int_handler, 'custom': custom,
                                   'ignored': signal.SIG_IGN}
    return handlers[policy]


def _assert_execution_handler(actual: _Handler, policy: str, caller_handler: _Handler) -> None:
    if policy == 'default':
        # run() defers the default exception until cancellation and native cleanup.
        assert callable(actual)
        assert actual is not signal.default_int_handler
    else:
        assert actual is caller_handler


@pytest.mark.parametrize('initial', _POLICIES)
@pytest.mark.parametrize('target', _POLICIES)
@pytest.mark.parametrize('change_at', ['callback', 'warmup', 'measurement'])
def test_suite_rechecks_signal_policy_after_each_childs_conversions(  # noqa: PLR0915 — One scope owns signal restoration and runner reuse.
    monkeypatch: pytest.MonkeyPatch, initial: str, target: str, change_at: str,
) -> None:
    """A previous callback or either second-child converter may change policy.

    HEAD called run() separately for each child: both float conversions ran
    under the caller's current policy before run() decided whether to own
    default Ctrl+C. The shared suite must make that same decision for its next
    child. Custom and ignored policies retain their exact handler objects;
    default SIGINT cancels and escapes as KeyboardInterrupt, never as a failed
    progress callback. A fresh call on the same runner checks complete cleanup.
    The measurement window allows worker startup on loaded Windows runners;
    this test checks signal policy rather than short-window throughput.
    """
    events: List[str] = []
    observed: Dict[str, _Handler] = {}
    conversion_handlers: Dict[str, _Handler] = {}
    conversions = {'warmup': 0, 'measurement': 0}
    runner = BenchmarkRunner()
    monkeypatch.setattr(api, 'BenchmarkRunner', lambda: runner)

    def initial_custom(_signum: int, _frame: Optional[FrameType]) -> None:
        events.append('initial-custom')

    def target_custom(_signum: int, _frame: Optional[FrameType]) -> None:
        events.append('target-custom')

    initial_handler = _handler(initial, initial_custom)
    target_handler = _handler(target, target_custom)

    class Seconds(float):
        def __new__(cls, field: str) -> 'Seconds':
            return super().__new__(cls, 0.0 if field == 'warmup' else .1)

        def __init__(self, field: str) -> None:
            self.field = field

        def __float__(self) -> float:
            conversions[self.field] += 1
            ordinal = conversions[self.field]
            conversion_handlers[self.field + ':' + str(ordinal)] = signal.getsignal(signal.SIGINT)
            if ordinal == 2 and self.field == change_at:
                signal.signal(signal.SIGINT, target_handler)
            return float.__float__(self)

    signalled = False

    def progress(value: Progress) -> None:
        nonlocal signalled
        if value.benchmark == 'single_core' and value.phase == 'completed' and change_at == 'callback':
            signal.signal(signal.SIGINT, target_handler)
        if value.benchmark == 'multi_core' and value.phase in ('warmup', 'measurement') and not signalled:
            signalled = True
            observed['second'] = signal.getsignal(signal.SIGINT)
            events.append('before-signal')
            signal.raise_signal(signal.SIGINT)
            events.append('after-signal')

    previous = signal.signal(signal.SIGINT, initial_handler)
    suite: Optional[SuiteResult] = None
    interrupted = False
    try:
        try:
            suite = run_all(warmup_seconds=Seconds('warmup'), measurement_seconds=Seconds('measurement'),
                            on_progress=progress)
        except KeyboardInterrupt:
            interrupted = True
        assert signal.getsignal(signal.SIGINT) is target_handler
        assert_runner_reusable(runner)
        assert signal.getsignal(signal.SIGINT) is target_handler
    finally:
        signal.signal(signal.SIGINT, previous)

    assert conversions == {'warmup': 2, 'measurement': 2}
    assert conversion_handlers['warmup:1'] is initial_handler
    assert conversion_handlers['measurement:1'] is initial_handler
    assert conversion_handlers['warmup:2'] is (target_handler if change_at == 'callback' else initial_handler)
    assert conversion_handlers['measurement:2'] is (
        target_handler if change_at in ('callback', 'warmup') else initial_handler
    )
    assert signalled
    _assert_execution_handler(observed['second'], target, target_handler)
    assert interrupted == (target == 'default'), suite
    assert events == ['before-signal'] + (['target-custom'] if target == 'custom' else []) + ['after-signal']
    if target != 'default':
        assert suite is not None
        assert tuple(result.status for result in suite.results) == ('completed', 'completed')


@pytest.mark.parametrize('initial', _POLICIES)
@pytest.mark.parametrize('target', _POLICIES)
def test_single_run_rechecks_signal_policy_after_name_validation(initial: str, target: str) -> None:
    """A typed name operator changes policy before native execution owns signals.

    Legacy name validation preceded the SIGINT decision. In particular a custom
    or ignored initial policy changed to default must acquire deferred native
    cancellation, while a new custom/ignored policy must remain untouched.
    """
    events: List[str] = []
    observed: List[_Handler] = []
    comparison_handlers: List[_Handler] = []
    comparisons = 0
    runner = BenchmarkRunner()

    def initial_custom(_signum: int, _frame: Optional[FrameType]) -> None:
        events.append('initial-custom')

    def target_custom(_signum: int, _frame: Optional[FrameType]) -> None:
        events.append('target-custom')

    initial_handler = _handler(initial, initial_custom)
    target_handler = _handler(target, target_custom)

    class Name(str):
        __slots__ = ()
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
            nonlocal comparisons
            comparisons += 1
            comparison_handlers.append(signal.getsignal(signal.SIGINT))
            if comparisons == 1:
                signal.signal(signal.SIGINT, target_handler)
            return str.__eq__(self, other)

    signalled = False

    def progress(value: Progress) -> None:
        nonlocal signalled
        if value.phase in ('warmup', 'measurement') and not signalled:
            signalled = True
            observed.append(signal.getsignal(signal.SIGINT))
            events.append('before-signal')
            signal.raise_signal(signal.SIGINT)
            events.append('after-signal')

    previous = signal.signal(signal.SIGINT, initial_handler)
    result = None
    interrupted = False
    try:
        try:
            result = runner.run(Name('single_core'), warmup_seconds=0, measurement_seconds=.01,
                                on_progress=progress)
        except KeyboardInterrupt:
            interrupted = True
        assert signal.getsignal(signal.SIGINT) is target_handler
        assert_runner_reusable(runner)
        assert signal.getsignal(signal.SIGINT) is target_handler
    finally:
        signal.signal(signal.SIGINT, previous)

    assert comparisons == 2
    assert comparison_handlers[0] is initial_handler
    _assert_execution_handler(comparison_handlers[1], target, target_handler)
    assert signalled
    _assert_execution_handler(observed[0], target, target_handler)
    assert interrupted == (target == 'default'), result
    assert events == ['before-signal'] + (['target-custom'] if target == 'custom' else []) + ['after-signal']
    if target != 'default':
        assert result is not None
        assert result.success


@pytest.mark.parametrize('initial', ['custom', 'ignored'])
@pytest.mark.parametrize('window', ['warmup', 'measurement'])
@pytest.mark.parametrize('error_type', [RuntimeError, KeyboardInterrupt])
def test_new_default_policy_protects_cleanup_after_a_converter_raises(
    monkeypatch: pytest.MonkeyPatch, initial: str, window: str, error_type: Type[BaseException],
) -> None:
    """A converter's newly selected default policy also applies during cleanup.

    HEAD had freed child one before this conversion. A shared suite still owns
    its handle and must defer a cleanup SIGINT without replacing the caller's
    original error or leaving activity behind. The free hook always completes
    real native freeing, including when a broken signal policy raises there.
    """
    runner = BenchmarkRunner()
    monkeypatch.setattr(api, 'BenchmarkRunner', lambda: runner)
    original = error_type('second conversion failed after selecting default')
    original_free = Native.free
    conversions = {'warmup': 0, 'measurement': 0}
    raised = False
    cleanup_signals = 0

    def custom(_signum: int, _frame: Optional[FrameType]) -> None:
        pass

    class Seconds(float):
        def __new__(cls, field: str) -> 'Seconds':
            return super().__new__(cls, 0.0 if field == 'warmup' else .01)

        def __init__(self, field: str) -> None:
            self.field = field

        def __float__(self) -> float:
            nonlocal raised
            conversions[self.field] += 1
            if self.field == window and conversions[self.field] == 2:
                signal.signal(signal.SIGINT, signal.default_int_handler)
                raised = True
                raise original
            return float.__float__(self)

    def free(native: Native, handle: int, suite: bool = False) -> None:
        nonlocal cleanup_signals
        try:
            if raised:
                cleanup_signals += 1
                signal.raise_signal(signal.SIGINT)
        finally:
            original_free(native, handle, True) if suite else original_free(native, handle)

    monkeypatch.setattr(Native, 'free', free)
    previous = signal.signal(signal.SIGINT, _handler(initial, custom))
    caught: Optional[BaseException] = None
    try:
        try:
            run_all(warmup_seconds=Seconds('warmup'), measurement_seconds=Seconds('measurement'))
        except BaseException as error:  # noqa: BLE001 — Compare every possible replacement with the exact caller error.
            caught = error
        raised = False
        assert caught is original
        assert caught.__context__ is None
        assert cleanup_signals == (1 if hasattr(Native, 'execute_suite') else 0)
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
        assert_runner_reusable(runner)
    finally:
        signal.signal(signal.SIGINT, previous)
