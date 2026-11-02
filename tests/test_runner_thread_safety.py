"""Custom signal reentry must keep cancellation attached to its original call.

The runner intentionally becomes idle during host validation. These regressions
exercise both sides of that handoff: a signal can cancel the still-active loader
owner, or complete another call while the outer call is idle. A reentrant RLock
alone cannot protect a shared cancellation flag from those same-thread changes.
"""

import subprocess
import sys

import pytest

from benchset import BenchmarkRunner

_PROGRAM = r'''
import inspect
import signal
import sys
from benchset import BenchmarkRunner, api
from benchset._native import Native

stage, outer_mode, nested_mode = sys.argv[1:]
outer_suite, nested_suite = outer_mode == 'suite', nested_mode == 'suite'
runner = BenchmarkRunner()
live, allocations, cancellations, releases = {}, [], [], []
nested, signals, progress_calls = [], [], []
loaded = False
armed = True

class AuditedNative(Native):
    def create(self, suite=False):
        handle = super().create(suite) if suite else super().create()
        assert handle not in live, 'an allocation overlaps a still-live handle'
        identity = (self, handle, suite)
        live[handle] = identity
        allocations.append(identity)
        return handle

    def cancel(self, handle, suite=False):
        identity = (self, handle, suite)
        assert live.get(handle) == identity, 'cancel used a freed, foreign, or wrong-mode handle'
        cancellations.append(identity)
        return super().cancel(handle, suite) if suite else super().cancel(handle)

    def free(self, handle, suite=False):
        identity = (self, handle, suite)
        assert live.pop(handle) == identity, 'free used a foreign or wrong-mode handle'
        releases.append(identity)
        return super().free(handle, suite) if suite else super().free(handle)


def factory():
    global loaded
    native = AuditedNative()
    if not loaded:
        loaded = True
        if stage == 'completed':
            # This cancellation belongs to the loader's provisional owner.
            runner.cancel()
    return native

api.Native = factory


def execute(suite, callback=None):
    return (runner._run_all(0, .1, callback) if suite else
            runner.run('single_core', warmup_seconds=0, measurement_seconds=.1, on_progress=callback))


def results(value, suite):
    return value.results if suite else (value,)


def cancel_nested(value):
    if not progress_calls:
        progress_calls.append(value.phase)
        runner.cancel()


def handler(_signum, _frame):
    signals.append(runner._active is not None)
    if stage in ('completed', 'cancelled'):
        nested.append(execute(nested_suite, cancel_nested if stage == 'cancelled' else None))
    else:
        runner.cancel()


method = getattr(BenchmarkRunner, '_run', BenchmarkRunner.run)
source, start = inspect.getsourcelines(method)
claims = [start + index for index, line in enumerate(source) if line.strip() == 'self._active = owner']
handoff = len(claims) == 2
if handoff:
    if stage == 'release':
        # Immediately before publication is cleared: the original owner is active.
        boundary = next(start + index for index, line in enumerate(source)
                        if line.strip() == 'self._active = None')
    elif stage == 'published':
        # The next traced statement runs after the owner assignment completed.
        boundary = claims[-1] + 1
    else:
        boundary = claims[-1]
else:
    # Actual HEAD has one uninterrupted single-run owner. Equivalent reentry
    # before allocation must remain busy and must retain loader cancellation.
    boundary = next(start + index for index, line in enumerate(source)
                    if line.strip() == ('native = Native()' if stage == 'published' else 'handle = native.create()'))


def trace(frame, event, _arg):
    global armed
    if armed and event == 'line' and frame.f_code is method.__code__ and frame.f_lineno == boundary:
        armed = False
        signal.raise_signal(signal.SIGINT)
    return trace


previous_handler = signal.signal(signal.SIGINT, handler)
previous_trace = sys.gettrace()
sys.settrace(trace)
try:
    outer = execute(outer_suite)
finally:
    sys.settrace(previous_trace)
    assert signal.getsignal(signal.SIGINT) is handler, 'the runner replaced the caller signal policy'
    signal.signal(signal.SIGINT, previous_handler)

assert not armed and len(signals) == 1, 'the selected signal boundary was not exercised'
assert signals == [stage in ('release', 'published') or not handoff]
assert not live and releases == allocations, 'every native handle must be released exactly once'
assert runner._active is None and runner._handle is None and runner._native is None
expected = 'completed' if stage == 'cancelled' else 'cancelled'
assert results(outer, outer_suite)[-1].status == expected, (
    stage, outer_suite, nested_suite, results(outer, outer_suite),
)
assert signal.getsignal(signal.SIGINT) is previous_handler

if stage in ('completed', 'cancelled'):
    assert len(nested) == 1
    child_results = results(nested[0], nested_suite)
    if handoff:
        expected_child = 'completed' if stage == 'completed' else 'cancelled'
        assert child_results[-1].status == expected_child, child_results
        assert len(allocations) == 2
        # The nested call allocates/frees before its suspended outer call.
        child_identity, outer_identity = allocations
        assert child_identity[2] == nested_suite and outer_identity[2] == outer_suite
        assert cancellations == [outer_identity if stage == 'completed' else child_identity]
        assert bool(progress_calls) == (stage == 'cancelled')
    else:
        assert child_results[0].reason == 'this runner is already running'
        assert len(allocations) == 1 and not progress_calls
        assert cancellations == (allocations if stage == 'completed' else [])
else:
    assert len(allocations) == 1
    assert cancellations == allocations

# Reuse under the same caller-owned policy checks complete owner and signal cleanup.
previous_handler = signal.signal(signal.SIGINT, handler)
try:
    reused = execute(outer_suite)
    assert all(result.success for result in results(reused, outer_suite)), reused
    assert signal.getsignal(signal.SIGINT) is handler
finally:
    signal.signal(signal.SIGINT, previous_handler)
assert not live and releases == allocations
assert runner._active is None and runner._handle is None and runner._native is None
'''


@pytest.mark.parametrize('outer_suite', [False, True], ids=['single', 'suite'])
@pytest.mark.parametrize(('stage', 'nested_suite'), [
    ('release', False),
    ('published', False),
    ('completed', False),
    ('completed', True),
    ('cancelled', False),
    ('cancelled', True),
], ids=['release', 'published', 'completed-single', 'completed-suite', 'cancelled-single', 'cancelled-suite'])
def test_signal_reentry_preserves_each_calls_cancellation(
    stage: str, outer_suite: bool, nested_suite: bool,
) -> None:
    """Real custom signals cannot lose or import cancellation at ownership handoffs.

    Release: cancel after loading while the provisional owner is still active.
    Completed: retain an outer loader cancellation across a completed nested call.
    Cancelled: keep a nested cancellation from contaminating an uncancelled outer.
    Published: cancel the newly published owner as a negative control for resets.

    Child processes isolate tracing, signal policy, and possible wrong-handle
    faults. Actual native calls and handle audits verify cancellation and free,
    rather than merely inspecting a final Python-side flag.
    """
    if not hasattr(BenchmarkRunner, '_run_all') and (outer_suite or nested_suite):
        pytest.skip('HEAD supports reusable runner control only for single runs')
    result = subprocess.run(
        [sys.executable, '-c', _PROGRAM, stage,
         'suite' if outer_suite else 'single', 'suite' if nested_suite else 'single'],
        capture_output=True, text=True, check=False, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
