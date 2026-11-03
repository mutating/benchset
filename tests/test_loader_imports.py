"""Import guards must not become permanent owners of native-loader state."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_BOOTSTRAP = '''
import ctypes
import gc
import pathlib
import sys
import weakref
assert 'benchset._native' not in sys.modules
standard_loader = ctypes.CDLL
calls = []
class Payload:
    def __init__(self):
        self.data = bytearray(256 * 1024)
class Loader:
    def __init__(self, payload):
        self.payload = payload
    def __call__(self, *args):
        calls.append(args)
        raise AssertionError('import must not load Rust')
def guarded_function(payload):
    def load(*args):
        calls.append(args)
        assert payload is not None
        raise AssertionError('import must not load Rust')
    return load
payload = Payload()
loader = guarded_function(payload) if sys.argv[1] == 'function' else Loader(payload)
payload_reference = weakref.ref(payload)
loader_reference = weakref.ref(loader)
ctypes.CDLL = loader
import benchset
assert pathlib.Path(benchset.__file__).resolve().parent == pathlib.Path.cwd() / 'benchset'
ctypes.CDLL = standard_loader
assert calls == []
del loader, payload
'''


def _run(program: str, *arguments: str) -> None:
    environment = dict(os.environ)
    # This scenario requires an untouched import state, including under coverage.
    for key in ('COVERAGE_PROCESS_START', 'COVERAGE_FILE'):
        environment.pop(key, None)
    result = subprocess.run([sys.executable, '-c', _BOOTSTRAP + program, *arguments], cwd=ROOT,
                            env=environment, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stderr


@pytest.mark.parametrize('factory_kind', ['callable', 'function'])
def test_native_free_import_releases_a_temporary_loader(factory_kind: str) -> None:
    """A pre-import guard and its data are released after restoration, even after an explicit collection.

    Fresh subprocesses prevent prior imports from hiding factory capture. Cover
    both a callable's copied attributes and a function's captured payload.
    """
    _run('''
gc.collect()
assert loader_reference() is None, 'native import retained the temporary factory'
assert payload_reference() is None, 'native import retained the factory payload'
''', factory_kind)


@pytest.mark.parametrize('operation', ['aggregate', 'cli', 'invalid_run'])
def test_restored_standard_loader_keeps_native_memory_bounded(operation: str) -> None:
    """After a guarded import, normal operations must still reuse the bounded standard library.

    Observe weak function-type references and actual DLL objects in three
    GC-disabled batches. This isolates loader ownership from argparse cycles
    and allocator caching while permitting one process-lived native image.
    """
    _run('''
import contextlib
import io
from benchset import BenchmarkResult, SuiteResult, _native, api, cli
base_native = _native.Native
function_types = []
class ObservedNative(base_native):
    def __init__(self):
        super().__init__()
        function_types.append(weakref.ref(type(self.lib.benchset_create)))
for module in (_native, api, cli):
    module.Native = ObservedNative
values = (BenchmarkResult('single_core', status='completed', pi_equivalent=1),
          BenchmarkResult('multi_core', status='completed', pi_equivalent=4))
def invoke():
    if sys.argv[2] == 'aggregate':
        assert SuiteResult.from_results(values).aggregated_scores.pi_equivalent == 2
    elif sys.argv[2] == 'cli':
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                cli.settings(['--warmup=-1', '--duration=.1'])
            except SystemExit as error:
                assert error.code == 2
            else:
                raise AssertionError('invalid duration must be rejected')
    else:
        result = api.run('unknown', warmup_seconds=0, measurement_seconds=.1)
        assert result.status == 'failed'
        assert result.reason == 'unknown benchmark: unknown'
ObservedNative()
invoke()
gc.collect()
function_types.clear()
baseline = sum(isinstance(value, standard_loader) for value in gc.get_objects())
gc.disable()
for batch in range(3):
    for attempt in range(20):
        invoke()
    surviving_types = {reference() for reference in function_types if reference() is not None}
    assert len(surviving_types) <= 1, 'restored loader creates a cyclic function type per call'
    live = sum(isinstance(value, standard_loader) for value in gc.get_objects())
    assert live <= baseline + 1, 'restored loader creates a DLL wrapper per call'
''', 'callable', operation)
