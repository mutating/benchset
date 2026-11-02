"""Pin the explicitly chosen formatter and integer-normalization contracts."""

from typing import Any, List, Tuple, Type

import pytest

from benchset import BENCHMARKS, BenchmarkResult, BenchmarkRunner, Progress, SuiteResult
from benchset._native import Native


class _StringLookalike:
    def __getattribute__(self, name: str) -> Any:
        if name == '__class__':
            return str
        return super().__getattribute__(name)


class _Diagnostic(str):
    __slots__ = ()

    def __str__(self) -> str:
        raise AssertionError('a valid diagnostic must not be coerced')


_Probe = Tuple[BenchmarkRunner, List[Progress], List[int], List[int]]


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> _Probe:
    """Observe real native allocation/release without changing validation behavior."""
    runner = BenchmarkRunner()
    progress: List[Progress] = []
    created: List[int] = []
    released: List[int] = []
    original_create, original_free = Native.create, Native.free

    def create(native: Native, suite: bool = False) -> int:
        handle = original_create(native, True) if suite else original_create(native)
        created.append(handle)
        return handle

    def free(native: Native, handle: int, suite: bool = False) -> None:
        original_free(native, handle, True) if suite else original_free(native, handle)
        released.append(handle)

    monkeypatch.setattr(Native, 'create', create)
    monkeypatch.setattr(Native, 'free', free)
    return runner, progress, created, released


def _assert_no_work_and_reuse(probe: _Probe) -> None:
    runner, progress, created, released = probe
    assert not progress
    assert not created
    assert not released
    assert runner._active is None
    assert runner._native is None
    assert runner._handle is None
    result = runner.run('single_core', warmup_seconds=0, measurement_seconds=.01,
                        on_progress=lambda _value: runner.cancel())
    assert result.status == 'cancelled'
    assert len(created) == 1
    assert released == created
    assert runner._active is None


@pytest.mark.parametrize('diagnostic', [
    pytest.param(None, id='none'), pytest.param(False, id='false'),
    pytest.param(0, id='zero'), pytest.param(4.0, id='float'),
    pytest.param(b'text', id='bytes'), pytest.param(['text'], id='list'),
    pytest.param(_StringLookalike(), id='spoofed-str-class'),
])
@pytest.mark.parametrize(('warmup', 'duration'), [(0.0, .01), (-1.0, 0.0)])
def test_nonstring_diagnostic_results_raise_before_native_work(
    probe: _Probe, diagnostic: object, warmup: float, duration: float,
) -> None:
    """A malformed reflected formatter cannot validate or execute an unknown name.

    The chosen contract requires an actual str or str subclass, including when
    a non-string spoofs __class__. Unknown-name formatting precedes duration
    validation, so invalid windows cannot hide its TypeError. No handle or
    progress may be created; the same runner remains usable afterward.
    """
    runner, progress, _created, _released = probe
    prefixes: List[str] = []

    class Name(str):
        __slots__ = ()

        def __radd__(self, prefix: str) -> Any:
            prefixes.append(prefix)
            return diagnostic

    with pytest.raises(TypeError, match=r'^benchmark diagnostic must be a string$'):
        runner.run(Name('unknown'), warmup_seconds=warmup, measurement_seconds=duration,
                   on_progress=progress.append)
    assert prefixes == ['unknown benchmark: ']
    _assert_no_work_and_reuse(probe)


@pytest.mark.parametrize('diagnostic', [
    pytest.param('caller diagnostic', id='str'), pytest.param('', id='empty-str'),
    pytest.param(_Diagnostic('caller diagnostic'), id='str-subclass'),
    pytest.param(_Diagnostic(''), id='empty-str-subclass'),
])
@pytest.mark.parametrize(('warmup', 'duration'), [(0.0, .01), (-1.0, 0.0)])
def test_real_string_diagnostics_keep_identity_without_coercion(
    probe: _Probe, diagnostic: str, warmup: float, duration: float,
) -> None:
    """Actual strings, including empty/subclass values, remain failed diagnostics.

    Type validation must neither call str(value), reject falsy strings, nor
    replace a caller-owned diagnostic object. Invalid windows keep the original
    unknown-name precedence, with no native work and complete runner reuse.
    """
    runner, progress, _created, _released = probe
    prefixes: List[str] = []

    class Name(str):
        __slots__ = ()

        def __radd__(self, prefix: str) -> str:
            prefixes.append(prefix)
            return diagnostic

    name = Name('unknown')
    result = runner.run(name, warmup_seconds=warmup, measurement_seconds=duration,
                        on_progress=progress.append)
    assert result.status == 'failed'
    assert result.benchmark is name
    assert result.reason is diagnostic
    assert prefixes == ['unknown benchmark: ']
    _assert_no_work_and_reuse(probe)


@pytest.mark.parametrize('error_type', [TypeError, RuntimeError, OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize(('warmup', 'duration'), [(0.0, .01), (-1.0, 0.0)])
def test_formatter_errors_escape_unchanged_before_duration_checks(
    probe: _Probe, error_type: Type[BaseException], warmup: float, duration: float,
) -> None:
    """A formatter's own error retains identity, cause and context through Rust.

    The new return-type guard must not reclassify genuine formatter exceptions,
    even backend-looking errors or control exceptions. Name formatting still
    takes precedence over invalid windows and leaves no native activity.
    """
    runner, progress, _created, _released = probe
    original = error_type('formatter failed')
    context = LookupError('formatter cause')
    prefixes: List[str] = []

    class Name(str):
        __slots__ = ()

        def __radd__(self, prefix: str) -> str:
            prefixes.append(prefix)
            try:
                raise context
            except LookupError:
                raise original from context

    with pytest.raises(error_type) as caught:
        runner.run(Name('unknown'), warmup_seconds=warmup, measurement_seconds=duration,
                   on_progress=progress.append)
    assert caught.value is original
    assert original.__context__ is context
    assert original.__cause__ is context
    assert original.__suppress_context__
    assert prefixes == ['unknown benchmark: ']
    _assert_no_work_and_reuse(probe)


@pytest.mark.parametrize('position', [0, 1])
@pytest.mark.parametrize(('finite', 'log_value', 'expected'), [(4.0, 4.0, 2.0), (9.0, 4.0, 2.0), (4.0, 9.0, 3.0)])
def test_integer_subclasses_use_each_fresh_float_conversion_for_aggregation(
    position: int, finite: float, log_value: float, expected: float,
) -> None:
    """The approved numeric contract uses converted values rather than int payloads.

    An int payload of 16 converts to four and therefore contributes log(4),
    producing two with a unit peer. A changed second conversion proves that
    the logarithm reads fresh host numeric data, independently of validation.
    """
    conversions: List[float] = []
    events: List[str] = []

    class Ratio(int):
        __hash__ = int.__hash__

        def __float__(self) -> float:
            assert len(conversions) < 2
            events.append('float:' + str(len(conversions)))
            value = (finite, log_value)[len(conversions)]
            conversions.append(value)
            return value

        def __ge__(self, other: Any) -> bool:
            events.append('ge')
            return int.__ge__(self, other)

        def __eq__(self, other: object) -> bool:
            events.append('eq')
            return int.__eq__(self, other)

    values: List[float] = [1.0, 1.0]
    values[position] = Ratio(16)
    results = tuple(BenchmarkResult(name, status='completed', pi_equivalent=value)
                    for name, value in zip(BENCHMARKS, values))
    assert SuiteResult.from_results(results).aggregated_scores.pi_equivalent == pytest.approx(expected)
    assert conversions == [finite, log_value]
    assert events == ['float:0', 'ge', 'eq', 'float:1']


@pytest.mark.parametrize('position', [0, 1])
@pytest.mark.parametrize('conversion', [0, 1], ids=['validation', 'logarithm'])
@pytest.mark.parametrize('error_type', [RuntimeError, KeyboardInterrupt])
def test_integer_float_conversion_errors_keep_identity_at_both_stages(
    position: int, conversion: int, error_type: Type[BaseException],
) -> None:
    """Both required integer conversions remain fallible host primitives.

    Preserve exact errors rather than caching the validation value, suppressing
    a logarithm conversion failure, or using the integer payload instead.
    """
    original = error_type('integer conversion failed')
    calls: List[int] = []
    events: List[str] = []

    class Ratio(int):
        __hash__ = int.__hash__

        def __float__(self) -> float:
            current = len(calls)
            calls.append(current)
            events.append('float:' + str(current))
            if current == conversion:
                raise original
            return 4.0

        def __ge__(self, other: Any) -> bool:
            events.append('ge')
            return int.__ge__(self, other)

        def __eq__(self, other: object) -> bool:
            events.append('eq')
            return int.__eq__(self, other)

    values: List[float] = [1.0, 1.0]
    values[position] = Ratio(16)
    results = tuple(BenchmarkResult(name, status='completed', pi_equivalent=value)
                    for name, value in zip(BENCHMARKS, values))
    with pytest.raises(error_type) as caught:
        SuiteResult.from_results(results)
    assert caught.value is original
    assert calls == list(range(conversion + 1))
    assert events == (['float:0'] if conversion == 0 else ['float:0', 'ge', 'eq', 'float:1'])
