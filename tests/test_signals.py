"""Default Ctrl+C must cross the native boundary only after complete cleanup."""

import os
import signal
import subprocess
import sys
from typing import Iterator

import pytest

from benchset import BenchmarkRunner, Progress, api
from tests.test_semantics import ManagedNative


@pytest.fixture
def default_sigint() -> Iterator[None]:
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX subprocess SIGINT delivery')
@pytest.mark.parametrize('callback', [False, True])
@pytest.mark.parametrize('warmup', [0, 2])
@pytest.mark.parametrize('ignored_parent_sigint', [False, True])
def test_real_sigint_interrupts_api_and_permits_reuse(
    callback: bool, warmup: int, ignored_parent_sigint: bool,
) -> None:
    code = f'''
import signal
import sys
signal.signal(signal.SIGINT, signal.default_int_handler)
from benchset import BenchmarkRunner
from benchset._native import Native
from tests.helpers import assert_runner_reusable
execute = Native.execute
armed = True

def wait_for_signal():
    global armed
    if armed:
        armed = False
        print('ready', flush=True)
        assert sys.stdin.readline() == 'release\\n'

def synchronized_execute(self, *args, **kwargs):
    # run() has installed its handler and published a live native handle.
    # Without a callback, synchronize before entering the C function. With a
    # callback, synchronize inside the first observed native phase instead.
    if not {callback}:
        wait_for_signal()
    return execute(self, *args, **kwargs)

Native.execute = synchronized_execute
runner = BenchmarkRunner()
try:
    runner.run('single_core', warmup_seconds={warmup}, measurement_seconds=2,
               on_progress=(lambda _progress: wait_for_signal()) if {callback} else None)
except KeyboardInterrupt:
    Native.execute = execute
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    assert_runner_reusable(runner)
    print('interrupted and reused')
else:
    raise SystemExit('SIGINT was swallowed')
'''
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN if ignored_parent_sigint else signal.default_int_handler)
    try:
        process = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, stdin=subprocess.PIPE, text=True)
    finally:
        signal.signal(signal.SIGINT, previous)
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == 'ready\n'
        process.send_signal(signal.SIGINT)
        output, errors = process.communicate(input='release\n', timeout=5)
        assert process.returncode == 0, errors
        assert output == 'interrupted and reused\n'
        assert not errors
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.usefixtures('default_sigint')
@pytest.mark.parametrize('stage', ['loading', 'allocation', 'execute', 'free_before', 'free_after'])
def test_sigint_defers_through_handle_lifetime_and_restores_runner(
    monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    backend = ManagedNative()
    original_create, original_free = backend.create, backend.free
    armed = True

    def interrupt() -> None:
        if armed:
            signal.raise_signal(signal.SIGINT)
            signal.raise_signal(signal.SIGINT)

    def create() -> int:
        handle = original_create()
        if stage == 'allocation':
            interrupt()
        return handle

    def free(handle: int) -> None:
        if stage == 'free_before':
            interrupt()
        original_free(handle)
        if stage == 'free_after':
            interrupt()

    def cancel(handle: int) -> None:
        assert 'free' not in backend.calls, 'a signal must never cancel a freed handle'
        assert not stage.startswith('free'), 'cancellation must be disabled before freeing starts'
        assert handle == 123
        backend.calls.append('cancel')

    def factory() -> ManagedNative:
        if stage == 'loading':
            interrupt()
        return backend

    def progress(_value: Progress) -> None:
        if stage == 'execute':
            interrupt()

    monkeypatch.setattr(backend, 'create', create)
    monkeypatch.setattr(backend, 'free', free)
    monkeypatch.setattr(backend, 'cancel', cancel)
    monkeypatch.setattr(api, 'Native', factory)
    runner = BenchmarkRunner()
    try:
        with pytest.raises(KeyboardInterrupt):
            runner.run('single_core', warmup_seconds=0, measurement_seconds=.01, on_progress=progress)
        assert backend.calls.count('free') == backend.calls.count('create')
        assert runner._handle is None
        assert runner._native is None
        assert not runner._active
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    finally:
        armed = False
    assert runner.run('single_core', warmup_seconds=0, measurement_seconds=.01).success
    assert backend.calls.count('free') == backend.calls.count('create')


@pytest.mark.usefixtures('default_sigint')
def test_sigint_keeps_precedence_when_native_execution_also_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = ManagedNative()
    monkeypatch.setattr(api, 'Native', lambda: backend)

    def fail(_progress: Progress) -> None:
        signal.raise_signal(signal.SIGINT)
        raise OverflowError('native execution failed too')

    runner = BenchmarkRunner()
    with pytest.raises(KeyboardInterrupt):
        runner.run('single_core', warmup_seconds=0, measurement_seconds=.01, on_progress=fail)
    assert backend.calls.count('free') == 1
    assert not runner._active
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


@pytest.mark.parametrize('kind', ['custom', 'ignored', 'termination'])
def test_api_preserves_user_sigint_handlers(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    backend = ManagedNative()
    monkeypatch.setattr(api, 'Native', lambda: backend)
    handler = (lambda _signum, _frame: None) if kind == 'custom' else (
        signal.SIG_IGN if kind == 'ignored' else signal.SIG_DFL
    )
    previous = signal.signal(signal.SIGINT, handler)
    try:
        def progress(_value: Progress) -> None:
            assert signal.getsignal(signal.SIGINT) is handler
        assert BenchmarkRunner().run('single_core', on_progress=progress).success
        assert signal.getsignal(signal.SIGINT) is handler
    finally:
        signal.signal(signal.SIGINT, previous)


@pytest.mark.usefixtures('default_sigint')
def test_callback_can_install_its_own_signal_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, 'Native', ManagedNative)

    def handler(_signum: int, _frame: object) -> None:
        pass

    def progress(_value: Progress) -> None:
        signal.signal(signal.SIGINT, handler)

    assert BenchmarkRunner().run('single_core', on_progress=progress).success
    assert signal.getsignal(signal.SIGINT) is handler


@pytest.mark.parametrize('stage', [
    'entry', 'claimed', 'publishing', 'published', 'cancel', 'free_before', 'free_after',
    'claimed+interrupt', 'allocated+interrupt', 'published+interrupt',
])
def test_custom_sigint_can_cancel_inside_runner_critical_sections(stage: str) -> None:
    # A child contains deadlocks; a real signal at an observed line controls
    # reentry without depending on scheduler timing or completing CPU work.
    code = r'''
import faulthandler
import inspect
import signal
import sys
from dataclasses import replace
from benchset import BenchmarkRunner, api
from tests.test_semantics import ManagedNative

stage, _, interrupt = sys.argv[1].partition('+')
armed = True

def deliver():
    global armed
    armed = False
    signal.raise_signal(signal.SIGINT)

class Backend(ManagedNative):
    cancelled = False
    freeing = False

    def create(self):
        self.cancelled = False
        return super().create()

    def cancel(self, handle):
        assert not self.freeing, 'cancellation during/after free'
        super().cancel(handle)
        self.cancelled = True

    def free(self, handle):
        self.freeing = True
        if stage == 'free_before' and armed:
            deliver()
        super().free(handle)
        if stage == 'free_after' and armed:
            deliver()
        self.freeing = False

    def execute(self, *args):
        result = super().execute(*args)
        return replace(result, status='cancelled', score=None) if self.cancelled else result

backend = Backend()
api.Native = lambda: backend
runner = BenchmarkRunner()

def handler(*_):
    runner.cancel()
    if stage != 'entry':
        busy = runner.run('single_core', warmup_seconds=0, measurement_seconds=.01)
        assert busy.reason == 'this runner is already running'
    if interrupt:
        raise KeyboardInterrupt

def entry_line(method):
    source, start = inspect.getsourcelines(method)
    return next(start + i for i, line in enumerate(source) if line.strip() == 'if self._active:')

run_entry, cancel_entry = entry_line(BenchmarkRunner.run), entry_line(BenchmarkRunner.cancel)

def trace(frame, event, _arg):
    if armed and event == 'line':
        if frame.f_code is BenchmarkRunner.run.__code__:
            if stage == 'entry' and frame.f_lineno == run_entry:
                deliver()
            elif stage == 'claimed' and runner._active and runner._native is None:
                deliver()
            elif stage == 'allocated' and frame.f_locals.get('handle') is not None and runner._native is None:
                deliver()
            elif stage == 'publishing' and runner._native is not None and runner._handle is None:
                deliver()
            elif stage == 'published' and runner._native is not None and runner._handle is not None:
                deliver()
        elif frame.f_code is BenchmarkRunner.cancel.__code__ and stage == 'cancel' and frame.f_lineno == cancel_entry:
            deliver()
    return trace

signal.signal(signal.SIGINT, handler)
faulthandler.dump_traceback_later(5, exit=True)
sys.settrace(trace)
try:
    result = runner.run('single_core', warmup_seconds=0, measurement_seconds=.01,
                        on_progress=(lambda _progress: runner.cancel()) if stage == 'cancel' else None)
except KeyboardInterrupt:
    assert interrupt
else:
    assert not interrupt
    expected = 'completed' if stage in {'entry', 'free_before', 'free_after'} else 'cancelled'
    assert result.status == expected, (result.status, result.reason)
finally:
    sys.settrace(None)
assert not armed, 'the selected injection point was not reached'
assert not runner._active and runner._handle is None and runner._native is None
if stage in {'entry', 'free_before', 'free_after'}:
    assert 'cancel' not in backend.calls
allocations = 0 if interrupt and stage == 'claimed' else 1
assert backend.calls.count('create') == backend.calls.count('free') == allocations
assert signal.getsignal(signal.SIGINT) is handler
assert runner.run('single_core', warmup_seconds=0, measurement_seconds=.01).success
assert backend.calls.count('create') == backend.calls.count('free') == allocations + 1
faulthandler.cancel_dump_traceback_later()
'''
    result = subprocess.run([sys.executable, '-c', code, stage], capture_output=True,
                            text=True, check=False, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
