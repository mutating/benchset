"""Caller-owned name operators run before the reusable runner claims execution."""

import signal
from threading import Event, Thread
from typing import List, Optional

import pytest

from benchset import BenchmarkResult, BenchmarkRunner, Progress
from tests.helpers import assert_runner_reusable


@pytest.mark.parametrize('cancel_at', [1, 2])
def test_name_operator_cancellation_keeps_validation_before_runner_ownership(cancel_at: int) -> None:
    """Validation cancellation is idle; execution cancellation targets the newly claimed run."""
    runner = BenchmarkRunner()

    class Name(str):
        __slots__ = ('__dict__',)
        __hash__ = str.__hash__
        comparisons = 0

        def __eq__(self, other: object) -> bool:
            self.comparisons += 1
            if self.comparisons == cancel_at:
                runner.cancel()
            return str.__eq__(self, other)

    name = Name('single_core')
    result = runner.run(name, warmup_seconds=0, measurement_seconds=.1)
    assert result.status == ('completed' if cancel_at == 1 else 'cancelled')
    assert result.benchmark is name
    assert name.comparisons == 2
    assert_runner_reusable(runner)


def test_name_operator_can_complete_a_nested_run_before_outer_ownership() -> None:
    """A typed name may run the idle runner while its outer request is still being validated."""
    runner = BenchmarkRunner()
    nested: List[BenchmarkResult] = []

    class Name(str):
        __slots__ = ()
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
            if not nested:
                nested.append(runner.run('multi_core', warmup_seconds=0, measurement_seconds=.1))
            return str.__eq__(self, other)

    result = runner.run(Name('single_core'), warmup_seconds=0, measurement_seconds=.1)
    assert len(nested) == 1
    assert nested[0].success
    assert result.success
    assert_runner_reusable(runner)


@pytest.mark.parametrize('outcome', ['valid', 'invalid', 'error'])
def test_name_operator_may_publish_another_owner_during_validation(outcome: str) -> None:
    """Recheck activity after host comparison and never release the owner it starts.

    Validation may return a valid name, reject it, or raise the caller's own
    exception while that other run stays active. Each losing path must leave
    cancellation and cleanup in the other run's hands.
    """
    runner = BenchmarkRunner()
    ready, release = Event(), Event()
    results: List[BenchmarkResult] = []
    worker: Optional[Thread] = None
    error = ValueError('name comparison failed')

    def progress(_value: Progress) -> None:
        ready.set()
        assert release.wait(5)

    def execute() -> None:
        try:
            results.append(runner.run('multi_core', warmup_seconds=0, measurement_seconds=.1,
                                      on_progress=progress))
        finally:
            ready.set()

    class Name(str):
        __slots__ = ()
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
            nonlocal worker
            if worker is None:
                worker = Thread(target=execute)
                worker.start()
                assert ready.wait(5)
            if outcome == 'error':
                raise error
            return outcome == 'valid' and str.__eq__(self, other)

    try:
        if outcome == 'error':
            with pytest.raises(ValueError, match='name comparison failed') as caught:
                runner.run(Name('single_core'), warmup_seconds=0, measurement_seconds=.1)
            assert caught.value is error
            assert caught.value.__context__ is None
        else:
            result = runner.run(Name('single_core'), warmup_seconds=0, measurement_seconds=.1)
            expected = 'this runner is already running' if outcome == 'valid' else 'unknown benchmark: single_core'
            assert result.reason == expected
        assert not results
        assert runner.run('single_core', warmup_seconds=0, measurement_seconds=.1).reason == 'this runner is already running'
    finally:
        runner.cancel()
        release.set()
        if worker is not None:
            worker.join(5)
    assert worker is not None
    assert not worker.is_alive()
    assert len(results) == 1
    assert results[0].status == 'cancelled'
    assert_runner_reusable(runner)


def test_name_operator_can_finish_an_existing_owner_before_claim() -> None:
    """A request that starts out busy may claim the runner once its comparison finishes that owner."""
    runner = BenchmarkRunner()
    entered, release = Event(), Event()
    results: List[BenchmarkResult] = []

    def progress(_value: Progress) -> None:
        entered.set()
        assert release.wait(5)

    worker = Thread(target=lambda: results.append(runner.run('multi_core', warmup_seconds=0,
                                                             measurement_seconds=.1, on_progress=progress)))

    class Name(str):
        __slots__ = ('__dict__',)
        __hash__ = str.__hash__
        comparisons = 0

        def __eq__(self, other: object) -> bool:
            self.comparisons += 1
            if self.comparisons == 1:
                runner.cancel()
                release.set()
                worker.join(5)
            return str.__eq__(self, other)

    worker.start()
    try:
        assert entered.wait(5)
        result = runner.run(Name('single_core'), warmup_seconds=0, measurement_seconds=.1)
        assert result.success
        assert len(results) == 1
        assert results[0].status == 'cancelled'
    finally:
        runner.cancel()
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert_runner_reusable(runner)


@pytest.mark.parametrize('policy', ['default', 'handled', 'custom', 'ignored'])
def test_name_validation_operator_observes_the_callers_sigint_policy(policy: str) -> None:
    """A validation signal interrupts its operator immediately, unless the caller handles or ignores it."""
    runner = BenchmarkRunner()
    events: List[str] = []
    raised: List[KeyboardInterrupt] = []

    class Name(str):
        __slots__ = ('__dict__',)
        __hash__ = str.__hash__
        comparisons = 0

        def __eq__(self, other: object) -> bool:
            self.comparisons += 1
            if self.comparisons == 1:
                assert signal.getsignal(signal.SIGINT) is handler
                events.append('compare')
                try:
                    signal.raise_signal(signal.SIGINT)
                except KeyboardInterrupt as error:
                    raised.append(error)
                    if policy != 'handled':
                        raise
                events.append('after-signal')
            return str.__eq__(self, other)

    def custom_handler(_signum: int, _frame: object) -> None:
        events.append('custom-handler')

    handler = custom_handler if policy == 'custom' else (
        signal.SIG_IGN if policy == 'ignored' else signal.default_int_handler
    )
    previous = signal.signal(signal.SIGINT, handler)
    try:
        if policy == 'default':
            with pytest.raises(KeyboardInterrupt) as caught:
                runner.run(Name('single_core'), warmup_seconds=0, measurement_seconds=.1)
            assert len(raised) == 1
            assert caught.value is raised[0]
            assert caught.value.__context__ is None
            assert events == ['compare']
        else:
            try:
                result = runner.run(Name('single_core'), warmup_seconds=0, measurement_seconds=.1)
            except KeyboardInterrupt:
                pytest.fail('the caller handled or ignored the validation signal')
            assert result.success
            assert len(raised) == (1 if policy == 'handled' else 0)
            expected = ['compare', 'custom-handler', 'after-signal'] if policy == 'custom' else ['compare', 'after-signal']
            assert events == expected
        assert signal.getsignal(signal.SIGINT) is handler
        assert_runner_reusable(runner)
    finally:
        signal.signal(signal.SIGINT, previous)


@pytest.mark.parametrize('signal_at', ['nested', 'outer'])
def test_nested_run_from_name_validation_keeps_default_sigint_cancellation(signal_at: str) -> None:
    """A nested runner still owns default signal cancellation while an outer operator is suspended."""
    runner = BenchmarkRunner()
    events: List[str] = []

    def progress(_value: Progress) -> None:
        if signal_at == 'nested' and events[-1] == 'nested':
            events.append('nested-progress')
            signal.raise_signal(signal.SIGINT)

    def outer_progress(_value: Progress) -> None:
        if signal_at == 'outer' and events[-1] == 'after-nested':
            events.append('outer-progress')
            signal.raise_signal(signal.SIGINT)

    class Name(str):
        __slots__ = ()
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
            if not events:
                events.append('nested')
                runner.run('multi_core', warmup_seconds=0, measurement_seconds=.1, on_progress=progress)
                events.append('after-nested')
            return str.__eq__(self, other)

    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            runner.run(Name('single_core'), warmup_seconds=0, measurement_seconds=.1, on_progress=outer_progress)
        expected = ['nested', 'nested-progress'] if signal_at == 'nested' else ['nested', 'after-nested', 'outer-progress']
        assert events == expected
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
        assert_runner_reusable(runner)
    finally:
        signal.signal(signal.SIGINT, previous)


@pytest.mark.parametrize('second_signal', ['after-catching', 'finally'])
def test_repeated_name_operator_sigint_preserves_default_exception_chain(second_signal: str) -> None:
    """Validation operators keep each default SIGINT and the caller's exception context chain."""
    runner = BenchmarkRunner()
    events: List[str] = []
    raised: List[KeyboardInterrupt] = []

    class Name(str):
        __slots__ = ()
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
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
                    events.append('operator-finally')
                    try:
                        signal.raise_signal(signal.SIGINT)
                    except KeyboardInterrupt as error:
                        raised.append(error)
                        raise
                    events.append('after-finally-signal')
            return str.__eq__(self, other)

    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            runner.run(Name('single_core'), warmup_seconds=0, measurement_seconds=.1)
        assert len(raised) == 2
        assert caught.value is raised[1]
        assert raised[0].__context__ is None
        assert caught.value.__context__ is (raised[0] if second_signal == 'finally' else None)
        assert caught.value.__cause__ is None
        assert events == ['operator-finally' if second_signal == 'finally' else 'after-first-signal']
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
        assert_runner_reusable(runner)
    finally:
        signal.signal(signal.SIGINT, previous)
