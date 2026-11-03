"""Final caller-handler restoration must not replace or retain an earlier error."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

import pytest

from tests.test_cli_cleanup_signals import _CLEANUP

_ROOT = Path(__file__).resolve().parents[1]


def _program(stage: str, boundary: str, latest: bool, owned: bool = False) -> str:
    """Extend the real native cleanup fixture with a final restoration signal.

    SIGTERM models a caller exception before installing SIGINT's old policy;
    SIGINT exercises the boundary just after that policy has been installed.
    The fixture records native ownership before its independent fallback teardown.
    """
    program = _CLEANUP
    replacements = {
        'remaining = 3\ncalls = 0':
            'remaining = ' + ('0' if stage == 'normal' else '3') + '\ncalls = 0\n'
            'final_restore_fired = False\n'
            'latest = ' + repr(latest) + '\n'
            'def replacement(_signum, _frame):\n    pass',
        '    calls += 1\n':
            '    calls += 1\n'
            '    if latest and final_restore_fired:\n'
            '        install(signal.SIGINT, replacement)\n',
        'previous = install(signal.SIGINT, custom)\n':
            'previous = install(signal.SIGINT, custom)\n'
            'previous_term = install(signal.SIGTERM, custom)\n',
        '    global remaining\n': '    global remaining, final_restore_fired\n',
        '    return install(signum, handler)\n\ndef allocate':
            '    final = ((normal_joined if normal else outputting)\n'
            '             and handler is custom and not final_restore_fired)\n'
            '    if final:\n'
            '        final_restore_fired = True\n'
            + ('        signal.raise_signal(signal.SIGTERM)\n' if boundary == 'before' else '')
            + '    value = install(signum, handler)\n'
            + ('    if final:\n        signal.raise_signal(signal.SIGINT)\n' if boundary == 'after' else '')
            + '    return value\n\ndef allocate',
        'restored=signal.getsignal(signal.SIGINT) is custom,':
            'restored=signal.getsignal(signal.SIGINT) is '
            '(replacement if latest and final_restore_fired else custom),',
        '    install(signal.SIGINT, previous)\n':
            '    install(signal.SIGINT, previous)\n'
            '    install(signal.SIGTERM, previous_term)\n',
        '    result.update(finished=finished.is_set(),':
            '    result.update(diagnostics=sys.stderr.getvalue(), finished=finished.is_set(),',
    }
    for old, new in replacements.items():
        assert program.count(old) == 1
        program = program.replace(old, new)
    if owned:
        replacements = {
            'previous_term = install(signal.SIGTERM, custom)':
                'policy_selected = Event()\n'
                'def select_policy(_signum, _frame):\n'
                '    install(signal.SIGINT, replacement)\n'
                '    policy_selected.set()\n'
                'previous_term = install(signal.SIGTERM, select_policy)',
            '        entered.set()\n':
                '        signal.raise_signal(signal.SIGTERM)\n'
                '        entered.set()\n',
            "            assert entered.wait(5), 'native cleanup was never entered'\n":
                "            assert entered.wait(5), 'native cleanup was never entered'\n"
                "            assert policy_selected.wait(5), 'caller policy was never selected'\n",
        }
        for old, new in replacements.items():
            assert program.count(old) == 1
            program = program.replace(old, new)
    return program


def _invoke(  # noqa: PLR0913 — Independent caller-policy and exception-state controls.
    stage: str, boundary: str, primary: str, *, latest: bool = False, owned: bool = False, inherited: bool = False,
) -> Dict[str, object]:
    environment = os.environ.copy()
    environment.pop('PYTHONPATH', None)
    arguments: List[str] = [primary, 'GeneratorExit', 'yes' if inherited else 'no',
                           'immediate' if stage == 'body' else 'normal']
    process = subprocess.run(
        [sys.executable, '-c', _program(stage, boundary, latest, owned), *arguments],
        cwd=_ROOT, env=environment, capture_output=True, text=True, check=False, timeout=15,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert not process.stderr
    result = json.loads(process.stdout)
    assert result['cli_path'] == str((_ROOT / 'benchset/cli.py').resolve())
    assert result['finished']
    assert not result['active']
    assert not result['native']
    assert result['handle'] is None
    assert result['live'] == 0
    assert not any(result['workers'])
    assert result['restored']
    return result


@pytest.mark.parametrize('stage', ['body', 'guard', 'normal'])
@pytest.mark.parametrize('boundary', ['before', 'after'])
@pytest.mark.parametrize('primary', ['RuntimeError', 'KeyboardInterrupt', 'OSError'])
def test_final_caller_handler_restoration_preserves_the_first_error(
    stage: str, boundary: str, primary: str,
) -> None:
    """A later restoration signal cannot replace a body or saved guard error.

    Also cover the first error arising only at normal final restoration: it
    must propagate after restoring caller policy, rather than being swallowed.
    No native handle, active runner or CLI worker may survive any exit.
    """
    result = _invoke(stage, boundary, primary)
    if primary == 'KeyboardInterrupt':
        assert result['code'] == 130
    elif primary == 'OSError':
        assert result['code'] == 1
        assert 'primary caller signal' in str(result['diagnostics'])
        assert 'secondary caller signal' not in str(result['diagnostics'])
    else:
        assert result['exception'] == primary
        assert result['original']


@pytest.mark.parametrize('stage', ['body', 'guard', 'normal'])
@pytest.mark.parametrize('boundary', ['before', 'after'])
def test_final_caller_handler_restoration_does_not_retain_its_saved_error(
    stage: str, boundary: str,
) -> None:
    """Clear saved errors on every restoration exit with cyclic GC disabled.

    A saved error's traceback contains the CLI frame and its error list. Leaving
    that error in the list after a secondary exception creates a self-retaining
    cycle, even though all native handles were already freed.
    """
    result = _invoke(stage, boundary, 'WeakError')
    assert result['exception'] == 'WeakError'
    assert result['original']
    assert result['error_collected']


@pytest.mark.parametrize('stage', ['body', 'guard', 'normal'])
@pytest.mark.parametrize('boundary', ['before', 'after'])
def test_final_caller_handler_restoration_preserves_a_policy_selected_by_the_caller(
    stage: str, boundary: str,
) -> None:
    """A caller handler can select a new SIGINT policy before raising its error.

    Retrying an interrupted final installation must retain that latest caller
    choice instead of overwriting it with the policy saved on CLI entry.
    """
    result = _invoke(stage, boundary, 'RuntimeError', latest=True)
    assert result['exception'] == 'RuntimeError'
    assert result['original']


def test_final_restoration_does_not_adopt_a_policy_changed_during_owned_native_cleanup() -> None:
    """Only a released caller policy can replace the saved CLI entry policy.

    SIGTERM selects another SIGINT handler while real native release is blocked
    and the CLI still owns cleanup. The first final installation must restore
    the saved caller handler; only retries of an interrupted final installation
    may retain a subsequent caller choice.
    """
    result = _invoke('body', 'owned', 'RuntimeError', owned=True)
    assert result['exception'] == 'RuntimeError'
    assert result['original']


@pytest.mark.parametrize('boundary', ['before', 'after'])
@pytest.mark.parametrize('primary', ['RuntimeError', 'KeyboardInterrupt'])
def test_final_restoration_keeps_a_new_error_when_the_caller_is_in_an_unrelated_except_block(
    boundary: str, primary: str,
) -> None:
    """Inherited except context is not an error currently unwinding this CLI.

    The first error occurs only during final restoration after a normal join.
    Looking at sys.exc_info() instead of explicit completion state would confuse
    the caller's handled LookupError with a pending CLI failure and hide it.
    """
    result = _invoke('normal', boundary, primary, inherited=True)
    if primary == 'KeyboardInterrupt':
        assert result['code'] == 130
    else:
        assert result['exception'] == 'RuntimeError'
        assert result['original']
