"""Preserve canonical public string operands when Rust invokes host comparisons."""

import ctypes
from types import SimpleNamespace
from typing import Any, List, Type, cast

import pytest

from benchset import BENCHMARKS, BenchmarkResult, BenchmarkRunner, SuiteResult, _native


@pytest.mark.parametrize('stage', ['validation', 'execution'])
def test_public_run_name_operators_receive_the_canonical_catalog_operand(stage: str) -> None:
    """An overridden str equality may recognize the exported catalog by identity.

    Validation and execution each compared against the same canonical Python
    string before migration. A newly decoded equal string must not turn a
    valid name into an unknown name or select the multicore fallback.
    """
    operands: List[object] = []

    class Name(str):
        __slots__ = ()
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
            operands.append(other)
            current_stage = 'validation' if len(operands) == 1 else 'execution'
            return bool(str.__eq__(self, other)) and (
                current_stage != stage or other is BENCHMARKS[0]
            )

    runner = BenchmarkRunner()
    result = runner.run(Name('single_core'), warmup_seconds=0, measurement_seconds=.01,
                        on_progress=lambda _progress: runner.cancel())
    assert result.status == 'cancelled'
    assert len(operands) == 2
    assert operands[0 if stage == 'validation' else 1] is BENCHMARKS[0]


@pytest.mark.parametrize('count', [1, 2, 3])
def test_aggregate_name_operators_receive_each_canonical_prefix_operand(count: int) -> None:
    """Canonical operands remain visible at both positions before length rejection.

    The host string need not equal the catalog bytes: its overridden equality
    remains authoritative. The Rust calculator must still compare the common
    prefix before rejecting an incomplete or overlong caller tuple.
    """
    operands: List[object] = []

    class Name(str):
        __slots__ = ()
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
            operands.append(other)
            return other is BENCHMARKS[len(operands) - 1]

    results = tuple(BenchmarkResult(Name('host name'), status='completed', pi_equivalent=value)
                    for value in (4.0, 9.0, 16.0)[:count])
    equivalent = SuiteResult.from_results(results).aggregated_scores.pi_equivalent
    assert equivalent == (6.0 if count == 2 else None)
    assert len(operands) == min(count, 2)
    assert all(operand is BENCHMARKS[index] for index, operand in enumerate(operands))


@pytest.mark.parametrize('position', [0, 1])
@pytest.mark.parametrize('error_type', [ValueError, RuntimeError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('factory', ['aggregate', 'validation'])
def test_canonical_name_operator_errors_keep_identity_and_stop_at_their_position(
    position: int, error_type: Type[BaseException], factory: str,
) -> None:
    """A canonical-operand-dependent exception must escape unchanged and lazily.

    Freshly decoded strings hide the caller's exception by producing an ordinary
    mismatch. Check both aggregate positions and both validation candidates,
    including backend-looking exceptions which validation used to propagate.
    """
    original = error_type('canonical comparison failed')
    operands: List[object] = []

    class Name(str):
        __slots__ = ()
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
            operands.append(other)
            if other is BENCHMARKS[position]:
                raise original
            return factory == 'aggregate' and other is BENCHMARKS[0]

    def invoke() -> object:
        if factory == 'aggregate':
            return SuiteResult.from_results(tuple(
                BenchmarkResult(Name('host name'), status='completed', pi_equivalent=1.0)
                for _index in range(2)
            ))
        return BenchmarkRunner().run(Name('host name'), warmup_seconds=0, measurement_seconds=.01)

    with pytest.raises(error_type, match='canonical comparison failed') as caught:
        invoke()
    assert caught.value is original
    assert len(operands) == position + 1
    assert all(operand is BENCHMARKS[index] for index, operand in enumerate(operands))


@pytest.mark.parametrize('error_type', [ValueError, KeyboardInterrupt, SystemExit])
def test_execution_canonical_name_operator_errors_keep_identity(error_type: Type[BaseException]) -> None:
    """Execution preserves exceptions raised only for the canonical single-core operand.

    Let validation succeed independently, then expose the execution comparison
    without letting a wrong multicore selection consume a long benchmark run.
    """
    original = error_type('canonical execution failed')
    operands: List[object] = []

    class Name(str):
        __slots__ = ()
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
            operands.append(other)
            if len(operands) == 1:
                return True
            if other is BENCHMARKS[0]:
                raise original
            return False

    runner = BenchmarkRunner()
    with pytest.raises(error_type, match='canonical execution failed') as caught:
        runner.run(Name('single_core'), warmup_seconds=0, measurement_seconds=.01,
                   on_progress=lambda _progress: runner.cancel())
    assert caught.value is original
    assert len(operands) == 2
    assert operands[1] is BENCHMARKS[0]


@pytest.mark.parametrize('factory', ['validation', 'execution', 'aggregate'])
def test_unknown_core_name_operands_are_forwarded_without_catalog_substitution(
    monkeypatch: pytest.MonkeyPatch, factory: str,
) -> None:
    """Canonical-object reuse must leave future or injected core operands intact.

    Drive the actual host callbacks with an unknown borrowed UTF-8 name. The
    transport may reuse known metadata strings, but must not select a catalog
    position itself or silently replace an unrecognized core operand.
    """
    from benchset import _aggregate  # noqa: I001, PLC0415 — The old package has no lazy aggregate module.

    raw = b'future_core_name'
    buffer = ctypes.create_string_buffer(raw)
    operands: List[object] = []

    class Name(str):
        __slots__ = ()
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
            operands.append(other)
            return other == raw.decode('utf-8')

    def match(callback: Any, user: object, _execution: int) -> int:
        return 47 if callback(ctypes.addressof(buffer), len(raw), user) else 0

    def calculate(_names: object, _count: int, reader: Any, user: object, _output: object, _host: int) -> int:
        request = _aggregate._Input()
        request.name = ctypes.addressof(buffer)
        request.name_size = len(raw)
        assert reader(0, 5, ctypes.byref(request), user) == 1
        assert request.has_pi_equivalent == 1
        return 1

    native = _native.Native.__new__(_native.Native)
    cast(Any, native).lib = SimpleNamespace(benchset_benchmark_match=match, benchset_aggregate_read=calculate)
    if factory == 'aggregate':
        monkeypatch.setattr(_native, 'Native', lambda: native)
        assert SuiteResult.from_results((BenchmarkResult(Name('host name')),)).aggregated_scores.pi_equivalent is None
    else:
        assert native.benchmark_id(Name('host name'), execution=factory == 'execution') == 47
    assert operands == ['future_core_name']
    assert all(operands[0] is not candidate for candidate in BENCHMARKS)
