/* Frozen ABI-1 declarations: never include the current benchset.h. */
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

typedef struct BenchsetHandle BenchsetHandle;
typedef struct {
    uint32_t abi_version, struct_size, benchmark, reserved;
    double warmup_seconds, measurement_seconds;
} LegacyConfig;
typedef struct {
    uint32_t phase, workers;
    double fraction, phase_elapsed_seconds, phase_duration_seconds;
    uint64_t iterations;
} LegacyProgress;
typedef struct {
    uint32_t abi_version, status, benchmark, workers;
    double score, warmup_seconds, measurement_seconds, elapsed_seconds, variation_percent;
    uint64_t iterations, checksum, sample_count;
    uint32_t unstable, has_variation;
} LegacyResult;
typedef struct {
    double elapsed_seconds, duration_seconds;
    uint64_t iterations;
    double iterations_per_second;
} LegacySample;
typedef void (*LegacyCallback)(const LegacyProgress *, void *);
extern uint32_t benchset_abi_version(void);
extern int32_t benchset_default_config(LegacyConfig *);
extern BenchsetHandle *benchset_create(void);
extern uint32_t benchset_run(BenchsetHandle *, const LegacyConfig *, LegacyCallback, void *);
extern void benchset_cancel(BenchsetHandle *);
extern int32_t benchset_result(const BenchsetHandle *, LegacyResult *);
extern int32_t benchset_sample(const BenchsetHandle *, uint64_t, LegacySample *);
extern const char *benchset_reason(const BenchsetHandle *);
extern void benchset_free(BenchsetHandle *);

_Static_assert(sizeof(LegacyConfig) == 32, "ABI-1 config");
_Static_assert(sizeof(LegacyProgress) == 40, "ABI-1 progress");
_Static_assert(sizeof(LegacyResult) == 88, "ABI-1 result");
_Static_assert(sizeof(LegacySample) == 32, "ABI-1 sample");

static void progress(const LegacyProgress *value, void *raw) {
    unsigned *calls = raw;
    assert(value != NULL && value->workers > 0);
    assert(value->phase >= 1 && value->phase <= 5);
    assert(value->fraction >= 0 && value->fraction <= 1);
    assert(value->phase_elapsed_seconds >= 0 && value->phase_duration_seconds > 0);
    *calls += 1;
}

int main(void) {
    /* An unchanged old consumer checks this before allocating a handle. */
    assert(benchset_abi_version() == 1);
    for (uint32_t defaults = 0; defaults <= 1; ++defaults) {
        for (uint32_t workload = 1; workload <= 2; ++workload) {
            for (uint32_t expected = 0; expected <= 2; ++expected) {
                BenchsetHandle *handle = benchset_create();
                assert(handle != NULL);
                LegacyConfig config = {1, sizeof(LegacyConfig), workload, 0, 0, 0.025};
                if (defaults) {
                    assert(benchset_default_config(&config) == 1);
                    assert(config.abi_version == 1 && config.struct_size == sizeof(config));
                    assert(config.benchmark == 1 && config.reserved == 0);
                    assert(config.warmup_seconds == 120 && config.measurement_seconds == 60);
                    config.benchmark = workload;
                    config.warmup_seconds = 0;
                    config.measurement_seconds = 0.025;
                }
                if (expected == 1) benchset_cancel(handle);
                if (expected == 2) config.reserved = 1;
                struct { uint64_t before; LegacyResult result; uint64_t after[4]; } guarded;
                guarded.before = UINT64_C(0x123456789abcdef0);
                for (unsigned i = 0; i < 4; ++i) guarded.after[i] = UINT64_C(0xfedcba9876543210);
                unsigned calls = 0;
                const uint32_t actual = benchset_run(handle, &config, progress, &calls);
                assert(actual == expected || (expected == 0 && actual == 2));
                assert(benchset_result(handle, &guarded.result) == 1);
                assert(guarded.before == UINT64_C(0x123456789abcdef0));
                for (unsigned i = 0; i < 4; ++i) assert(guarded.after[i] == UINT64_C(0xfedcba9876543210));
                const LegacyResult saved = guarded.result;
                assert(saved.abi_version == 1 && saved.status == actual && saved.benchmark == workload);
                assert(saved.warmup_seconds == 0 && saved.measurement_seconds == 0.025);
                const char *reason = benchset_reason(handle);
                assert(reason != NULL);
                if (expected == 0) {
                    assert(calls >= 2 && saved.workers > 0);
                    if (actual == 0) {
                        assert(saved.score > 0 && saved.iterations > 0 && saved.elapsed_seconds > 0);
                        assert(saved.score == (double)saved.iterations / saved.elapsed_seconds);
                        assert(saved.sample_count > 0 && strcmp(reason, "") == 0);
                        LegacySample sample;
                        assert(benchset_sample(handle, 0, &sample) == 1);
                        assert(sample.iterations > 0 && sample.duration_seconds > 0);
                        assert(sample.iterations_per_second == (double)sample.iterations / sample.duration_seconds);
                    } else {
                        /* A short window can end before a worker is scheduled. */
                        assert(strcmp(reason, "measurement window was too short to complete any work") == 0);
                        assert(saved.iterations == 0 || saved.elapsed_seconds == 0);
                        assert(saved.score == 0);
                    }
                } else {
                    assert(calls == 0 && saved.score == 0 && saved.iterations == 0);
                    assert(saved.elapsed_seconds == 0 && saved.sample_count == 0 && saved.checksum == 0);
                    assert(saved.unstable == 0 && saved.has_variation == 0 && saved.variation_percent == 0);
                    assert(strcmp(reason, expected == 1 ? "cancelled before starting" :
                                  "reserved config field must be zero") == 0);
                }
                benchset_cancel(handle);
                assert(benchset_result(handle, &guarded.result) == 1);
                assert(memcmp(&saved, &guarded.result, sizeof(saved)) == 0);
                assert(benchset_reason(handle) == reason);
                assert(guarded.before == UINT64_C(0x123456789abcdef0));
                for (unsigned i = 0; i < 4; ++i) assert(guarded.after[i] == UINT64_C(0xfedcba9876543210));
                benchset_free(handle);
            }
        }
    }
    puts("legacy ABI 1 consumer remains compatible");
    return 0;
}
