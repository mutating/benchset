"""Preserve bounded CLI failure for signal handlers owned by a native host."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PROGRAM = r'''
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import sys
from threading import Event, Thread
sys.path.insert(0, str(Path.cwd()))
from benchset import BenchmarkRunner, cli
from benchset._native import Native

benchmark = sys.argv[1]
window = next((value for value in sys.argv[2:] if value in ('warmup', 'duration', 'syntax')), 'valid')
arguments = ['--bench=' + benchmark, '--warmup=0', '--duration=.01']
if window == 'warmup':
    arguments[1] = '--warmup=-1'
elif window == 'duration':
    arguments[2] = '--duration=0'
elif window == 'syntax':
    arguments[2] = '--duration=not-a-number'
runner = BenchmarkRunner()
workers, live, allocated, released, invalid_restores = [], [], [], [], []
getsignal, install = signal.getsignal, signal.signal
initialize, create, free = Native.__init__, Native.create, Native.free
native_initializations = 0
initial = getsignal(signal.SIGINT)
captured = False
installed_guard = None

# A native embedding host with install_signal_handlers=0 can legitimately
# produce this state. Subsequent reads observe the actual Python CLI guard.
def embedded_handler(signum):
    global captured
    if signum == signal.SIGINT and not captured:
        captured = True
        return None
    return getsignal(signum)

expected = None
if window != 'syntax':
    try:
        install(signal.SIGINT, None)
    except TypeError as error:
        expected = str(error)

def restore(signum, handler):
    global installed_guard
    try:
        value = install(signum, handler)
        if signum == signal.SIGINT and installed_guard is None:
            installed_guard = handler
        return value
    except TypeError as error:
        if signum == signal.SIGINT and handler is None:
            invalid_restores.append(error)
            if len(invalid_restores) > 3:
                # Make an unbounded retry an immediate ordinary test failure,
                # rather than depending on a subprocess timeout or Ctrl+C.
                print(json.dumps({'unbounded_retry': True}), file=stdout, flush=True)
                os._exit(90)
        raise

def construct(native):
    global native_initializations
    native_initializations += 1
    initialize(native)

def allocate(native, *arguments):
    handle = create(native, *arguments)
    identity = id(native), handle, arguments
    live.append(identity)
    allocated.append(identity)
    return handle

def release(native, handle, *arguments):
    free(native, handle, *arguments)
    identity = id(native), handle, arguments
    live.remove(identity)
    released.append(identity)

class Worker(Thread):
    def __init__(self, *arguments, **options):
        super().__init__(*arguments, **options)
        self.finished = Event()
        self.allow_finish = Event()
        workers.append(self)

    def run(self):
        try:
            super().run()
        finally:
            assert self.allow_finish.wait(5), 'CLI did not join its worker'
            self.finished.set()

    def join(self, *arguments, **options):
        self.allow_finish.set()
        return super().join(*arguments, **options)

Native.__init__, Native.create, Native.free = construct, allocate, release
cli.BenchmarkRunner = lambda: runner
cli.Thread = Worker
signal.getsignal, signal.signal = embedded_handler, restore
output, errors = io.StringIO(), io.StringIO()
stdout, stderr = sys.stdout, sys.stderr
try:
    sys.stdout, sys.stderr = output, errors
    observed = {}
    try:
        observed['code'] = cli.main(arguments)
    except TypeError as error:
        observed.update(error=str(error), original=bool(invalid_restores) and error is invalid_restores[0])
    observed.update(
        captured=captured, allocated=len(allocated), released=allocated == released,
        native_initializations=native_initializations, live=len(live),
        active=runner._active is not None, native=runner._native is not None, handle=runner._handle,
        workers_finished=bool(workers) and all(worker.finished.is_set() for worker in workers),
        workers_alive=any(worker.is_alive() for worker in workers), workers_count=len(workers),
        output=output.getvalue(), errors=errors.getvalue(), expected=expected,
        guard_installed=installed_guard is not None,
        guard_retained=getsignal(signal.SIGINT) is installed_guard,
    )
finally:
    sys.stdout, sys.stderr = stdout, stderr
    signal.getsignal, signal.signal = getsignal, install
    install(signal.SIGINT, initial)
    for worker in workers:
        worker.join(timeout=5)

result = runner.run('single_core', warmup_seconds=10, measurement_seconds=10,
                    on_progress=lambda _progress: runner.cancel())
observed['reuse_status'] = result.status
observed['reuse_clean'] = not live and runner._active is None and runner._handle is None
observed['cli_path'] = str(Path(cli.__file__).resolve())
observed['cli_sha256'] = hashlib.sha256(Path(cli.__file__).read_bytes()).hexdigest()
print(json.dumps(observed))
'''


@pytest.mark.parametrize('benchmark', ['single_core', 'all'])
@pytest.mark.parametrize('window', ['valid', 'warmup', 'duration'])
def test_cli_native_owned_sigint_state_raises_original_type_error_after_cleanup(benchmark: str, window: str) -> None:
    """A native-owned SIGINT handler must not make final restoration retry forever.

    Real C embedding with PyConfig.install_signal_handlers=0 established that
    getsignal(SIGINT) may return None. Python cannot reinstall that opaque C
    handler: preserve the original one-shot TypeError instead of substituting
    SIG_DFL. Model only that initial backend state, run real native benchmarks,
    and audit resources before reusing the runner. A completion event requires
    an actual worker join. A bounded invalid-call witness makes the previous
    infinite retry fail promptly without timing assumptions.

    The user explicitly approved this TypeError preceding numeric range errors
    after successful syntax parsing. Invalid windows therefore allocate no native
    handle, while the parsing-only syntax failure is covered separately below.
    """
    environment = os.environ.copy()
    environment.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, '-c', _PROGRAM, benchmark, window], cwd=_ROOT,
                            env=environment, capture_output=True, text=True, check=False, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stderr
    observed = json.loads(result.stdout)
    assert observed['cli_path'] == str((_ROOT / 'benchset/cli.py').resolve())
    assert observed['captured']
    assert 'error' in observed, observed
    assert observed['error'] == observed['expected']
    assert observed['original']
    assert observed['guard_retained'], 'an opaque native handler must not be silently replaced with SIG_DFL'
    assert observed['native_initializations'] == 1
    assert observed['allocated'] == (1 if window == 'valid' else 0)
    assert observed['released']
    assert observed['live'] == 0
    assert not observed['active']
    assert not observed['native']
    assert observed['handle'] is None
    assert observed['workers_finished']
    assert not observed['workers_alive']
    assert not observed['output']
    assert not observed['errors']
    assert observed['reuse_status'] == 'cancelled'
    assert observed['reuse_clean']


def test_cli_syntax_errors_precede_native_signal_ownership_without_loading_rust() -> None:
    """Malformed argument syntax remains argparse exit 2 in an opaque native host.

    Unlike the explicitly approved numeric-range priority, parsing must finish
    before signal ownership, Rust construction or worker startup. Snapshot all
    three before the shared fixture restores its environment and reuses the
    runner, so its later successful work cannot mask eager initialization.
    """
    environment = os.environ.copy()
    environment.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, '-c', _PROGRAM, 'single_core', 'syntax'], cwd=_ROOT,
                            env=environment, capture_output=True, text=True, check=False, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stderr
    observed = json.loads(result.stdout)
    assert observed['cli_path'] == str((_ROOT / 'benchset/cli.py').resolve())
    assert observed['code'] == 2
    assert not observed['guard_installed']
    assert observed['native_initializations'] == 0
    assert observed['allocated'] == observed['live'] == observed['workers_count'] == 0
    assert not observed['active']
    assert not observed['native']
    assert observed['handle'] is None
    assert not observed['output']
    assert 'usage: benchset' in observed['errors']
    assert 'must be a number of seconds' in observed['errors']
    assert 'Traceback' not in observed['errors']
    assert observed['reuse_status'] == 'cancelled'
    assert observed['reuse_clean']


@pytest.mark.parametrize('caller', ['callable', 'default'])
def test_valid_final_sigint_policy_still_retries_a_caller_type_error(caller: str) -> None:
    """Only an opaque None policy makes TypeError a permanent input failure.

    A caller's SIGTERM handler can instead raise TypeError just before a valid
    SIGINT policy is reinstalled. Retry that interrupted operation and preserve
    its first error after complete cleanup. SIG_DFL is valid despite being zero,
    so a truthiness check must not confuse it with the unsupported None state.
    """
    from tests.test_cli_final_signal_restore import _program  # noqa: PLC0415

    # Python 3.8's metadata finder does not treat an empty sys.path entry as
    # the current directory; keep this checkout ahead of any older install.
    program = 'import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path.cwd()))\n' + _program('normal', 'before', False)
    if caller == 'default':
        replacements = {
            'previous = install(signal.SIGINT, custom)':
                'previous = install(signal.SIGINT, signal.SIG_DFL)',
            'and handler is custom and not final_restore_fired)':
                'and handler is signal.SIG_DFL and not final_restore_fired)',
            '(replacement if latest and final_restore_fired else custom)':
                '(replacement if latest and final_restore_fired else signal.SIG_DFL)',
        }
        for old, new in replacements.items():
            assert program.count(old) == 1
            program = program.replace(old, new)
    environment = os.environ.copy()
    environment.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, '-c', program, 'TypeError', 'GeneratorExit', 'no', 'normal'],
                            cwd=_ROOT, env=environment, capture_output=True, text=True, check=False, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stderr
    observed = json.loads(result.stdout)
    assert observed['exception'] == 'TypeError'
    assert observed['original']
    assert observed['finished']
    assert not observed['active']
    assert not observed['native']
    assert observed['handle'] is None
    assert observed['live'] == 0
    assert not any(observed['workers'])
    assert observed['restored']
