"""Exercise pip install from source in a fresh venv without Cargo on PATH."""

import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Shared by the source-install and unpacked-wheel integration checks. This runs
# in the consumer interpreter, without importing anything from this checkout.
INSTALLED_API_CHECK = '''
import ctypes
import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import get_type_hints

load_native = ctypes.CDLL
def forbidden(*args, **kwargs):
    raise AssertionError('reference inspection must not load native code')
ctypes.CDLL = forbidden
import benchset
from benchset import (
    PI_REFERENCE_MACHINE, PI_REFERENCE_RUN, BenchmarkResult,
    ReferenceMachine, ReferenceRun, run, run_all,
)

assert Path(benchset.__file__).resolve().parent.parent != Path.cwd()
assert Path(benchset.__file__).with_name('py.typed').is_file()
annotations = get_type_hints(BenchmarkResult)
assert annotations['reference_machine'] is ReferenceMachine
assert annotations['reference_run'] is ReferenceRun
for result in (BenchmarkResult('single_core'), run('unknown')):
    assert result.reference_machine is PI_REFERENCE_MACHINE
    assert result.reference_run is PI_REFERENCE_RUN
    payload = json.loads(json.dumps(asdict(result), allow_nan=False))
    assert payload['reference_machine'] == asdict(PI_REFERENCE_MACHINE)
    assert payload['reference_run']['started_at_unix_seconds'] == 1792767590.517391
    assert 'started_at' not in payload['reference_run']
ctypes.CDLL = load_native
machine = asdict(PI_REFERENCE_MACHINE)
assert machine == {
    'name': 'Raspberry Pi 5 Model B Rev 1.0', 'short_name': 'pi',
    'os_name': 'Ubuntu Server', 'os_version': '26.04 LTS',
    'cpu_architecture': 'aarch64', 'memory_bytes': 8589934592, 'cpu_cores': 4,
}
reference = PI_REFERENCE_RUN
assert reference.started_at_unix_seconds == 1792767590.517391
assert reference.start_temperature_c == 41.35
assert reference.end_temperature_c == 66.65
assert reference.warmup_seconds == 1248.0
expected = {'single_core': 47231.227029007096, 'multi_core': 181953.17419139884}
assert {value.benchmark: value.score for value in reference.results} == expected
results = [run(name, warmup_seconds=0, measurement_seconds=0.05) for name in expected]
assert tuple(value.benchmark for value in results) == tuple(expected)
for result in results:
    if result.success:
        assert result.score is not None
        assert isinstance(result.pi_equivalent, float)
        assert math.isfinite(result.pi_equivalent) and result.pi_equivalent > 0
        assert result.pi_equivalent == result.score / expected[result.benchmark]
    else:
        assert result.status == 'failed', result.reason
        assert result.reason == 'measurement window was too short to complete any work'
        assert result.iterations == result.checksum == 0
        assert result.score is result.pi_equivalent is result.variation_percent is None
        assert not result.unstable
        assert all(sample.iterations == sample.iterations_per_second == 0 for sample in result.samples)
    assert result.reference_machine is PI_REFERENCE_MACHINE
    assert result.reference_run is PI_REFERENCE_RUN
    payload = json.loads(json.dumps(asdict(result), allow_nan=False))
    assert payload['reference_machine'] == machine
    assert payload['reference_run'] == json.loads(json.dumps(asdict(reference)))
print('Installed API: result references, JSON, typing and pi equivalents verified')
'''


def verify_cli(output, errors='', returncode=0, measurement_seconds=0.05):
    """Check completed results or the exact no-work failure of a short window."""
    output, errors = output.replace('\r\n', '\n'), errors.replace('\r\n', '\n')
    names = ('single_core', 'multi_core')
    completed = output.rstrip().split('\n\n') if output else []
    failed = errors.rstrip().split('\n\n') if errors else []
    if ('\r' in output + errors or '\x1b' in output + errors
            or (failed and (returncode != 1 or len(failed) != 1 or len(completed) >= len(names)))
            or (not failed and (returncode != 0 or len(completed) != len(names)))):
        raise RuntimeError('the installed CLI produced invalid streams, exit status or suite order')
    expected_fields = {
        'Benchmark', 'Version', 'Status', 'Success', 'Reason', 'Score', 'Unit',
        'Reference machine', 'Pi equivalent', 'Workers', 'Warmup seconds',
        'Measurement seconds', 'Actual measurement seconds', 'Iterations',
        'Checksum', 'Variation percent', 'Unstable',
    }
    try:
        for index, section in enumerate(completed + failed):
            table, samples = section.split('Samples:\n')
            fields = dict(line.split(' : ', 1) for line in table.splitlines())
            fields = {key.strip(): value.strip() for key, value in fields.items()}
            if (set(fields) != expected_fields or len(fields) != len(table.splitlines())
                    or fields['Benchmark'] != names[index]
                    or fields['Reference machine'] != 'Raspberry Pi 5 Model B Rev 1.0 (pi)'
                    or fields['Unit'] != 'iterations/s' or int(fields['Workers']) < 1
                    or float(fields['Warmup seconds']) != 0
                    or float(fields['Measurement seconds']) != measurement_seconds):
                raise RuntimeError('the installed CLI produced invalid result fields')
            if index < len(completed):
                number, separator, reference = fields['Pi equivalent'].replace(' x ', ' \u00d7 ').partition(' \u00d7 ')
                ratio = float(number)
                if (fields['Status'] != 'completed' or fields['Success'] != 'True'
                        or not separator or reference != 'pi' or not math.isfinite(ratio) or ratio <= 0):
                    raise RuntimeError('the installed CLI produced an invalid completed result or pi equivalent')
            else:
                unavailable = fields['Score']
                if (fields['Status'] != 'failed' or fields['Success'] != 'False'
                        or fields['Reason'] != 'measurement window was too short to complete any work'
                        or fields['Iterations'] != '0' or fields['Checksum'] != '0'
                        or unavailable not in ('—', '-', '\\u2014')
                        or fields['Pi equivalent'] != unavailable or fields['Variation percent'] != unavailable
                        or fields['Unstable'] != 'False'):
                    raise RuntimeError('the installed CLI produced an unexpected failure')
                if samples.strip() != unavailable:
                    rows = [[cell.strip() for cell in row.split('|')] for row in samples.splitlines()]
                    if rows[0] != ['elapsed s', 'duration s', 'iterations', 'iterations/s'] or len(rows) < 2:
                        raise RuntimeError('the installed CLI produced invalid no-work samples')
                    for elapsed, duration, iterations, rate in rows[1:]:
                        if not (float(elapsed) >= 0 and float(duration) > 0 and int(iterations) == float(rate) == 0):
                            raise RuntimeError('the installed CLI produced nonzero no-work samples')
    except (IndexError, KeyError, ValueError) as error:
        raise RuntimeError('the installed CLI produced malformed results') from error


def main():
    environment = os.environ.copy()
    child_io_encoding = sys.stdout.encoding or 'utf-8'
    environment['PYTHONIOENCODING'] = child_io_encoding
    names = ('cargo.exe', 'cargo')
    # CI hosts usually ship Rust. Hide its bin directories for this test so the
    # actual PEP 517 subprocess must use the automatic bootstrap path.
    environment['PATH'] = os.pathsep.join(
        entry for entry in environment.get('PATH', '').split(os.pathsep)
        if entry and not any((Path(entry) / name).is_file() for name in names)
    )
    if shutil.which('cargo', path=environment['PATH']) is not None:
        raise RuntimeError('Cargo is still visible in the source-install test')
    with tempfile.TemporaryDirectory(prefix='benchset-install-') as temporary:
        directory = Path(temporary)
        venv = directory / 'venv'
        subprocess.run([sys.executable, '-m', 'venv', str(venv)], check=True)
        windows = os.name == 'nt'
        python = venv / ('Scripts/python.exe' if windows else 'bin/python')
        command = venv / ('Scripts/benchset.exe' if windows else 'bin/benchset')
        subprocess.run([str(python), '-m', 'pip', 'install', str(ROOT)],
                       env=environment, cwd=directory, check=True)
        # Run outside the repository so imports come from the installed wheel.
        subprocess.run([str(python), '-c', INSTALLED_API_CHECK],
                       env=environment, cwd=directory, check=True)
        for invocation in ([str(command)], [str(python), '-m', 'benchset']):
            result = subprocess.run([
                *invocation, '--warmup', '0', '--duration', '0.05',
            ], env=environment, cwd=directory, encoding=child_io_encoding, capture_output=True, check=False)
            verify_cli(result.stdout, result.stderr, result.returncode)
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
        subprocess.run([str(python), '-m', 'pip', 'check'], env=environment, cwd=directory, check=True)


if __name__ == '__main__':
    main()
