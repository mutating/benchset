/* Frozen ABI-1 declarations with the additive V2 result format; never include benchset.h. */
#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

typedef struct BenchsetHandle BenchsetHandle;
typedef struct {
    uint32_t abi_version, struct_size, benchmark, reserved;
    double warmup_seconds, measurement_seconds;
} LegacyConfig;
typedef struct {
    uint32_t abi_version, status, benchmark, workers;
    double score, warmup_seconds, measurement_seconds, elapsed_seconds, variation_percent;
    uint64_t iterations, checksum, sample_count;
    uint32_t unstable, has_variation;
} LegacyResult;
typedef struct {
    LegacyResult base;
    double pi_equivalent;
    uint32_t has_pi_equivalent;
} LegacyResultV2;
typedef struct {
    const char *name, *short_name, *os_name, *os_version, *cpu_architecture;
    uint64_t memory_bytes;
    uint32_t cpu_cores;
} LegacyMachine;
typedef struct {
    double started_at_unix_seconds, start_temperature_c, end_temperature_c, warmup_seconds;
    uint64_t result_count;
} LegacyRun;
typedef struct {
    uint32_t phase, workers;
    double fraction, phase_elapsed_seconds, phase_duration_seconds;
    uint64_t iterations;
} LegacyProgress;
typedef void (*LegacyCallback)(const LegacyProgress *, void *);
extern uint32_t benchset_abi_version(void);
extern int32_t benchset_default_config(LegacyConfig *);
extern BenchsetHandle *benchset_create(void);
extern uint32_t benchset_run(BenchsetHandle *, const LegacyConfig *, LegacyCallback, void *);
extern void benchset_cancel(BenchsetHandle *);
extern int32_t benchset_result(const BenchsetHandle *, LegacyResult *);
extern int32_t benchset_result_v2(const BenchsetHandle *, LegacyResultV2 *, uint32_t);
extern int32_t benchset_reference_machine(LegacyMachine *, uint32_t);
extern int32_t benchset_reference_run(LegacyRun *, uint32_t);
extern const char *benchset_reason(const BenchsetHandle *);
extern void benchset_free(BenchsetHandle *);

_Static_assert(sizeof(LegacyConfig) == 32, "ABI-1 config");
_Static_assert(sizeof(LegacyResult) == 88, "ABI-1 legacy result");
_Static_assert(sizeof(LegacyResultV2) == 104, "V2 extended result");
_Static_assert(offsetof(LegacyResultV2, pi_equivalent) == 88, "V2 ratio position");
_Static_assert(offsetof(LegacyResultV2, has_pi_equivalent) == 96, "V2 presence position");
_Static_assert(sizeof(LegacyMachine) == 56 && sizeof(LegacyRun) == 40, "reference metadata");

static void same_prefix(const LegacyResult *a, const LegacyResult *b) {
#define SAME(field) assert(a->field == b->field)
    SAME(abi_version); SAME(status); SAME(benchmark); SAME(workers); SAME(score);
    SAME(warmup_seconds); SAME(measurement_seconds); SAME(elapsed_seconds);
    SAME(variation_percent); SAME(iterations); SAME(checksum); SAME(sample_count);
    SAME(unstable); SAME(has_variation);
#undef SAME
}

int main(void) {
    assert(benchset_abi_version() == 1);
    LegacyMachine machine;
    LegacyRun reference;
    assert(benchset_reference_machine(&machine, sizeof(machine)) == 1);
    assert(benchset_reference_run(&reference, sizeof(reference)) == 1);
    assert(strcmp(machine.short_name, "pi") == 0);
    assert(machine.memory_bytes == UINT64_C(8589934592) && machine.cpu_cores == 4);
    assert(reference.result_count == 2 && reference.warmup_seconds == 1248.0);
    const char *saved_name = machine.name;
    const double scores[] = {47231.227029007096, 181953.17419139884};
    for (uint32_t workload = 1; workload <= 2; ++workload) {
        for (uint32_t status = 0; status <= 2; ++status) {
            BenchsetHandle *handle = benchset_create();
            assert(handle != NULL);
            LegacyConfig config;
            assert(benchset_default_config(&config) == 1);
            assert(config.abi_version == 1 && config.struct_size == sizeof(config));
            config.benchmark = workload;
            config.warmup_seconds = 0;
            config.measurement_seconds = 0.025;
            if (status == 1) benchset_cancel(handle);
            if (status == 2) config.reserved = 1;
            struct { uint64_t before; LegacyResult result; uint64_t after[4]; } v1;
            struct { uint64_t before; LegacyResultV2 result; uint64_t after[4]; } v2;
            v1.before = v2.before = UINT64_C(0x123456789abcdef0);
            for (unsigned i = 0; i < 4; ++i) v1.after[i] = v2.after[i] = UINT64_C(0xfedcba9876543210);
            memset(&v2.result, 0xa5, sizeof(v2.result));
            unsigned char absent[sizeof(v2.result)];
            memcpy(absent, &v2.result, sizeof(absent));
            assert(benchset_result_v2(handle, &v2.result, sizeof(v2.result)) == 0);
            assert(memcmp(absent, &v2.result, sizeof(absent)) == 0);
            const uint32_t actual = benchset_run(handle, &config, NULL, NULL);
            assert(actual == status || (status == 0 && actual == 2));
            assert(benchset_result(handle, &v1.result) == 1);
            assert(benchset_result_v2(handle, &v2.result, sizeof(v2.result)) == 1);
            assert(v1.before == UINT64_C(0x123456789abcdef0));
            assert(v2.before == UINT64_C(0x123456789abcdef0));
            for (unsigned i = 0; i < 4; ++i) {
                assert(v1.after[i] == UINT64_C(0xfedcba9876543210));
                assert(v2.after[i] == UINT64_C(0xfedcba9876543210));
            }
            same_prefix(&v1.result, &v2.result.base);
            assert(v2.result.base.abi_version == 1 && v2.result.base.benchmark == workload);
            assert(v2.result.base.status == actual);
            if (actual == 0) {
                assert(v2.result.base.score > 0 && v2.result.has_pi_equivalent != 0);
                assert(v2.result.pi_equivalent == v2.result.base.score / scores[workload - 1]);
                assert(strcmp(benchset_reason(handle), "") == 0);
            } else {
                assert(v2.result.base.score == 0 && v2.result.has_pi_equivalent == 0);
                assert(v2.result.pi_equivalent == 0);
                if (status == 0) {
                    /* The ABI permits a short window with no scheduled work. */
                    assert(strcmp(benchset_reason(handle), "measurement window was too short to complete any work") == 0);
                    assert(v2.result.base.iterations == 0 || v2.result.base.elapsed_seconds == 0);
                } else {
                    assert(strlen(benchset_reason(handle)) > 0);
                }
            }
            benchset_free(handle);
            assert(strcmp(saved_name, "Raspberry Pi 5 Model B Rev 1.0") == 0);
        }
    }
    puts("legacy V2 result consumer remains compatible");
    return 0;
}
