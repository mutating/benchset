"""Public calibration models, native multiplier mapping and CLI semantics."""

import ctypes
import gc
import io
import json
import pickle
import subprocess
import sys
import weakref
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from typing import Any, Callable, List, Optional, Tuple, get_type_hints

import pytest

import benchset
import benchset._native as native_api
from benchset import (
    BENCHMARKS,
    DEFAULT_MEASUREMENT_SECONDS,
    DEFAULT_WARMUP_SECONDS,
    PI_REFERENCE_MACHINE,
    PI_REFERENCE_RUN,
    BenchmarkResult,
    BenchmarkRunner,
    Progress,
    ReferenceBenchmarkResult,
    ReferenceMachine,
    ReferenceRun,
    Sample,
    __version__,
    api,
    cli,
    reference as reference_api,
    run,
    run_all,
)
from benchset._native import Native
from tests.helpers import assert_result_references
from tests.test_semantics import CallbackLibrary, ManagedNative, ResultLibrary

REFERENCE_SCORES = {
    'single_core': 47231.227029007096,
    'multi_core': 181953.17419139884,
}


def assert_short_native_result_references_and_multiplier(result: BenchmarkResult) -> None:
    assert_result_references(result)
    if result.success:
        assert result.score is not None
        assert result.score == result.iterations / result.elapsed_seconds
        assert result.pi_equivalent == result.score / REFERENCE_SCORES[result.benchmark]
    else:
        # A worker can be descheduled beyond this intentionally short window.
        assert result.status == 'failed', result.reason
        assert result.reason == 'measurement window was too short to complete any work'
        assert result.iterations == result.checksum == 0
        assert result.score is result.pi_equivalent is result.variation_percent is None
        assert not result.unstable
        assert all(sample.iterations == sample.iterations_per_second == 0 for sample in result.samples)


@pytest.mark.parametrize('benchmark', BENCHMARKS)
def test_result_reference_fields_are_public_required_types_with_global_defaults(benchmark: str) -> None:
    result = BenchmarkResult(benchmark)
    assert_result_references(result)
    annotations = get_type_hints(BenchmarkResult)
    assert annotations['reference_machine'] is ReferenceMachine
    assert annotations['reference_run'] is ReferenceRun
    result_fields = {field.name: field for field in fields(BenchmarkResult)}
    assert result_fields['reference_machine'].default is PI_REFERENCE_MACHINE
    assert result_fields['reference_run'].default is PI_REFERENCE_RUN
    assert reference_api.PI_REFERENCE_MACHINE is result.reference_machine
    assert reference_api.PI_REFERENCE_RUN is result.reference_run


def test_result_reference_fields_are_frozen_and_replace_preserves_reference_identity() -> None:
    result = BenchmarkResult('single_core')
    for field, replacement in [('reference_machine', PI_REFERENCE_MACHINE), ('reference_run', PI_REFERENCE_RUN)]:
        with pytest.raises(FrozenInstanceError):
            setattr(result, field, replacement)
        with pytest.raises(FrozenInstanceError):
            delattr(result, field)
    for value, field, replacement in [
        (result.reference_machine, 'name', 'changed'),
        (result.reference_run, 'warmup_seconds', 0),
        (result.reference_run.results[0], 'score', 0),
    ]:
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, replacement)
    with pytest.raises(TypeError):
        result.reference_run.results[0] = ReferenceBenchmarkResult('single_core', 0)  # type: ignore[index]
    assert_result_references(replace(result, reason='changed diagnostic'))

    # The public dataclass still permits explicitly supplied calibration values.
    custom_machine = replace(PI_REFERENCE_MACHINE, name='consumer machine')
    custom_run = replace(PI_REFERENCE_RUN, warmup_seconds=1.0)
    custom = BenchmarkResult('multi_core', reference_machine=custom_machine, reference_run=custom_run)
    changed = replace(custom, status='completed')
    assert changed.reference_machine is custom_machine
    assert changed.reference_run is custom_run
    replaced = replace(result, reference_machine=custom_machine, reference_run=custom_run)
    assert replaced.reference_machine is custom_machine
    assert replaced.reference_run is custom_run
    assert_result_references(result)


def test_result_reference_serialization_copies_data_without_mutating_globals() -> None:
    result = BenchmarkResult('single_core', status='completed', score=8.0, pi_equivalent=0.5,
                             samples=(Sample(1, 1, 8, 8.0),))
    payload = asdict(result)
    assert payload['reference_machine'] == asdict(PI_REFERENCE_MACHINE)
    assert payload['reference_run'] == asdict(PI_REFERENCE_RUN)
    serialized = json.loads(json.dumps(payload, allow_nan=False))
    assert serialized['benchmark'] == result.benchmark
    assert serialized['pi_equivalent'] == 0.5
    assert serialized['reference_machine'] == asdict(PI_REFERENCE_MACHINE)
    assert serialized['reference_run'] == json.loads(json.dumps(asdict(PI_REFERENCE_RUN)))
    assert type(serialized['reference_run']['started_at_unix_seconds']) is float
    assert 'started_at' not in serialized['reference_run']
    payload['reference_machine']['name'] = 'changed copy'
    payload['reference_run']['results'][0]['score'] = 0
    assert result.reference_machine.name == 'Raspberry Pi 5 Model B Rev 1.0'
    assert result.reference_run.results[0].score == REFERENCE_SCORES['single_core']
    # Ordinary copying/serialization promises equal data, not singleton identity.
    assert deepcopy(result) == result
    assert pickle.loads(pickle.dumps(result)) == result
    assert_result_references(result)


def test_calibration_fixture_python_and_native_metadata_agree_without_a_handle() -> None:
    calibration = json.loads((Path(__file__).resolve().parents[1] / 'native/reference-calibration.json').read_text())
    assert calibration['machine'] == asdict(PI_REFERENCE_MACHINE)
    assert calibration['run'] == json.loads(json.dumps(asdict(PI_REFERENCE_RUN)))
    for name, score in REFERENCE_SCORES.items():
        total = calibration['totals'][name]
        assert total['iterations'] / total['elapsed_seconds'] == score

    native = Native()
    machine = native_api.CReferenceMachine()
    assert native.lib.benchset_reference_machine(ctypes.byref(machine), ctypes.sizeof(machine)) == 1
    for field in ('name', 'short_name', 'os_name', 'os_version', 'cpu_architecture'):
        assert getattr(machine, field).decode('utf-8') == calibration['machine'][field]
    assert machine.memory_bytes == calibration['machine']['memory_bytes']
    assert machine.cpu_cores == calibration['machine']['cpu_cores']

    reference = native_api.CReferenceRun()
    assert native.lib.benchset_reference_run(ctypes.byref(reference), ctypes.sizeof(reference)) == 1
    for field in ('started_at_unix_seconds', 'start_temperature_c', 'end_temperature_c', 'warmup_seconds'):
        assert getattr(reference, field) == calibration['run'][field]
    assert reference.result_count == len(calibration['run']['results'])
    for index, expected in enumerate(calibration['run']['results']):
        result = native_api.CReferenceBenchmarkResult()
        assert native.lib.benchset_reference_result(index, ctypes.byref(result), ctypes.sizeof(result)) == 1
        assert result.benchmark.decode('utf-8') == expected['benchmark']
        assert result.score == expected['score']


def test_public_machine_and_run_models_have_exact_recorded_metadata() -> None:
    assert isinstance(PI_REFERENCE_MACHINE, ReferenceMachine)
    assert asdict(PI_REFERENCE_MACHINE) == {
        'name': 'Raspberry Pi 5 Model B Rev 1.0',
        'short_name': 'pi',
        'os_name': 'Ubuntu Server',
        'os_version': '26.04 LTS',
        'cpu_architecture': 'aarch64',
        'memory_bytes': 8589934592,
        'cpu_cores': 4,
    }
    assert type(PI_REFERENCE_MACHINE.memory_bytes) is int
    assert PI_REFERENCE_MACHINE.memory_bytes == 8 * 1024**3
    assert PI_REFERENCE_MACHINE.memory_bytes > 8317272064  # Installed capacity differs from Linux MemTotal.
    assert isinstance(PI_REFERENCE_RUN, ReferenceRun)
    assert PI_REFERENCE_RUN.started_at_unix_seconds == 1792767590.517391
    assert PI_REFERENCE_RUN.start_temperature_c == 41.35
    assert PI_REFERENCE_RUN.end_temperature_c == 66.65
    assert PI_REFERENCE_RUN.warmup_seconds == 1248.0
    assert tuple(value.benchmark for value in PI_REFERENCE_RUN.results) == BENCHMARKS
    assert len({value.benchmark for value in PI_REFERENCE_RUN.results}) == 2
    assert all(isinstance(value, ReferenceBenchmarkResult) for value in PI_REFERENCE_RUN.results)
    assert {value.benchmark: value.score for value in PI_REFERENCE_RUN.results} == REFERENCE_SCORES
    assert REFERENCE_SCORES['single_core'] == 42508112 / 900.000162475
    assert REFERENCE_SCORES['multi_core'] == 163757952 / 900.000523364
    # Historical calibration settings do not silently change runtime defaults.
    assert DEFAULT_WARMUP_SECONDS == BenchmarkResult('single_core').warmup_seconds == 120.0
    assert DEFAULT_MEASUREMENT_SECONDS == BenchmarkResult('single_core').measurement_seconds == 60.0


def test_reference_timestamp_is_aware_utc_and_json_keeps_primitive_epoch_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('TZ', 'Pacific/Honolulu')
    expected = datetime(2026, 10, 23, 14, 59, 50, 517391, tzinfo=timezone.utc)
    assert PI_REFERENCE_RUN.started_at == expected
    assert PI_REFERENCE_RUN.started_at.tzinfo is timezone.utc
    assert PI_REFERENCE_RUN.started_at.timestamp() == PI_REFERENCE_RUN.started_at_unix_seconds
    assert PI_REFERENCE_RUN.started_at.isoformat() == '2026-10-23T14:59:50.517391+00:00'
    payload = asdict(PI_REFERENCE_RUN)
    assert 'started_at' not in payload
    assert type(payload['started_at_unix_seconds']) is float
    serialized = json.loads(json.dumps(payload, allow_nan=False))
    assert serialized == {
        'started_at_unix_seconds': 1792767590.517391,
        'start_temperature_c': 41.35,
        'end_temperature_c': 66.65,
        'warmup_seconds': 1248.0,
        'results': [
            {'benchmark': 'single_core', 'score': REFERENCE_SCORES['single_core']},
            {'benchmark': 'multi_core', 'score': REFERENCE_SCORES['multi_core']},
        ],
    }
    assert json.loads(json.dumps(asdict(PI_REFERENCE_MACHINE), allow_nan=False)) == asdict(PI_REFERENCE_MACHINE)


def test_reference_models_are_frozen_including_nested_results_and_tuple() -> None:
    for value, field, replacement in [
        (PI_REFERENCE_MACHINE, 'name', 'different machine'),
        (PI_REFERENCE_MACHINE, 'memory_bytes', 1),
        (PI_REFERENCE_RUN, 'warmup_seconds', 0),
        (PI_REFERENCE_RUN, 'results', ()),
        (PI_REFERENCE_RUN.results[0], 'score', 0),
        (PI_REFERENCE_RUN.results[1], 'benchmark', 'single_core'),
    ]:
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, replacement)
    assert isinstance(PI_REFERENCE_RUN.results, tuple)
    with pytest.raises(TypeError):
        PI_REFERENCE_RUN.results[0] = ReferenceBenchmarkResult('single_core', 0)  # type: ignore[index]
    copy = asdict(PI_REFERENCE_RUN)
    copy['results'][0]['score'] = 0
    assert PI_REFERENCE_RUN.results[0].score == REFERENCE_SCORES['single_core']
    changed = replace(PI_REFERENCE_RUN, results=(ReferenceBenchmarkResult('single_core', 1),))
    assert changed.results != PI_REFERENCE_RUN.results
    assert PI_REFERENCE_RUN.results[0].score == REFERENCE_SCORES['single_core']


def test_calibration_imports_and_serialization_do_not_load_native_code() -> None:
    # A fresh interpreter checks actual import behavior rather than an already
    # cached module. This also runs on the oldest supported Python in CI.
    program = """
import ctypes
import json
from dataclasses import asdict
def forbidden(*args, **kwargs):
    raise AssertionError('calibration import must not load native code')
ctypes.CDLL = forbidden
from benchset import (
    PI_REFERENCE_MACHINE, PI_REFERENCE_RUN, ReferenceMachine,
    ReferenceRun, ReferenceBenchmarkResult, BenchmarkResult, BenchmarkRunner, run, run_all,
)
assert isinstance(PI_REFERENCE_MACHINE, ReferenceMachine)
assert isinstance(PI_REFERENCE_RUN, ReferenceRun)
assert isinstance(PI_REFERENCE_RUN.results[0], ReferenceBenchmarkResult)
assert PI_REFERENCE_RUN.started_at.utcoffset().total_seconds() == 0
assert json.loads(json.dumps(asdict(PI_REFERENCE_RUN)))['warmup_seconds'] == 1248
results = [BenchmarkResult('single_core'), run('unknown'),
           BenchmarkRunner().run('multi_core', warmup_seconds=-1)]
results.extend(run_all(measurement_seconds=0).results)
assert len(results) == 4
for result in results:
    assert result.pi_equivalent is None
    assert result.reference_machine is PI_REFERENCE_MACHINE
    assert result.reference_run is PI_REFERENCE_RUN
    assert result.reference_run.started_at.utcoffset().total_seconds() == 0
    serialized = json.loads(json.dumps(asdict(result), allow_nan=False))
    assert serialized['reference_machine'] == asdict(PI_REFERENCE_MACHINE)
    assert serialized['reference_run']['results'][0]['benchmark'] == 'single_core'
    assert serialized['reference_run']['warmup_seconds'] == 1248
print(PI_REFERENCE_MACHINE.short_name)
"""
    result = subprocess.run([sys.executable, '-c', program], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'pi\n'
    assert not result.stderr


def test_metadata_and_short_measurements_need_no_network_ssh_or_sensor_access() -> None:
    program = """
import builtins
import io
import os
import socket
import subprocess

def forbidden(*args, **kwargs):
    raise AssertionError('runtime calibration must not use network or child processes')

socket.socket = forbidden
socket.create_connection = forbidden
subprocess.Popen = forbidden
subprocess.run = forbidden
subprocess.check_call = forbidden
subprocess.check_output = forbidden
os.system = forbidden

def guarded_open(original):
    def open_without_sensors(path, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)):
            name = os.fsdecode(path)
            if name.startswith(('/sys/class/thermal', '/sys/class/hwmon', '/dev/vcio')):
                raise AssertionError('runtime calibration must not read temperature sensors')
            if 'benchset-pi-reference-20261023' in name:
                raise AssertionError('runtime calibration must not read the external experiment folder')
        return original(path, *args, **kwargs)
    return open_without_sensors

builtins.open = guarded_open(builtins.open)
io.open = guarded_open(io.open)
from benchset import PI_REFERENCE_MACHINE, PI_REFERENCE_RUN, run
assert PI_REFERENCE_MACHINE.short_name == 'pi'
references = {value.benchmark: value.score for value in PI_REFERENCE_RUN.results}
for benchmark in ('single_core', 'multi_core'):
    result = run(benchmark, warmup_seconds=0, measurement_seconds=0.02)
    assert result.reference_machine is PI_REFERENCE_MACHINE
    assert result.reference_run is PI_REFERENCE_RUN
    if result.success:
        assert result.score == result.iterations / result.elapsed_seconds
        assert result.pi_equivalent == result.score / references[benchmark]
    else:
        assert result.status == 'failed', result.reason
        assert result.reason == 'measurement window was too short to complete any work'
        assert result.iterations == result.checksum == 0
        assert result.score is result.pi_equivalent is result.variation_percent is None
        assert not result.unstable
        assert all(sample.iterations == sample.iterations_per_second == 0 for sample in result.samples)
print('self-contained')
"""
    result = subprocess.run([sys.executable, '-c', program], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'self-contained\n'
    assert not result.stderr


def test_multiplier_is_a_real_optional_field_appended_after_legacy_positional_arguments() -> None:
    samples = (Sample(1.0, 1.0, 8, 8.0),)
    legacy = BenchmarkResult('single_core', '1.2.3', 'completed', 'reason', 8.0, 'iterations/s', 1,
                             0.5, 2.5, 1.0, 8, 9, samples, 0.0, True)
    assert [field.name for field in fields(BenchmarkResult)][-3:] == [
        'pi_equivalent', 'reference_machine', 'reference_run',
    ]
    assert_result_references(legacy)
    assert legacy.pi_equivalent is None
    assert (legacy.version, legacy.status, legacy.reason, legacy.score, legacy.workers,
            legacy.warmup_seconds, legacy.measurement_seconds, legacy.elapsed_seconds,
            legacy.iterations, legacy.checksum, legacy.samples, legacy.variation_percent,
            legacy.unstable) == ('1.2.3', 'completed', 'reason', 8.0, 1, 0.5, 2.5, 1.0, 8, 9, samples, 0.0, True)
    populated = BenchmarkResult('single_core', '1.2.3', 'completed', None, 8.0, 'iterations/s', 1,
                                0.5, 2.5, 1.0, 8, 9, samples, None, False, 0.5)
    assert_result_references(populated)
    assert populated.pi_equivalent == 0.5
    assert asdict(legacy)['pi_equivalent'] is None
    assert json.loads(json.dumps(asdict(populated)))['pi_equivalent'] == 0.5
    with pytest.raises(FrozenInstanceError):
        populated.pi_equivalent = 1.0  # type: ignore[misc]


@pytest.mark.parametrize('benchmark', BENCHMARKS)
@pytest.mark.parametrize('status', [0, 1, 2])
@pytest.mark.parametrize('has_ratio', [0, 1, 7, 2**32 - 1])
@pytest.mark.parametrize('ratio', [0.0, 0.5, 1.0, 2.0, 1 / 3, 1e-100, 123456.78901234567])
def test_ctypes_maps_multiplier_without_recalculation_and_respects_status_and_presence(
    monkeypatch: pytest.MonkeyPatch, benchmark: str, status: int, has_ratio: int, ratio: float,
) -> None:
    """Honor native status/presence without recomputing or losing double precision.

    The wire fixture leaves nonzero C padding after the uint32 presence flag;
    reading that padding as part of a wider flag must not fabricate a ratio.
    """
    native = Native()
    library = ResultLibrary(status, None, 0)
    library.value.pi_equivalent = ratio
    library.value.has_pi_equivalent = has_ratio
    monkeypatch.setattr(native, 'lib', library)
    result = native.execute(123, benchmark, 0.5, 1.5, None)
    assert_result_references(result)
    expected = ratio if status == 0 and has_ratio else None
    assert result.pi_equivalent == expected
    if expected is not None:
        assert type(result.pi_equivalent) is float
    # The deliberately unrelated score catches wrapper recalculation and
    # worker normalization. The Rust core is responsible for deriving ratios.
    assert result.score == (123.5 if status == 0 else None)
    assert result.workers == 2**32 - 1
    assert result.unstable


@pytest.mark.parametrize('failure_at', ['config', 'result', 'sample'])
def test_extended_result_failures_clean_up_once_and_recovery_has_fresh_ratio(
    monkeypatch: pytest.MonkeyPatch, failure_at: str,
) -> None:
    native = Native()
    library = CallbackLibrary()
    monkeypatch.setattr(native, 'lib', library)
    monkeypatch.setattr(api, 'Native', lambda: native)
    runner = BenchmarkRunner()
    if failure_at == 'config':
        original = native_api.Config

        class BrokenConfig(native_api.Config):
            def __init__(self, *_arguments: object) -> None:
                raise RuntimeError('config unavailable')

        monkeypatch.setattr(native_api, 'Config', BrokenConfig)
    else:
        name = 'benchset_result_v2' if failure_at == 'result' else 'benchset_sample'
        original = getattr(library, name)
        monkeypatch.setattr(library, name, lambda *_arguments: 0)
    result = runner.run('single_core', warmup_seconds=0, measurement_seconds=0.02)
    assert_result_references(result)
    assert result.status == 'failed'
    assert result.score is result.pi_equivalent is None
    assert library.created == library.freed == [1]
    if failure_at == 'config':
        monkeypatch.setattr(native_api, 'Config', original)
    else:
        monkeypatch.setattr(library, name, original)
    library.value.pi_equivalent = 2.125
    recovered = runner.run('single_core', warmup_seconds=0, measurement_seconds=0.02)
    assert_result_references(recovered)
    assert recovered.success
    assert recovered.pi_equivalent == 2.125
    assert library.created == library.freed == [1, 2]


def test_same_version_legacy_library_reports_missing_symbols_without_allocating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allocations: List[int] = []

    class LegacyLibrary:
        def __init__(self) -> None:
            self.benchset_abi_version = lambda: 1
            self.benchset_version = lambda: __version__.encode('utf-8')
            self.benchset_create = lambda: allocations.append(1)
            # The original ABI-1 binary exports all original entry points, but
            # predates the additive multiplier and calibration getters.
            for name in ('benchset_run', 'benchset_cancel', 'benchset_result',
                         'benchset_sample', 'benchset_reason', 'benchset_free'):
                setattr(self, name, lambda *_arguments: None)

    monkeypatch.setattr(ctypes, 'CDLL', lambda _path: LegacyLibrary())
    runner = BenchmarkRunner()
    result = runner.run('single_core', warmup_seconds=0, measurement_seconds=0.01)
    assert_result_references(result)
    assert result.status == 'failed'
    assert result.score is result.pi_equivalent is None
    assert result.reason == 'native library is missing required symbol: benchset_result_v2'
    assert not allocations
    assert not runner._active
    assert runner._native is runner._handle is None


@pytest.mark.parametrize(('abi_version', 'version'), [
    (0, __version__), (2, __version__), (1, 'older-package'),
])
def test_incompatible_library_is_rejected_before_resolving_new_symbols(
    monkeypatch: pytest.MonkeyPatch, abi_version: int, version: str,
) -> None:
    class OldLibrary:
        def __init__(self) -> None:
            self.benchset_abi_version = lambda: abi_version
            self.benchset_version = lambda: version.encode('utf-8')

        def __getattr__(self, name: str) -> None:
            pytest.fail('incompatible native library was asked for symbol ' + name)

    library = OldLibrary()
    monkeypatch.setattr(ctypes, 'CDLL', lambda _path: library)
    with pytest.raises(RuntimeError, match='version/ABI'):
        Native()


def test_globals_remain_identical_and_readable_during_callbacks_and_after_terminal_states() -> None:
    machine = PI_REFERENCE_MACHINE
    reference_run = PI_REFERENCE_RUN
    snapshot = (asdict(machine), asdict(reference_run))
    runner = BenchmarkRunner()
    observations: List[Tuple[str, str]] = []

    def progress(value: Progress) -> None:
        assert benchset.PI_REFERENCE_MACHINE is machine
        assert benchset.PI_REFERENCE_RUN is reference_run
        assert (asdict(machine), asdict(reference_run)) == snapshot
        observations.append((value.benchmark, value.phase))
        if value.phase == 'completed':
            runner.cancel()  # A terminal cancellation preserves a completed multiplier.

    completed = runner.run('single_core', warmup_seconds=0, measurement_seconds=0.03, on_progress=progress)
    assert_short_native_result_references_and_multiplier(completed)
    assert observations[-1] == ('single_core', completed.status)

    def cancelled(value: Progress) -> None:
        progress(value)
        if value.phase == 'measurement':
            runner.cancel()

    cancelled_result = runner.run('multi_core', warmup_seconds=0, measurement_seconds=1, on_progress=cancelled)
    assert_result_references(cancelled_result)
    assert cancelled_result.status == 'cancelled'
    assert cancelled_result.pi_equivalent is None
    failure = runner.run('unknown', warmup_seconds=0, measurement_seconds=0.01)
    assert_result_references(failure)
    assert failure.status == 'failed'
    assert failure.pi_equivalent is None
    recovered = runner.run('multi_core', warmup_seconds=0, measurement_seconds=0.02, on_progress=progress)
    assert_short_native_result_references_and_multiplier(recovered)
    assert (asdict(machine), asdict(reference_run)) == snapshot


def test_retained_result_metadata_outlives_runner_and_is_readable_during_later_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = Native()
    library = CallbackLibrary()
    monkeypatch.setattr(native, 'lib', library)
    monkeypatch.setattr(api, 'Native', lambda: native)
    runner = BenchmarkRunner()
    retained = runner.run('single_core', warmup_seconds=0, measurement_seconds=0.02)
    assert retained.success
    assert_result_references(retained)
    assert library.created == library.freed == [1]
    runner_reference = weakref.ref(runner)
    del runner
    gc.collect()
    assert runner_reference() is None
    snapshot = (asdict(retained.reference_machine), asdict(retained.reference_run))

    backend = ManagedNative()
    started, release = Event(), Event()
    execute = backend.execute

    def blocked(*args: Any) -> BenchmarkResult:
        started.set()
        assert release.wait(5)
        return execute(*args)

    monkeypatch.setattr(backend, 'execute', blocked)
    monkeypatch.setattr(api, 'Native', lambda: backend)
    later_runner = BenchmarkRunner()
    results: List[BenchmarkResult] = []
    observations: List[str] = []

    def observe(progress: Progress) -> None:
        assert_result_references(retained)
        assert (asdict(retained.reference_machine), asdict(retained.reference_run)) == snapshot
        observations.append(progress.phase)

    thread = Thread(target=lambda: results.append(later_runner.run('multi_core', on_progress=observe)))
    thread.start()
    try:
        assert started.wait(5)
        assert later_runner._handle == 123
        assert_result_references(retained)
        assert_result_references(BenchmarkResult('multi_core'))
        assert (asdict(retained.reference_machine), asdict(retained.reference_run)) == snapshot
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert len(results) == 1
    assert_result_references(results[0])
    assert observations == ['completed']
    assert backend.calls == ['create', 'execute', 'free']
    later_runner.cancel()
    assert_result_references(retained)
    assert (asdict(retained.reference_machine), asdict(retained.reference_run)) == snapshot


@pytest.mark.parametrize(('stop_index', 'status'), [(0, 'failed'), (0, 'cancelled'), (1, 'failed'), (1, 'cancelled'), (1, 'completed')])
def test_run_all_preserves_individual_native_multipliers_and_forwarded_settings(
    monkeypatch: pytest.MonkeyPatch, stop_index: int, status: str,
) -> None:
    names: List[str] = []
    callback = lambda _progress: None

    def execute(benchmark: str, *, warmup_seconds: float, measurement_seconds: float,
                on_progress: Optional[Callable[[Progress], None]]) -> BenchmarkResult:
        assert (warmup_seconds, measurement_seconds, on_progress) == (0.25, 0.75, callback)
        current_status = status if len(names) == stop_index else 'completed'
        ratio = 0.5 if benchmark == 'single_core' else 2.0
        names.append(benchmark)
        return BenchmarkResult(benchmark, status=current_status, score=123.5 if current_status == 'completed' else None,
                               pi_equivalent=ratio if current_status == 'completed' else None)

    monkeypatch.setattr(api, 'run', execute)
    results = run_all(warmup_seconds=0.25, measurement_seconds=0.75, on_progress=callback).results
    for result in results:
        assert_result_references(result)
    assert names == list(BENCHMARKS[:stop_index + 1])
    assert [value.pi_equivalent for value in results] == [
        (0.5 if value.benchmark == 'single_core' else 2.0) if value.success else None for value in results
    ]


@pytest.mark.parametrize('ratio', [None, 0.0, 0.5, 1.0, 2.0, 1 / 3, 1e-100, 123456789012345.67])
def test_cli_renders_native_multiplier_including_zero_tiny_large_and_unavailable(ratio: Optional[float]) -> None:
    result = BenchmarkResult('multi_core', status='completed', score=123.5, pi_equivalent=ratio)
    output = io.StringIO()
    cli.render(result, output)
    text = output.getvalue()
    ratio_line = next(line for line in text.splitlines() if line.startswith('Pi equivalent'))
    machine_line = next(line for line in text.splitlines() if line.startswith('Reference machine'))
    assert PI_REFERENCE_MACHINE.name in machine_line
    assert PI_REFERENCE_MACHINE.short_name in machine_line
    assert ('—' if ratio is None else f'{ratio:.12g} \u00d7 pi') in ratio_line
    assert '123.500' in text
    assert 'iterations/s' in text


@pytest.mark.parametrize(('status', 'code'), [('completed', 0), ('cancelled', 130), ('failed', 1)])
@pytest.mark.parametrize('benchmark', ['all', 'single_core', 'multi_core'])
def test_cli_preserves_native_ratio_streams_exit_status_and_selection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], status: str, code: int, benchmark: str,
) -> None:
    """Display each authoritative ratio and derive a non-unit aggregate from them.

    Completed unstable results still contribute to the non-unit mean, guarding
    against filtering them out or hardcoding a unit summary in CLI orchestration.
    Failure and cancellation still route unavailable scores to stderr.
    """
    calls: List[str] = []

    def execute(_self: BenchmarkRunner, name: str, **_options: Any) -> BenchmarkResult:
        calls.append(name)
        ratio = 0.5 if name == 'single_core' else 8.0
        return BenchmarkResult(name, status=status, score=123.5 if status == 'completed' else None,
                               pi_equivalent=ratio if status == 'completed' else None,
                               unstable=status == 'completed')

    monkeypatch.setattr(BenchmarkRunner, 'run', execute)
    assert cli.main(['--bench', benchmark, '--warmup', '0', '--duration', '0.01']) == code
    captured = capsys.readouterr()
    expected_calls = list(BENCHMARKS) if benchmark == 'all' else [benchmark]
    if status != 'completed':
        expected_calls = expected_calls[:1]
    assert calls == expected_calls
    text = captured.out if status == 'completed' else captured.err
    assert bool(captured.out) == (status == 'completed')
    assert bool(captured.err) == (status != 'completed')
    assert sum(line.startswith('Pi equivalent') for line in text.splitlines()) == len(expected_calls)
    if benchmark == 'all':
        assert 'Full Pi equivalent : ' + ('2 × pi' if status == 'completed' else '—') in text  # noqa: RUF001
    else:
        assert 'Full Pi equivalent' not in text
    for name in expected_calls:
        assert name in text
        if status == 'completed':
            assert ('0.5 \u00d7 pi' if name == 'single_core' else '8 \u00d7 pi') in text
        else:
            assert '—' in text


@pytest.mark.parametrize('benchmark', BENCHMARKS)
def test_short_native_run_uses_recorded_denominator_and_requested_runtime_options(benchmark: str) -> None:
    result = run(benchmark, warmup_seconds=0.0, measurement_seconds=0.02)
    assert_short_native_result_references_and_multiplier(result)
    assert (result.warmup_seconds, result.measurement_seconds) == (0.0, 0.02)
