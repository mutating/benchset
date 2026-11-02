"""Characterize per-benchmark option conversion through the public suite API."""

import signal
from typing import List, Optional, Tuple, Type, Union

import pytest

from benchset import BenchmarkRunner, Progress, api, run_all


class _Seconds(float):
    """Expose when a float-compatible caller supplies each successive value."""

    def __new__(
        cls, _name: str, _values: Tuple[Union[float, BaseException], ...], _events: List[str],
    ) -> '_Seconds':
        return super().__new__(cls, 0.0)

    def __init__(
        self, name: str, values: Tuple[Union[float, BaseException], ...], events: List[str],
    ) -> None:
        self.name = name
        self.values = values
        self.events = events
        self.conversions = 0

    def __float__(self) -> float:
        self.conversions += 1
        self.events.append(f'{self.name}:{self.conversions}')
        value = self.values[self.conversions - 1]
        if isinstance(value, BaseException):
            raise value
        return value


def _record_progress(events: List[str], value: Progress) -> None:
    """Record child boundaries without depending on progress sampling frequency."""
    started = 'start:' + value.benchmark
    if started not in events:
        events.append(started)
    if value.phase in ('completed', 'failed', 'cancelled'):
        events.append(value.phase + ':' + value.benchmark)


def test_terminal_sigint_stops_before_another_option_conversion() -> None:
    """A signal in the first terminal callback must not inspect the next child's options.

    Legacy run() raised after cleanup before run_all could attempt another child.
    The native suite must stop at the same host boundary, even though its first
    stored result was already completed when the interrupt arrived.
    """
    events: List[str] = []
    warmup = _Seconds('warmup', (0.0, AssertionError('unused warmup')), events)
    measurement = _Seconds('measurement', (0.1, AssertionError('unused measurement')), events)

    def progress(value: Progress) -> None:
        _record_progress(events, value)
        if value.benchmark == 'single_core' and value.phase == 'completed':
            signal.raise_signal(signal.SIGINT)

    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_all(warmup_seconds=warmup, measurement_seconds=measurement, on_progress=progress)
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    finally:
        signal.signal(signal.SIGINT, previous)
    assert warmup.conversions == measurement.conversions == 1
    assert events == ['warmup:1', 'measurement:1', 'start:single_core', 'completed:single_core']


@pytest.mark.parametrize(('second_warmup', 'second_measurement', 'reason'), [
    (0.02, 0.12, None),
    (-1.0, 0.12, 'warmup_seconds must be finite and nonnegative'),
    (0.02, 0.0, 'measurement_seconds must be finite and positive'),
])
def test_each_child_converts_current_windows_in_warmup_then_measurement_order(
    second_warmup: float, second_measurement: float, reason: Optional[str],
) -> None:
    """Read fresh options after the prior child finishes and before the next starts.

    Both conversions precede validation, even when the new warmup is invalid.
    Distinct second values must reach the second snapshot rather than reuse
    the first child's converted configuration.
    """
    events: List[str] = []
    warmup = _Seconds('warmup', (0.0, second_warmup), events)
    measurement = _Seconds('measurement', (0.1, second_measurement), events)

    suite = run_all(warmup_seconds=warmup, measurement_seconds=measurement,
                    on_progress=lambda value: _record_progress(events, value))

    assert len(suite.results) == 2
    first, second = suite.results
    assert first.benchmark == 'single_core'
    assert first.success
    assert (first.warmup_seconds, first.measurement_seconds) == (0.0, 0.1)
    assert second.benchmark == 'multi_core'
    assert (second.warmup_seconds, second.measurement_seconds) == (second_warmup, second_measurement)
    assert second.status == ('completed' if reason is None else 'failed')
    assert second.reason == reason
    assert warmup.conversions == measurement.conversions == 2
    expected = ['warmup:1', 'measurement:1', 'start:single_core', 'completed:single_core',
                'warmup:2', 'measurement:2']
    if reason is None:
        expected.extend(['start:multi_core', 'completed:multi_core'])
    assert events == expected
    assert (suite.aggregated_scores.pi_equivalent is not None) == (reason is None)


@pytest.mark.parametrize('failure', ['warmup', 'measurement', 'callback'])
def test_first_child_failure_does_not_convert_options_for_another_child(failure: str) -> None:
    """Stop before touching second values after validation or callback failure.

    An invalid first warmup must still convert the first measurement. Raising
    second values also detects speculative conversion ahead of suite stopping.
    """
    events: List[str] = []
    warmup = _Seconds('warmup', (-1.0 if failure == 'warmup' else 0.0,
                                AssertionError('unused second warmup')), events)
    measurement = _Seconds('measurement', (0.0 if failure == 'measurement' else 0.1,
                                          AssertionError('unused second measurement')), events)

    def progress(value: Progress) -> None:
        _record_progress(events, value)
        if failure == 'callback':
            raise ValueError('first child callback failed')

    suite = run_all(warmup_seconds=warmup, measurement_seconds=measurement, on_progress=progress)

    assert len(suite.results) == 1
    assert suite.results[0].benchmark == 'single_core'
    assert suite.results[0].status == 'failed'
    assert suite.results[0].reason == {
        'warmup': 'warmup_seconds must be finite and nonnegative',
        'measurement': 'measurement_seconds must be finite and positive',
        'callback': 'progress callback failed: ValueError: first child callback failed',
    }[failure]
    assert suite.aggregated_scores.pi_equivalent is None
    assert warmup.conversions == measurement.conversions == 1
    assert events == ['warmup:1', 'measurement:1'] + (['start:single_core'] if failure == 'callback' else [])


@pytest.mark.parametrize('window', ['warmup', 'measurement'])
@pytest.mark.parametrize('error_type', [
    TypeError, ValueError, OSError, RuntimeError, OverflowError, KeyboardInterrupt, SystemExit, GeneratorExit,
])
def test_second_child_conversion_exceptions_escape_after_cleanup_and_allow_reuse(
    monkeypatch: pytest.MonkeyPatch, window: str, error_type: Type[BaseException],
) -> None:
    """Preserve the original exception instead of a failed result or callback diagnostic.

    Only runner construction is shared: the public suite and single-run entry
    points retain their real execution paths. Reusing that runner after the
    exception exposes leaked activity, while signal restoration checks cleanup
    for BaseExceptions as well as ordinary numeric conversion errors.
    """
    runner = BenchmarkRunner()
    monkeypatch.setattr(api, 'BenchmarkRunner', lambda: runner)
    previous_handler = signal.getsignal(signal.SIGINT)
    events: List[str] = []
    error = error_type('second ' + window + ' conversion failed')
    warmup = _Seconds('warmup', (0.0, error if window == 'warmup' else 0.0), events)
    measurement = _Seconds('measurement', (0.1, error if window == 'measurement' else 0.1), events)

    with pytest.raises(error_type) as caught:
        run_all(warmup_seconds=warmup, measurement_seconds=measurement,
                on_progress=lambda value: _record_progress(events, value))

    assert caught.value is error
    assert caught.value.__context__ is None
    assert str(caught.value) == 'second ' + window + ' conversion failed'
    expected = ['warmup:1', 'measurement:1', 'start:single_core', 'completed:single_core', 'warmup:2']
    if window == 'measurement':
        expected.append('measurement:2')
    assert events == expected
    assert signal.getsignal(signal.SIGINT) is previous_handler
    assert runner.run('single_core', warmup_seconds=0, measurement_seconds=0.1).success


@pytest.mark.parametrize('window', ['warmup', 'measurement'])
@pytest.mark.parametrize('policy', ['default', 'handled', 'custom', 'ignored'])
def test_sigint_during_second_conversion_interrupts_caller_immediately(
    monkeypatch: pytest.MonkeyPatch, window: str, policy: str,
) -> None:
    """Legacy child conversions run under the caller's ordinary SIGINT policy.

    A signal raised inside __float__ must stop that method immediately, before
    its next statement or another conversion. The native callback catches the
    original KeyboardInterrupt until workers and handles are cleaned up, then
    returns that same exception without attaching its private transport error.
    """
    events: List[str] = []
    raised: List[KeyboardInterrupt] = []

    class Seconds(_Seconds):
        def __float__(self) -> float:
            value = super().__float__()
            if self.name == window and self.conversions == 2:
                assert signal.getsignal(signal.SIGINT) is handler
                try:
                    signal.raise_signal(signal.SIGINT)
                except KeyboardInterrupt as error:
                    raised.append(error)
                    if policy != 'handled':
                        raise
                events.append('after-signal')
            return value

    def custom_handler(_signum: int, _frame: object) -> None:
        events.append('custom-handler')

    runner = BenchmarkRunner()
    monkeypatch.setattr(api, 'BenchmarkRunner', lambda: runner)
    warmup = Seconds('warmup', (0.0, 0.0), events)
    measurement = Seconds('measurement', (0.1, 0.1), events)
    handler = custom_handler if policy == 'custom' else (
        signal.SIG_IGN if policy == 'ignored' else signal.default_int_handler
    )
    previous = signal.signal(signal.SIGINT, handler)
    try:
        if policy == 'default':
            with pytest.raises(KeyboardInterrupt) as caught:
                run_all(warmup_seconds=warmup, measurement_seconds=measurement,
                        on_progress=lambda value: _record_progress(events, value))
            assert len(raised) == 1
            assert raised[0] is caught.value
            assert caught.value.__context__ is None
        else:
            try:
                suite = run_all(warmup_seconds=warmup, measurement_seconds=measurement,
                                on_progress=lambda value: _record_progress(events, value))
            except KeyboardInterrupt:
                pytest.fail('the caller handled SIGINT and the suite must continue')
            assert len(suite.results) == 2
            assert all(result.success for result in suite.results)
            assert len(raised) == (1 if policy == 'handled' else 0)
        assert signal.getsignal(signal.SIGINT) is handler
        expected = ['warmup:1', 'measurement:1', 'start:single_core', 'completed:single_core', 'warmup:2']
        if window == 'measurement':
            expected.append('measurement:2')
        if policy != 'default':
            if policy == 'custom':
                expected.append('custom-handler')
            expected.append('after-signal')
            if window == 'warmup':
                expected.append('measurement:2')
            expected.extend(['start:multi_core', 'completed:multi_core'])
        assert events == expected
        assert runner.run('single_core', warmup_seconds=0, measurement_seconds=0.1).success
    finally:
        signal.signal(signal.SIGINT, previous)


@pytest.mark.parametrize('second_signal', ['after-catching', 'finally'])
def test_repeated_converter_sigint_preserves_default_exception_chain(
    monkeypatch: pytest.MonkeyPatch, second_signal: str,
) -> None:
    """Each user-code SIGINT raises, retaining Python's normal exception replacement chain."""
    events: List[str] = []
    raised: List[KeyboardInterrupt] = []

    class Seconds(_Seconds):
        def __float__(self) -> float:
            value = super().__float__()
            if self.conversions == 2:
                try:
                    try:
                        signal.raise_signal(signal.SIGINT)
                    except KeyboardInterrupt as error:
                        raised.append(error)
                        if second_signal == 'finally':
                            raise
                    events.append('after-first-signal')
                    try:
                        signal.raise_signal(signal.SIGINT)
                    except KeyboardInterrupt as error:
                        raised.append(error)
                        raise
                    events.append('after-second-signal')
                finally:
                    if second_signal == 'finally':
                        events.append('conversion-finally')
                        try:
                            signal.raise_signal(signal.SIGINT)
                        except KeyboardInterrupt as error:
                            raised.append(error)
                            raise
                        events.append('after-finally-signal')
            return value

    runner = BenchmarkRunner()
    monkeypatch.setattr(api, 'BenchmarkRunner', lambda: runner)
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            run_all(warmup_seconds=Seconds('warmup', (0.0, 0.0), events),
                    measurement_seconds=_Seconds('measurement', (0.1, AssertionError('unused measurement')), events))
        assert len(raised) == 2
        assert caught.value is raised[1]
        assert raised[0].__context__ is None
        assert caught.value.__context__ is (raised[0] if second_signal == 'finally' else None)
        assert caught.value.__cause__ is None
        boundary = 'conversion-finally' if second_signal == 'finally' else 'after-first-signal'
        assert events == ['warmup:1', 'measurement:1', 'warmup:2', boundary]
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
        assert runner.run('single_core', warmup_seconds=0, measurement_seconds=0.1).success
    finally:
        signal.signal(signal.SIGINT, previous)


def test_suite_configuration_exception_survives_sigint_during_native_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """The new shared suite handle defers teardown signals after host conversion already failed.

    HEAD had already freed its first child's handle before this conversion, so
    this is an added-wrapper ownership check, not an original-behavior baseline.
    Keep the conversion error, complete actual native cleanup and reuse the runner.
    """
    from benchset import _native  # noqa: PLC0415

    if not hasattr(_native.Native, 'execute_suite'):
        pytest.skip('HEAD has no shared native suite handle to free after a conversion error')

    events: List[str] = []
    raised: List[KeyboardInterrupt] = []

    class Seconds(_Seconds):
        def __float__(self) -> float:
            value = super().__float__()
            if self.conversions == 2:
                try:
                    signal.raise_signal(signal.SIGINT)
                except KeyboardInterrupt as error:
                    raised.append(error)
                    raise
            return value

    original_free = _native.Native.free

    def free(native: _native.Native, handle: int, suite: bool = False) -> None:
        if raised:
            events.append('native-cleanup')
            signal.raise_signal(signal.SIGINT)
            events.append('native-cleanup-done')
        original_free(native, handle, True) if suite else original_free(native, handle)

    monkeypatch.setattr(_native.Native, 'free', free)
    runner = BenchmarkRunner()
    monkeypatch.setattr(api, 'BenchmarkRunner', lambda: runner)
    warmup = Seconds('warmup', (0.0, 0.0), events)
    measurement = _Seconds('measurement', (0.1, AssertionError('unused measurement')), events)
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            run_all(warmup_seconds=warmup, measurement_seconds=measurement)
        assert len(raised) == 1
        assert caught.value is raised[0]
        assert caught.value.__context__ is None
        assert events == ['warmup:1', 'measurement:1', 'warmup:2', 'native-cleanup', 'native-cleanup-done']
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
        raised.clear()
        assert runner.run('single_core', warmup_seconds=0, measurement_seconds=0.1).success
    finally:
        signal.signal(signal.SIGINT, previous)


def test_handled_warmup_sigint_preserves_measurement_interrupt_policy() -> None:
    """Handling one converter's interrupt must not consume the next converter's signal."""
    events: List[str] = []
    raised: List[KeyboardInterrupt] = []

    class Seconds(_Seconds):
        def __float__(self) -> float:
            value = super().__float__()
            if self.conversions == 2:
                try:
                    signal.raise_signal(signal.SIGINT)
                except KeyboardInterrupt as error:
                    raised.append(error)
                    if self.name == 'measurement':
                        raise
                events.append('handled-warmup')
            return value

    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            run_all(warmup_seconds=Seconds('warmup', (0.0, 0.0), events),
                    measurement_seconds=Seconds('measurement', (0.1, 0.1), events))
        assert len(raised) == 2
        assert caught.value is raised[1]
        assert caught.value.__context__ is None
        assert events == ['warmup:1', 'measurement:1', 'warmup:2', 'handled-warmup', 'measurement:2']
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    finally:
        signal.signal(signal.SIGINT, previous)


def test_background_suite_conversions_preserve_the_callers_signal_policy() -> None:
    """A worker thread converts both children without changing process signal handlers."""
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    events: List[str] = []
    handler = signal.getsignal(signal.SIGINT)

    class Seconds(_Seconds):
        def __float__(self) -> float:
            assert signal.getsignal(signal.SIGINT) is handler
            return super().__float__()

    with ThreadPoolExecutor(max_workers=1) as pool:
        suite = pool.submit(run_all, warmup_seconds=Seconds('warmup', (0.0, 0.0), events),
                            measurement_seconds=Seconds('measurement', (0.1, 0.1), events)).result(timeout=5)
    assert len(suite.results) == 2
    assert all(result.success for result in suite.results)
    assert events == ['warmup:1', 'measurement:1', 'warmup:2', 'measurement:2']
    assert signal.getsignal(signal.SIGINT) is handler
