#include "benchset.h"
#include <assert.h>
#include <stddef.h>
#include <stdio.h>

_Static_assert(sizeof(BenchsetAggregatedScores) == 16, "aggregate layout");
_Static_assert(sizeof(BenchsetSuiteResult) == 32, "suite layout");
_Static_assert(offsetof(BenchsetSuiteProgress, progress) == 8, "progress layout");

/** Prevent public-header narrowing from silently truncating indices at the ABI boundary. */
_Static_assert(
    _Generic(&benchset_suite_sample,
             int32_t (*)(const BenchsetSuiteHandle *, uint64_t, uint64_t, BenchsetSample *): 1,
             default: 0),
    "suite sample indices are uint64_t");

static void cancel(const BenchsetSuiteProgress *progress, void *handle) {
    assert(progress->benchmark == BENCHSET_SINGLE_CORE);
    benchset_suite_cancel((BenchsetSuiteHandle *)handle);
}

/** Checks suite layout, getters and cancellation; benchmark override must leave const config unchanged. */
int main(void) {
    BenchsetConfig config;
    assert(benchset_default_config(&config));
    config.benchmark = 999; /* Ignored by the suite entrypoint. */
    config.warmup_seconds = 0;
    config.measurement_seconds = 0.1;
    BenchsetSuiteHandle *handle = benchset_suite_create();
    assert(handle);
    uint32_t status = benchset_run_all(handle, &config, NULL, NULL);
    assert(config.benchmark == 999);
    BenchsetSuiteResult suite;
    assert(benchset_suite_result(handle, &suite, sizeof(suite)));
    double product = 1;
    for (uint64_t i = 0; i < suite.result_count; ++i) {
        BenchsetResultV3 result;
        assert(benchset_suite_result_at(handle, i, &result, sizeof(result)));
        assert(suite.results[i].base.pi_equivalent == result.base.pi_equivalent);
        assert(result.base.base.benchmark == i + 1);
        assert(benchset_suite_reason(handle, i));
        assert(result.reference_machine && result.reference_run);
        if (result.base.base.status == BENCHSET_COMPLETED) {
            BenchsetSample sample;
            assert(benchset_suite_sample(handle, i, 0, &sample));
            assert(sample.iterations > 0);
            product *= result.base.pi_equivalent;
        }
    }
    if (status == BENCHSET_COMPLETED) {
        assert(suite.result_count == 2);
        assert(suite.aggregated_scores.has_pi_equivalent);
        double full = suite.aggregated_scores.pi_equivalent;
        double error = full * full / product - 1;
        assert(full > 0 && error > -1e-12 && error < 1e-12);
    } else {
        assert(!suite.aggregated_scores.has_pi_equivalent);
    }
    benchset_suite_free(handle);
    handle = benchset_suite_create();
    assert(handle);
    config.warmup_seconds = 10;
    assert(benchset_run_all(handle, &config, cancel, handle) == BENCHSET_CANCELLED);
    assert(config.benchmark == 999);
    assert(benchset_suite_result(handle, &suite, sizeof(suite)));
    assert(suite.result_count == 1 && !suite.aggregated_scores.has_pi_equivalent);
    benchset_suite_free(handle);
    puts("C full suite smoke passed");
    return 0;
}
