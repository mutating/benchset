/* Public C consumer: compile independently against the installed C header. */
#include "benchset.h"
#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

_Static_assert(BENCHSET_ABI_VERSION == 1u, "ABI version");
_Static_assert(BENCHSET_SINGLE_CORE == 1u && BENCHSET_MULTI_CORE == 2u, "benchmark wire values");
_Static_assert(BENCHSET_COMPLETED == 0u && BENCHSET_CANCELLED == 1u && BENCHSET_FAILED == 2u,
               "status wire values");
_Static_assert(BENCHSET_WARMUP == 1u && BENCHSET_MEASUREMENT == 2u &&
               BENCHSET_PHASE_COMPLETED == 3u && BENCHSET_PHASE_CANCELLED == 4u &&
               BENCHSET_PHASE_FAILED == 5u, "progress phase wire values");
_Static_assert(sizeof(BenchsetConfig) == 32, "config layout");
_Static_assert(sizeof(BenchsetProgress) == 40, "progress layout");
_Static_assert(sizeof(BenchsetResult) == 88, "legacy result layout");
_Static_assert(sizeof(BenchsetResultV2) == 104, "extended result layout");
_Static_assert(offsetof(BenchsetResultV2, base) == 0, "legacy prefix");
_Static_assert(offsetof(BenchsetResultV2, pi_equivalent) == 88, "ratio offset");
_Static_assert(offsetof(BenchsetResultV2, has_pi_equivalent) == 96, "presence offset");
_Static_assert(sizeof(BenchsetResultV3) == 120 && _Alignof(BenchsetResultV3) == 8, "V3 layout");
_Static_assert(offsetof(BenchsetResultV3, base) == 0, "V2 prefix");
_Static_assert(offsetof(BenchsetResultV3, reference_machine) == 104, "machine pointer offset");
_Static_assert(offsetof(BenchsetResultV3, reference_run) == 112, "run pointer offset");
_Static_assert(sizeof(BenchsetReferenceMachine) == 56, "machine layout");
_Static_assert(offsetof(BenchsetReferenceMachine, memory_bytes) == 40, "RAM offset");
_Static_assert(offsetof(BenchsetReferenceMachine, cpu_cores) == 48, "cores offset");
_Static_assert(sizeof(BenchsetReferenceRun) == 40, "run layout");
_Static_assert(offsetof(BenchsetReferenceRun, result_count) == 32, "count offset");
_Static_assert(sizeof(BenchsetReferenceBenchmarkResult) == 16, "reference result layout");
_Static_assert(sizeof(BenchsetSample) == 32, "sample layout");
_Static_assert(offsetof(BenchsetSample, elapsed_seconds) == 0 &&
               offsetof(BenchsetSample, duration_seconds) == 8 &&
               offsetof(BenchsetSample, iterations) == 16 &&
               offsetof(BenchsetSample, iterations_per_second) == 24, "sample field offsets");

static void assert_views(const BenchsetReferenceMachine *machine, const BenchsetReferenceRun *run) {
    assert(machine != NULL && run != NULL);
    BenchsetReferenceMachine copied_machine;
    BenchsetReferenceRun copied_run;
    assert(benchset_reference_machine(&copied_machine, sizeof(copied_machine)) == 1);
    assert(benchset_reference_run(&copied_run, sizeof(copied_run)) == 1);
    assert(machine->name == copied_machine.name);
    assert(machine->short_name == copied_machine.short_name);
    assert(machine->os_name == copied_machine.os_name);
    assert(machine->os_version == copied_machine.os_version);
    assert(machine->cpu_architecture == copied_machine.cpu_architecture);
    assert(strcmp(machine->name, "Raspberry Pi 5 Model B Rev 1.0") == 0);
    assert(strcmp(machine->short_name, "pi") == 0);
    assert(strcmp(machine->os_name, "Ubuntu Server") == 0);
    assert(strcmp(machine->os_version, "26.04 LTS") == 0);
    assert(strcmp(machine->cpu_architecture, "aarch64") == 0);
    assert(machine->memory_bytes == copied_machine.memory_bytes && machine->memory_bytes == UINT64_C(8589934592));
    assert(machine->cpu_cores == copied_machine.cpu_cores && machine->cpu_cores == 4);
    assert(run->started_at_unix_seconds == copied_run.started_at_unix_seconds);
    assert(run->started_at_unix_seconds == 1792767590.517391);
    assert(run->start_temperature_c == copied_run.start_temperature_c && run->start_temperature_c == 41.35);
    assert(run->end_temperature_c == copied_run.end_temperature_c && run->end_temperature_c == 66.65);
    assert(run->warmup_seconds == copied_run.warmup_seconds && run->warmup_seconds == 1248.0);
    assert(run->result_count == copied_run.result_count && run->result_count == 2);
    copied_machine.cpu_cores = 0;
    copied_run.result_count = 0;
    assert(machine->cpu_cores == 4 && run->result_count == 2);
}

static void unavailable_v3(const BenchsetHandle *handle, uint32_t size) {
    BenchsetResultV3 result;
    unsigned char before[sizeof(result)];
    memset(&result, 0xa5, sizeof(result));
    memcpy(before, &result, sizeof(before));
    assert(benchset_result_v3(handle, &result, size) == 0);
    assert(memcmp(before, &result, sizeof(before)) == 0);
}

static void progress(const BenchsetProgress *value, void *raw) {
    unsigned *calls = raw;
    BenchsetReferenceMachine machine;
    BenchsetReferenceRun run;
    BenchsetReferenceBenchmarkResult result;
    assert(value != NULL);
    assert(benchset_reference_machine(&machine, sizeof(machine)) == 1);
    assert(benchset_reference_run(&run, sizeof(run)) == 1);
    assert(benchset_reference_result(1, &result, sizeof(result)) == 1);
    assert(strcmp(machine.short_name, "pi") == 0);
    assert(run.result_count == 2);
    assert(strcmp(result.benchmark, "multi_core") == 0);
    *calls += 1;
}

int main(void) {
    BenchsetReferenceMachine machine;
    BenchsetReferenceRun reference;
    BenchsetReferenceBenchmarkResult calibration[2];
    assert(benchset_abi_version() == BENCHSET_ABI_VERSION);
    assert(benchset_version() != NULL);
    assert(benchset_reference_machine(&machine, sizeof(machine)) == 1);
    assert(strcmp(machine.name, "Raspberry Pi 5 Model B Rev 1.0") == 0);
    assert(strcmp(machine.short_name, "pi") == 0);
    assert(strcmp(machine.os_name, "Ubuntu Server") == 0);
    assert(strcmp(machine.os_version, "26.04 LTS") == 0);
    assert(strcmp(machine.cpu_architecture, "aarch64") == 0);
    assert(machine.memory_bytes == UINT64_C(8589934592));
    assert(machine.cpu_cores == 4);
    assert(benchset_reference_run(&reference, sizeof(reference)) == 1);
    assert(reference.started_at_unix_seconds == 1792767590.517391);
    assert(reference.start_temperature_c == 41.35);
    assert(reference.end_temperature_c == 66.65);
    assert(reference.warmup_seconds == 1248.0);
    assert(reference.result_count == 2);
    for (uint64_t index = 0; index < reference.result_count; ++index) {
        assert(benchset_reference_result(index, &calibration[index], sizeof(calibration[index])) == 1);
    }
    assert(strcmp(calibration[0].benchmark, "single_core") == 0);
    assert(strcmp(calibration[1].benchmark, "multi_core") == 0);
    assert(calibration[0].score == 47231.227029007096);
    assert(calibration[1].score == 181953.17419139884);
    const char *saved_name = machine.name;
    const char *saved_benchmark = calibration[1].benchmark;
    assert(benchset_reference_machine(NULL, sizeof(machine)) == 0);
    assert(benchset_reference_run(NULL, sizeof(reference)) == 0);
    assert(benchset_reference_result(0, NULL, sizeof(calibration[0])) == 0);
    BenchsetReferenceBenchmarkResult untouched;
    unsigned char before[sizeof(untouched)];
    memset(&untouched, 0xa5, sizeof(untouched));
    memcpy(before, &untouched, sizeof(before));
    assert(benchset_reference_result(reference.result_count, &untouched, sizeof(untouched)) == 0);
    assert(memcmp(before, &untouched, sizeof(before)) == 0);
    assert(benchset_reference_result(UINT64_MAX, &untouched, sizeof(untouched)) == 0);
    assert(memcmp(before, &untouched, sizeof(before)) == 0);
    assert(benchset_reference_result(0, &untouched, UINT32_MAX) == 0);
    assert(memcmp(before, &untouched, sizeof(before)) == 0);
    const BenchsetReferenceMachine *canonical_machine = NULL;
    const BenchsetReferenceRun *canonical_run = NULL;
    unavailable_v3(NULL, sizeof(BenchsetResultV3));
    for (uint32_t benchmark = BENCHSET_SINGLE_CORE; benchmark <= BENCHSET_MULTI_CORE; ++benchmark) {
        BenchsetHandle *handle = benchset_create();
        assert(handle != NULL);
        BenchsetConfig config;
        assert(benchset_default_config(&config) == 1);
        assert(config.abi_version == BENCHSET_ABI_VERSION);
        assert(config.struct_size == sizeof(config));
        config.benchmark = benchmark;
        config.warmup_seconds = 0;
        config.measurement_seconds = 0.025;
        BenchsetResultV2 result;
        memset(&result, 0xa5, sizeof(result));
        unsigned char absent[sizeof(result)];
        memcpy(absent, &result, sizeof(absent));
        assert(benchset_result_v2(handle, &result, sizeof(result)) == 0);
        assert(memcmp(absent, &result, sizeof(absent)) == 0);
        unavailable_v3(handle, sizeof(BenchsetResultV3));
        assert(benchset_result_v3(handle, NULL, sizeof(BenchsetResultV3)) == 0);
        unsigned calls = 0;
        const uint32_t status = benchset_run(handle, &config, progress, &calls);
        assert(status == BENCHSET_COMPLETED || status == BENCHSET_FAILED);
        assert(calls >= 2);
        assert(benchset_result_v2(handle, &result, sizeof(result)) == 1);
        assert(result.base.abi_version == BENCHSET_ABI_VERSION);
        assert(result.base.status == status);
        assert(result.base.benchmark == benchmark);
        assert(result.has_pi_equivalent == (status == BENCHSET_COMPLETED));
        if (status == BENCHSET_COMPLETED) {
            assert(result.base.score > 0);
            assert(result.pi_equivalent == result.base.score / calibration[benchmark - 1].score);
            assert(strcmp(benchset_reason(handle), "") == 0);
        } else {
            /* Descheduling may consume the entire short measurement window. */
            assert(strcmp(benchset_reason(handle), "measurement window was too short to complete any work") == 0);
            assert(result.base.iterations == 0 || result.base.elapsed_seconds == 0);
            assert(result.base.score == 0 && result.pi_equivalent == 0);
        }
        struct { uint64_t before; BenchsetResultV3 result; uint64_t after; } current;
        current.before = UINT64_C(0x123456789abcdef0);
        current.after = UINT64_C(0xfedcba9876543210);
        assert(benchset_result_v3(handle, &current.result, sizeof(current.result)) == 1);
        assert(current.before == UINT64_C(0x123456789abcdef0));
        assert(current.after == UINT64_C(0xfedcba9876543210));
        assert(current.result.base.base.score == result.base.score);
        assert(current.result.base.base.iterations == result.base.iterations);
        assert(current.result.base.pi_equivalent == result.pi_equivalent);
        assert(current.result.base.has_pi_equivalent == result.has_pi_equivalent);
        assert_views(current.result.reference_machine, current.result.reference_run);
        if (canonical_machine == NULL) {
            canonical_machine = current.result.reference_machine;
            canonical_run = current.result.reference_run;
        }
        assert(current.result.reference_machine == canonical_machine);
        assert(current.result.reference_run == canonical_run);
        const uint32_t bad_sizes[] = {0, 88, 104, 119, 121, UINT32_MAX};
        for (unsigned index = 0; index < sizeof(bad_sizes) / sizeof(bad_sizes[0]); ++index) {
            unavailable_v3(handle, bad_sizes[index]);
        }
        struct { BenchsetResultV2 result; uint64_t canary; } v2;
        v2.canary = UINT64_C(0xfedcba9876543210);
        assert(benchset_result_v2(handle, &v2.result, sizeof(v2.result)) == 1);
        assert(v2.canary == UINT64_C(0xfedcba9876543210));
        struct { BenchsetResult result; uint64_t canary; } legacy;
        legacy.canary = UINT64_C(0xfedcba9876543210);
        assert(benchset_result(handle, &legacy.result) == 1);
        assert(legacy.canary == UINT64_C(0xfedcba9876543210));
        assert(legacy.result.score == result.base.score);
        assert(legacy.result.iterations == result.base.iterations);
        if (status == BENCHSET_COMPLETED) {
            BenchsetSample sample;
            assert(benchset_sample(handle, 0, &sample) == 1);
            assert(sample.iterations > 0);
        }
        benchset_cancel(handle);
        assert(benchset_result_v2(handle, &result, sizeof(result)) == 1);
        assert(result.base.status == status);
        assert(result.has_pi_equivalent == (status == BENCHSET_COMPLETED));
        assert(benchset_result_v3(handle, &current.result, sizeof(current.result)) == 1);
        assert(current.result.reference_machine == canonical_machine);
        assert(current.result.reference_run == canonical_run);
        benchset_free(handle);
        memset(&current.result, 0, sizeof(current.result));
        assert_views(canonical_machine, canonical_run);
        assert(strcmp(saved_name, "Raspberry Pi 5 Model B Rev 1.0") == 0);
        assert(strcmp(saved_benchmark, "multi_core") == 0);
    }
    for (uint32_t workload = BENCHSET_SINGLE_CORE; workload <= BENCHSET_MULTI_CORE; ++workload) {
        for (uint32_t status = BENCHSET_CANCELLED; status <= BENCHSET_FAILED; ++status) {
            BenchsetHandle *handle = benchset_create();
            assert(handle != NULL);
            BenchsetConfig config;
            assert(benchset_default_config(&config) == 1);
            config.benchmark = workload;
            config.warmup_seconds = 0;
            config.measurement_seconds = 0.025;
            if (status == BENCHSET_CANCELLED) benchset_cancel(handle);
            else config.reserved = 1;
            assert(benchset_run(handle, &config, NULL, NULL) == status);
            BenchsetResultV3 result;
            assert(benchset_result_v3(handle, &result, sizeof(result)) == 1);
            assert(result.base.base.benchmark == workload && result.base.base.status == status);
            assert(result.base.has_pi_equivalent == 0);
            assert(result.reference_machine == canonical_machine);
            assert(result.reference_run == canonical_run);
            benchset_free(handle);
            memset(&result, 0, sizeof(result));
            assert_views(canonical_machine, canonical_run);
        }
    }
    puts("C ABI 1 reference smoke passed");
    return 0;
}
