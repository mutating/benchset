"""Keep native inspection failures inside the CLI's ordinary failed-result path."""

import ctypes
from threading import current_thread
from typing import Any, Callable, List

import pytest

from benchset import __version__, cli
from benchset._native import ABI_VERSION


class _BrokenLibrary:
    def __init__(self, failure: str) -> None:
        self.failure = failure

    def __getattr__(self, name: str) -> Callable[[], Any]:
        if name == 'benchset_abi_version':
            if self.failure == 'missing-abi':
                raise AttributeError('missing initial symbol: benchset_abi_version')

            def abi() -> int:
                if self.failure == 'abi-error':
                    raise ValueError('ABI getter exploded')
                return ABI_VERSION

            return abi
        if name == 'benchset_version':
            if self.failure == 'missing-version':
                raise AttributeError('missing initial symbol: benchset_version')

            def version() -> Any:
                if self.failure == 'invalid-utf8':
                    return b'\xff'
                if self.failure == 'none-version':
                    return None
                if self.failure == 'text-version':
                    return __version__
                if self.failure == 'version-error':
                    raise ValueError('version getter exploded')
                return __version__.encode('utf-8')

            return version
        raise AssertionError('unexpected native lookup after failed initial validation: ' + name)


@pytest.mark.parametrize(('failure', 'diagnostic'), [
    ('missing-abi', 'benchset_abi_version'),
    ('missing-version', 'benchset_version'),
    ('invalid-utf8', 'utf-8'),
    ('none-version', 'NoneType'),
    ('text-version', "'str' object"),
    ('version-error', 'version getter exploded'),
    ('abi-error', 'ABI getter exploded'),
])
@pytest.mark.parametrize('benchmark', ['single_core', 'all'])
@pytest.mark.parametrize('duration', ['.01', '-1'])
def test_cli_defers_initial_native_validation_errors_to_failed_results(  # noqa: PLR0913 — Independent loader/CLI input axes.
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    failure: str, diagnostic: str, benchmark: str, duration: str,
) -> None:
    """Run real settings and worker ownership around a successfully opened bad image.

    The pre-migration CLI inspected native symbols only in its worker: missing
    ABI/version symbols, invalid version bytes and getter exceptions became a
    failed result with exit 1. Eager Rust duration validation now reaches those
    same exceptions in settings before the worker starts. Preserve the old CLI
    result boundary without changing Native/run's existing raw exception API.
    An invalid duration must keep the accepted native-load-error precedence.
    """
    attempts: List[str] = []

    def load(_path: str) -> _BrokenLibrary:
        attempts.append(current_thread().name)
        return _BrokenLibrary(failure)

    monkeypatch.setattr(ctypes, 'CDLL', load)
    assert cli.main(['--bench=' + benchmark, '--warmup=0', '--duration=' + duration]) == 1
    output, errors = capsys.readouterr()
    assert not output
    assert attempts[-1] == 'benchset-cli', 'settings must defer the failure to worker reporting'
    assert 'Benchmark                  : single_core' in errors
    assert 'Status                     : failed' in errors
    assert diagnostic in errors
    assert 'Traceback' not in errors
    assert 'usage:' not in errors
    assert '--duration must be finite and positive' not in errors
    assert errors.count('Full Pi equivalent :') == (1 if benchmark == 'all' else 0)
