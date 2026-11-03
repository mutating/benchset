"""Keep native ownership safe through caller signal errors and nested output calls."""

import io
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from threading import enumerate as threads
from typing import Any, List

import pytest

from benchset import BenchmarkRunner, cli, run, run_all
from benchset._native import Native
from tests.helpers import assert_runner_reusable

_ROOT = Path(__file__).resolve().parents[1]
_CLEANUP = r'''
import builtins
import gc
import hashlib
import io
import json
from pathlib import Path
import signal
import sys
import weakref
from threading import Event, Thread
from benchset import BenchmarkRunner, cli
from benchset._native import Native

primary_type, secondary_type, inherited = sys.argv[1:4]
normal = len(sys.argv) > 4 and sys.argv[4] == 'normal'
immediate = len(sys.argv) > 4 and sys.argv[4] == 'immediate'
normal_joined = False
class WeakError(RuntimeError):
    pass

primary = (WeakError if primary_type == 'WeakError' else getattr(builtins, primary_type))('primary caller signal')
if primary_type == 'WeakError':
    gc.disable()
secondary = getattr(builtins, secondary_type)('secondary caller signal')
entered, release, finished = (Event() for _ in range(3))
runner, workers, acknowledgements, live = BenchmarkRunner(), [], [], set()
install, create, free = signal.signal, Native.create, Native.free
outputting = tearing_down = False
remaining = 3
calls = 0

def custom(_signum, _frame):
    global calls
    calls += 1
    # Exercise both immediate replacement and a repeated first handler error.
    raise primary if calls == 1 or (calls == 2 and not immediate and not normal) else secondary

previous = install(signal.SIGINT, custom)

def intercepted(signum, handler):
    global remaining
    if (normal_joined if normal else outputting) and getattr(handler, '__name__', '') == 'interrupt' and remaining:
        remaining -= 1
        signal.raise_signal(signal.SIGINT)
    return install(signum, handler)

def allocate(native, *arguments):
    handle = create(native, *arguments)
    live.add((id(native), handle, arguments))
    return handle

def blocked_free(native, handle, *arguments):
    free(native, handle, *arguments)
    live.remove((id(native), handle, arguments))
    if outputting and not normal:
        entered.set()
        assert release.wait(5), 'native cleanup was never released'

def render(*_arguments):
    global outputting
    outputting = True
    if not normal:
        signal.raise_signal(signal.SIGINT)

class Worker(Thread):
    armed = True

    def __init__(self, *arguments, **options):
        super().__init__(*arguments, **options)
        workers.append(self)

    def run(self):
        try:
            super().run()
        finally:
            finished.set()

    def join(self, *arguments, **options):
        global normal_joined
        if self.armed and outputting and not normal:
            self.armed = False
            assert entered.wait(5), 'native cleanup was never entered'
            signal.raise_signal(signal.SIGINT)
            release.set()
        value = super().join(*arguments, **options)
        if normal:
            install(signal.SIGINT, custom)
            normal_joined = True
        return value

class Acknowledgement(Event):
    def wait(self, timeout=None):
        return True if tearing_down else super().wait(timeout)

def acknowledgement():
    value = Acknowledgement()
    acknowledgements.append(value)
    return value

Native.create, Native.free = allocate, blocked_free
signal.signal = intercepted
cli.Thread, cli.ResultEvent = Worker, acknowledgement
cli.BenchmarkRunner, cli.render = lambda: runner, render
stdout, stderr = sys.stdout, sys.stderr
sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
result = {}

def invoke():
    try:
        result['code'] = cli.main(['--warmup=0', '--duration=.05'])
    except BaseException as error:
        result['exception'] = type(error).__name__
        result['original'] = error is primary

try:
    if inherited == 'yes':
        try:
            raise LookupError('unrelated caller context')
        except LookupError:
            invoke()
    else:
        invoke()
    result.update(finished=finished.is_set(), active=bool(runner._active),
                  native=runner._native is not None, handle=runner._handle,
                  live=len(live), restored=signal.getsignal(signal.SIGINT) is custom,
                  workers=[worker.is_alive() for worker in workers],
                  cli_path=str(Path(cli.__file__).resolve()),
                  cli_sha256=hashlib.sha256(Path(cli.__file__).read_bytes()).hexdigest())
finally:
    # Failed implementations must also leave this subprocess able to exit.
    # The assertions below use witnesses captured before this fallback cleanup.
    tearing_down = True
    signal.signal = install
    install(signal.SIGINT, previous)
    release.set()
    runner.cancel()
    for value in acknowledgements:
        value.set()
    for worker in workers:
        Thread.join(worker, timeout=5)
    sys.stdout, sys.stderr = stdout, stderr
if primary_type == 'WeakError':
    reference = weakref.ref(primary)
    primary = secondary = None
    result['error_collected'] = reference() is None
    gc.enable()
print(json.dumps(result))
'''


@pytest.mark.parametrize('primary', ['RuntimeError', 'ValueError', 'KeyboardInterrupt', 'SystemExit', 'GeneratorExit'])
@pytest.mark.parametrize('secondary', ['RuntimeError', 'KeyboardInterrupt', 'SystemExit'])
@pytest.mark.parametrize('inherited', [False, True])
@pytest.mark.parametrize('immediate', [False, True])
def test_cli_cleanup_keeps_the_first_custom_signal_error_and_joins_native_workers(
    primary: str, secondary: str, inherited: bool, immediate: bool,
) -> None:
    """Repeated caller signal errors cannot escape a live native cleanup boundary.

    An event blocks real native release, and another signal interrupts handoff
    and join, including a distinct error on the first restoration attempt.
    Secondary errors must preserve the first escaping error;
    KeyboardInterrupt retains CLI exit 130. A separate completion witness catches
    premature return even if interrupted Thread.join corrupts its own bookkeeping.
    Calling from an unrelated except block prevents caller context from being
    mistaken for this CLI's pending exception. Fallback teardown cannot satisfy
    the witnesses, because they are copied before fallback begins.
    """
    environment = os.environ.copy()
    environment.pop('PYTHONPATH', None)
    process = subprocess.run(
        [sys.executable, '-c', _CLEANUP, primary, secondary, 'yes' if inherited else 'no',
         'immediate' if immediate else 'repeated'],
        cwd=_ROOT, env=environment, capture_output=True, text=True, check=False, timeout=15,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert not process.stderr
    result = json.loads(process.stdout)
    assert result['cli_path'] == str((_ROOT / 'benchset/cli.py').resolve())
    assert result['finished'], 'CLI returned before its worker finished'
    assert not result['active']
    assert not result['native']
    assert result['handle'] is None
    assert result['live'] == 0
    assert not any(result['workers'])
    assert result['restored']
    if primary == 'KeyboardInterrupt':
        assert result['code'] == 130
    else:
        assert result['exception'] == primary
        assert result['original'], 'a later signal replaced the first caller error'


@pytest.mark.parametrize('primary', ['RuntimeError', 'KeyboardInterrupt', 'SystemExit', 'WeakError'])
@pytest.mark.parametrize('inherited', [False, True])
def test_cli_keeps_a_new_cleanup_guard_error_after_a_successful_join(
    primary: str, inherited: bool,
) -> None:
    """A fresh signal error after successful join must escape rather than disappear.

    The worker installs a caller handler immediately after its normal join;
    real signals then interrupt cleanup-guard installation. Preserve the first
    new error after teardown even when an unrelated caller exception is active.
    This distinguishes a body error being unwound from inherited except context.
    Weak errors must disappear with cyclic GC disabled once the caller drops them,
    protecting the temporary error holder from retaining its own traceback.
    """
    environment = os.environ.copy()
    environment.pop('PYTHONPATH', None)
    process = subprocess.run(
        [sys.executable, '-c', _CLEANUP, primary, 'GeneratorExit', 'yes' if inherited else 'no', 'normal'],
        cwd=_ROOT, env=environment, capture_output=True, text=True, check=False, timeout=15,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert not process.stderr
    result = json.loads(process.stdout)
    assert result['finished']
    assert not result['active']
    assert not result['native']
    assert result['handle'] is None
    assert result['live'] == 0
    assert not any(result['workers'])
    assert result['restored']
    if primary == 'KeyboardInterrupt':
        assert result['code'] == 130
    else:
        assert result['exception'] == primary
        assert result['original']
    if primary == 'WeakError':
        assert result['error_collected'], 'the guard retained its own exception traceback'


@pytest.mark.parametrize('nested', ['run', 'run_all'])
@pytest.mark.parametrize('repeated', [False, True])
def test_cli_nested_output_sigint_cancels_the_outer_cli_after_complete_native_cleanup(
    monkeypatch: pytest.MonkeyPatch, nested: str, repeated: bool,
) -> None:
    """Preserve the approved whole-CLI cancellation policy for nested output work.

    The caller's output sink handles any nested KeyboardInterrupt itself. SIGINT
    still cancels the enclosing CLI with 130; making the output guard look like
    the ordinary default handler must not silently isolate the nested call.
    Once cancelled, another signal must let the output sink finish normally.
    Audit all real native handles and worker completion before caller reuse.
    """
    live = set()
    allocated: List[Any] = []
    released: List[Any] = []
    create, free = Native.create, Native.free
    entered = False
    nested_finished = False

    def allocate(native: Native, *arguments: Any) -> int:
        handle = create(native, *arguments)
        identity = id(native), handle, arguments
        live.add(identity)
        allocated.append(identity)
        return handle

    def release(native: Native, handle: int, *arguments: Any) -> None:
        free(native, handle, *arguments)
        identity = id(native), handle, arguments
        live.remove(identity)
        released.append(identity)

    class Output(io.StringIO):
        def write(self, text: str) -> int:
            nonlocal entered, nested_finished
            if not entered and text.startswith('Benchmark'):
                entered = True
                emitted = False

                def progress(_value: Any) -> None:
                    nonlocal emitted
                    if not emitted:
                        emitted = True
                        signal.raise_signal(signal.SIGINT)

                try:
                    if nested == 'run':
                        run('single_core', warmup_seconds=0, measurement_seconds=.01, on_progress=progress)
                    else:
                        run_all(warmup_seconds=0, measurement_seconds=.01, on_progress=progress)
                except KeyboardInterrupt:
                    pass
                if repeated:
                    signal.raise_signal(signal.SIGINT)
                nested_finished = True
            return super().write(text)

    output, errors = Output(), io.StringIO()
    monkeypatch.setattr(Native, 'create', allocate)
    monkeypatch.setattr(Native, 'free', release)
    monkeypatch.setattr(cli.sys, 'stdout', output)
    monkeypatch.setattr(cli.sys, 'stderr', errors)
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        assert cli.main(['--warmup=0', '--duration=.05']) == 130
        assert entered
        assert nested_finished
        assert not live
        assert len(allocated) == len(released) == 2
        assert not any(worker.name == 'benchset-cli' for worker in threads())
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
        assert_runner_reusable(BenchmarkRunner())
        assert not live
    finally:
        signal.signal(signal.SIGINT, previous)
