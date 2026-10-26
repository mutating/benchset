"""Run public typing examples so they cannot silently become stale."""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(('fixture', 'expected_status'), [
    ('reference_positive.py', 0), ('reference_negative.py', 1),
])
def test_result_reference_public_type_contract(
    tmp_path: Path, fixture: str, expected_status: int,
) -> None:
    source = Path(__file__).with_name(fixture)
    result = subprocess.run([
        sys.executable, '-m', 'mypy', '--strict', '--disallow-any-expr',
        '--follow-imports=silent', '--show-error-codes', '--no-error-summary',
        '--cache-dir', str(tmp_path / 'mypy-cache'), str(source),
    ], capture_output=True, text=True, check=False)
    assert result.returncode == expected_status, result.stdout + result.stderr
    assert not result.stderr
    if expected_status == 0:
        assert not result.stdout
    else:
        errors = result.stdout.splitlines()
        assert len(errors) == 5, result.stdout
        assert sum('[assignment]' in error for error in errors) == 2
        assert sum('[arg-type]' in error for error in errors) == 2
        assert any('"reference_machine"' in error and 'read-only' in error and '[misc]' in error for error in errors)


def test_positive_result_reference_consumer_runs_without_native_code() -> None:
    program = """
import ctypes
import runpy
def forbidden(*args, **kwargs):
    raise AssertionError('typed metadata consumer must not load native code')
ctypes.CDLL = forbidden
runpy.run_module('tests.typing.reference_positive', run_name='__main__')
"""
    result = subprocess.run([sys.executable, '-c', program], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert not result.stdout
    assert not result.stderr
