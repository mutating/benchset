"""Observable contracts that must survive moving wrapper decisions into Rust."""

import math
from typing import List

import pytest

from benchset import BenchmarkRunner, Progress, run


@pytest.mark.parametrize(('name', 'warmup', 'measurement', 'reason'), [
    ('unknown', -1, 0, 'unknown benchmark: unknown'),
    ('', 0, 1, 'unknown benchmark: '),
    ('неизвестный\x00benchmark', 0, 1, 'unknown benchmark: неизвестный\x00benchmark'),
    ('\ud800', 0, 1, 'unknown benchmark: \ud800'),
    ('single_core\udfff', 0, 1, 'unknown benchmark: single_core\udfff'),
    ('single_core', -1, 0, 'warmup_seconds must be finite and nonnegative'),
    ('multi_core', math.nan, math.inf, 'warmup_seconds must be finite and nonnegative'),
    ('single_core', 0, -0.0, 'measurement_seconds must be finite and positive'),
    ('multi_core', -0.0, math.nan, 'measurement_seconds must be finite and positive'),
])
@pytest.mark.parametrize('reusable', [False, True])
def test_validation_preserves_exact_errors_precedence_and_requested_values(
    name: str, warmup: float, measurement: float, reason: str, reusable: bool,
) -> None:
    """Keep unknown-name/warmup/measurement precedence and lossless diagnostics.

    Embedded NUL, UTF-8 and lone surrogates guard the string ABI against
    truncation or encoding failures. Rejected options have no callbacks, and
    requested NaN/signed zero survive.
    """
    events: List[Progress] = []
    execute = BenchmarkRunner().run if reusable else run
    result = execute(name, warmup_seconds=warmup, measurement_seconds=measurement, on_progress=events.append)
    assert result.benchmark == name
    assert result.status == 'failed'
    assert result.reason == reason
    assert result.score is result.pi_equivalent is None
    assert not events
    for actual, requested in [(result.warmup_seconds, warmup), (result.measurement_seconds, measurement)]:
        if math.isnan(requested):
            assert math.isnan(actual)
        else:
            assert actual == requested
            assert math.copysign(1, actual) == math.copysign(1, requested)
