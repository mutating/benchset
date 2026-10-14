#include "benchset.h"
#include <stdio.h>

static void progress(const BenchsetProgress *p, void *context) {
    (void)context;
    printf("phase=%u progress=%.1f%%\n", p->phase, 100.0 * p->fraction);
}

int main(void) {
    BenchsetHandle *handle = benchset_create();
    if (!handle) return 1;
    BenchsetConfig config;
    benchset_default_config(&config);
    /* Short smoke test. Remove overrides for sustained defaults. */
    config.warmup_seconds = 0.1;
    config.measurement_seconds = 0.2;
    uint32_t status = benchset_run(handle, &config, progress, NULL);
    BenchsetResult result;
    if (benchset_result(handle, &result) && status == BENCHSET_COMPLETED) {
        printf("benchset %s: %.3f iterations/s\n", benchset_version(), result.score);
    } else {
        fprintf(stderr, "%s\n", benchset_reason(handle));
    }
    benchset_free(handle);
    return status == BENCHSET_COMPLETED ? 0 : 1;
}
