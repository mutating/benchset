#include "benchset.h"
#include <stdio.h>

static void progress(const BenchsetProgress *p, void *context) {
    (void)context;
    printf("phase=%u progress=%.1f%%\n", p->phase, 100.0 * p->fraction);
}

int main(void) {
    if (benchset_abi_version() != BENCHSET_ABI_VERSION) return 1;
    BenchsetHandle *handle = benchset_create();
    if (!handle) return 1;
    BenchsetConfig config;
    if (!benchset_default_config(&config)) {
        benchset_free(handle);
        return 1;
    }
    /* Short smoke test. Remove overrides for sustained defaults. */
    config.warmup_seconds = 0.1;
    config.measurement_seconds = 0.2;
    uint32_t status = benchset_run(handle, &config, progress, NULL);
    BenchsetResultV3 result;
    if (benchset_result_v3(handle, &result, sizeof(result))) {
        printf("Reference: %s (%s), warmup %.0f s\n",
               result.reference_machine->name, result.reference_machine->short_name,
               result.reference_run->warmup_seconds);
    } else {
        benchset_free(handle);
        return 1;
    }
    if (status == BENCHSET_COMPLETED && result.base.has_pi_equivalent) {
        printf("benchset %s: %.3f iterations/s, %.6g x %s\n",
               benchset_version(), result.base.base.score, result.base.pi_equivalent,
               result.reference_machine->short_name);
    } else {
        fprintf(stderr, "%s\n", benchset_reason(handle));
    }
    benchset_free(handle);
    return status == BENCHSET_COMPLETED ? 0 : 1;
}
