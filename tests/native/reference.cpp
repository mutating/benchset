/* C++ linkage and consumer types are checked without private Rust declarations. */
#include "benchset.h"
/** Real clients can encounter the public header through several independent includes. */
#include "benchset.h"
#include <array>
#include <cassert>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <mutex>
#include <thread>
#include <type_traits>
#include <utility>
#include <vector>

/** Freeze the supported 64-bit wire layout independently of the Rust implementation.
 * Same-type field swaps preserve sizeof but make existing binaries read unrelated
 * values. Type, signedness, constness, alignment and offsets are all ABI contracts.
 */
#define CHECK_RECORD(T, size) static_assert(sizeof(T) == size && alignof(T) == 8, #T " layout")
#define CHECK_FIELD(T, field, type, offset) \
    static_assert(std::is_same<decltype(T::field), type>::value, #T "." #field " type"); \
    static_assert(offsetof(T, field) == offset, #T "." #field " offset")
static_assert(BENCHSET_ABI_VERSION == 1u, "ABI version");
static_assert(BENCHSET_SINGLE_CORE == 1u && BENCHSET_MULTI_CORE == 2u, "benchmark wire values");
static_assert(BENCHSET_COMPLETED == 0u && BENCHSET_CANCELLED == 1u && BENCHSET_FAILED == 2u, "status wire values");
static_assert(BENCHSET_WARMUP == 1u && BENCHSET_MEASUREMENT == 2u && BENCHSET_PHASE_COMPLETED == 3u
              && BENCHSET_PHASE_CANCELLED == 4u && BENCHSET_PHASE_FAILED == 5u, "phase wire values");
CHECK_RECORD(BenchsetConfig, 32);
CHECK_FIELD(BenchsetConfig, abi_version, uint32_t, 0);
CHECK_FIELD(BenchsetConfig, struct_size, uint32_t, 4);
CHECK_FIELD(BenchsetConfig, benchmark, uint32_t, 8);
CHECK_FIELD(BenchsetConfig, reserved, uint32_t, 12);
CHECK_FIELD(BenchsetConfig, warmup_seconds, double, 16);
CHECK_FIELD(BenchsetConfig, measurement_seconds, double, 24);
CHECK_RECORD(BenchsetProgress, 40);
CHECK_FIELD(BenchsetProgress, phase, uint32_t, 0);
CHECK_FIELD(BenchsetProgress, workers, uint32_t, 4);
CHECK_FIELD(BenchsetProgress, fraction, double, 8);
CHECK_FIELD(BenchsetProgress, phase_elapsed_seconds, double, 16);
CHECK_FIELD(BenchsetProgress, phase_duration_seconds, double, 24);
CHECK_FIELD(BenchsetProgress, iterations, uint64_t, 32);
CHECK_RECORD(BenchsetResult, 88);
CHECK_FIELD(BenchsetResult, abi_version, uint32_t, 0);
CHECK_FIELD(BenchsetResult, status, uint32_t, 4);
CHECK_FIELD(BenchsetResult, benchmark, uint32_t, 8);
CHECK_FIELD(BenchsetResult, workers, uint32_t, 12);
CHECK_FIELD(BenchsetResult, score, double, 16);
CHECK_FIELD(BenchsetResult, warmup_seconds, double, 24);
CHECK_FIELD(BenchsetResult, measurement_seconds, double, 32);
CHECK_FIELD(BenchsetResult, elapsed_seconds, double, 40);
CHECK_FIELD(BenchsetResult, variation_percent, double, 48);
CHECK_FIELD(BenchsetResult, iterations, uint64_t, 56);
CHECK_FIELD(BenchsetResult, checksum, uint64_t, 64);
CHECK_FIELD(BenchsetResult, sample_count, uint64_t, 72);
CHECK_FIELD(BenchsetResult, unstable, uint32_t, 80);
CHECK_FIELD(BenchsetResult, has_variation, uint32_t, 84);
CHECK_RECORD(BenchsetResultV2, 104);
CHECK_FIELD(BenchsetResultV2, base, BenchsetResult, 0);
CHECK_FIELD(BenchsetResultV2, pi_equivalent, double, 88);
CHECK_FIELD(BenchsetResultV2, has_pi_equivalent, uint32_t, 96);
CHECK_RECORD(BenchsetReferenceMachine, 56);
CHECK_FIELD(BenchsetReferenceMachine, name, const char *, 0);
CHECK_FIELD(BenchsetReferenceMachine, short_name, const char *, 8);
CHECK_FIELD(BenchsetReferenceMachine, os_name, const char *, 16);
CHECK_FIELD(BenchsetReferenceMachine, os_version, const char *, 24);
CHECK_FIELD(BenchsetReferenceMachine, cpu_architecture, const char *, 32);
CHECK_FIELD(BenchsetReferenceMachine, memory_bytes, uint64_t, 40);
CHECK_FIELD(BenchsetReferenceMachine, cpu_cores, uint32_t, 48);
CHECK_RECORD(BenchsetReferenceRun, 40);
CHECK_FIELD(BenchsetReferenceRun, started_at_unix_seconds, double, 0);
CHECK_FIELD(BenchsetReferenceRun, start_temperature_c, double, 8);
CHECK_FIELD(BenchsetReferenceRun, end_temperature_c, double, 16);
CHECK_FIELD(BenchsetReferenceRun, warmup_seconds, double, 24);
CHECK_FIELD(BenchsetReferenceRun, result_count, uint64_t, 32);
CHECK_RECORD(BenchsetReferenceBenchmarkResult, 16);
CHECK_FIELD(BenchsetReferenceBenchmarkResult, benchmark, const char *, 0);
CHECK_FIELD(BenchsetReferenceBenchmarkResult, score, double, 8);
CHECK_RECORD(BenchsetResultV3, 120);
CHECK_FIELD(BenchsetResultV3, base, BenchsetResultV2, 0);
CHECK_FIELD(BenchsetResultV3, reference_machine, const BenchsetReferenceMachine *, 104);
CHECK_FIELD(BenchsetResultV3, reference_run, const BenchsetReferenceRun *, 112);
CHECK_RECORD(BenchsetSample, 32);
CHECK_FIELD(BenchsetSample, elapsed_seconds, double, 0);
CHECK_FIELD(BenchsetSample, duration_seconds, double, 8);
CHECK_FIELD(BenchsetSample, iterations, uint64_t, 16);
CHECK_FIELD(BenchsetSample, iterations_per_second, double, 24);
CHECK_RECORD(BenchsetAggregatedScores, 16);
CHECK_FIELD(BenchsetAggregatedScores, pi_equivalent, double, 0);
CHECK_FIELD(BenchsetAggregatedScores, has_pi_equivalent, uint32_t, 8);
CHECK_RECORD(BenchsetSuiteResult, 32);
CHECK_FIELD(BenchsetSuiteResult, results, const BenchsetResultV3 *, 0);
CHECK_FIELD(BenchsetSuiteResult, result_count, uint64_t, 8);
CHECK_FIELD(BenchsetSuiteResult, aggregated_scores, BenchsetAggregatedScores, 16);
CHECK_RECORD(BenchsetSuiteProgress, 48);
CHECK_FIELD(BenchsetSuiteProgress, benchmark, uint32_t, 0);
CHECK_FIELD(BenchsetSuiteProgress, progress, BenchsetProgress, 8);
#undef CHECK_FIELD
#undef CHECK_RECORD

/** Public function and callback signatures must remain compatible with existing callers. */
using ExpectedProgressCallback = void (*)(const BenchsetProgress *, void *);
using ExpectedSuiteCallback = void (*)(const BenchsetSuiteProgress *, void *);
static_assert(std::is_same<BenchsetProgressCallback, ExpectedProgressCallback>::value, "progress callback ABI");
static_assert(std::is_same<BenchsetSuiteProgressCallback, ExpectedSuiteCallback>::value, "suite callback ABI");
#define CHECK_FUNCTION(name, result, ...) \
    static_assert(std::is_same<decltype(&name), result (*)(__VA_ARGS__)>::value, #name " signature")
CHECK_FUNCTION(benchset_abi_version, uint32_t, void);
CHECK_FUNCTION(benchset_version, const char *, void);
CHECK_FUNCTION(benchset_default_config, int32_t, BenchsetConfig *);
CHECK_FUNCTION(benchset_create, BenchsetHandle *, void);
CHECK_FUNCTION(benchset_run, uint32_t, BenchsetHandle *, const BenchsetConfig *, ExpectedProgressCallback, void *);
CHECK_FUNCTION(benchset_cancel, void, BenchsetHandle *);
CHECK_FUNCTION(benchset_result, int32_t, const BenchsetHandle *, BenchsetResult *);
CHECK_FUNCTION(benchset_result_v2, int32_t, const BenchsetHandle *, BenchsetResultV2 *, uint32_t);
CHECK_FUNCTION(benchset_result_v3, int32_t, const BenchsetHandle *, BenchsetResultV3 *, uint32_t);
CHECK_FUNCTION(benchset_reference_machine, int32_t, BenchsetReferenceMachine *, uint32_t);
CHECK_FUNCTION(benchset_reference_run, int32_t, BenchsetReferenceRun *, uint32_t);
CHECK_FUNCTION(benchset_reference_result, int32_t, uint64_t , BenchsetReferenceBenchmarkResult *, uint32_t);
CHECK_FUNCTION(benchset_sample, int32_t, const BenchsetHandle *, uint64_t , BenchsetSample *);
CHECK_FUNCTION(benchset_reason, const char *, const BenchsetHandle *);
CHECK_FUNCTION(benchset_free, void, BenchsetHandle *);
CHECK_FUNCTION(benchset_suite_create, BenchsetSuiteHandle *, void);
CHECK_FUNCTION(benchset_run_all, uint32_t, BenchsetSuiteHandle *, const BenchsetConfig *, ExpectedSuiteCallback, void *);
CHECK_FUNCTION(benchset_suite_cancel, void, BenchsetSuiteHandle *);
CHECK_FUNCTION(benchset_suite_result, int32_t, const BenchsetSuiteHandle *, BenchsetSuiteResult *, uint32_t);
CHECK_FUNCTION(benchset_suite_result_at, int32_t, const BenchsetSuiteHandle *, uint64_t , BenchsetResultV3 *, uint32_t);
CHECK_FUNCTION(benchset_suite_sample, int32_t, const BenchsetSuiteHandle *, uint64_t , uint64_t , BenchsetSample *);
CHECK_FUNCTION(benchset_suite_reason, const char *, const BenchsetSuiteHandle *, uint64_t);
CHECK_FUNCTION(benchset_suite_free, void, BenchsetSuiteHandle *);
#undef CHECK_FUNCTION

static void assert_views(const BenchsetReferenceMachine *machine, const BenchsetReferenceRun *run) {
    assert(machine != nullptr && run != nullptr);
    BenchsetReferenceMachine copied_machine{};
    BenchsetReferenceRun copied_run{};
    assert(benchset_reference_machine(&copied_machine, sizeof(copied_machine)) == 1);
    assert(benchset_reference_run(&copied_run, sizeof(copied_run)) == 1);
    assert(machine->name == copied_machine.name);
    assert(machine->short_name == copied_machine.short_name);
    assert(machine->os_name == copied_machine.os_name);
    assert(machine->os_version == copied_machine.os_version);
    assert(machine->cpu_architecture == copied_machine.cpu_architecture);
    assert(std::strcmp(machine->name, "Raspberry Pi 5 Model B Rev 1.0") == 0);
    assert(std::strcmp(machine->short_name, "pi") == 0);
    assert(std::strcmp(machine->os_name, "Ubuntu Server") == 0);
    assert(std::strcmp(machine->os_version, "26.04 LTS") == 0);
    assert(std::strcmp(machine->cpu_architecture, "aarch64") == 0);
    assert(machine->memory_bytes == copied_machine.memory_bytes && machine->memory_bytes == UINT64_C(8589934592));
    assert(machine->cpu_cores == copied_machine.cpu_cores && machine->cpu_cores == 4);
    assert(run->started_at_unix_seconds == copied_run.started_at_unix_seconds);
    assert(run->started_at_unix_seconds == 1792767590.517391);
    assert(run->start_temperature_c == copied_run.start_temperature_c && run->start_temperature_c == 41.35);
    assert(run->end_temperature_c == copied_run.end_temperature_c && run->end_temperature_c == 66.65);
    assert(run->warmup_seconds == copied_run.warmup_seconds && run->warmup_seconds == 1248.0);
    assert(run->result_count == copied_run.result_count && run->result_count == 2);
}

static std::pair<const BenchsetReferenceMachine *, const BenchsetReferenceRun *> cold_concurrent_reads() {
    const std::size_t count = 8;
    std::array<BenchsetHandle *, count> handles{};
    std::array<BenchsetResultV3, count> first{};
    // No metadata getter (including V3) has run in this process. Preparing
    // failed handles does not initialize the C metadata views or strings.
    for (auto &handle : handles) {
        handle = benchset_create();
        assert(handle != nullptr);
        assert(benchset_run(handle, nullptr, nullptr, nullptr) == BENCHSET_FAILED);
    }
    std::mutex mutex;
    std::condition_variable barrier;
    std::size_t waiting = 0;
    bool released = false;
    std::vector<std::thread> readers;
    for (std::size_t index = 0; index < count; ++index) {
        readers.emplace_back([&, index] {
            {
                std::unique_lock<std::mutex> lock(mutex);
                ++waiting;
                barrier.notify_all();
                barrier.wait(lock, [&] { return released; });
            }
            // Race both scalar entry points against V3 on their first calls.
            if (index % 3 == 0) {
                BenchsetReferenceMachine machine{};
                assert(benchset_reference_machine(&machine, sizeof(machine)) == 1);
            } else if (index % 3 == 1) {
                BenchsetReferenceRun run{};
                assert(benchset_reference_run(&run, sizeof(run)) == 1);
            }
            assert(benchset_result_v3(handles[index], &first[index], sizeof(first[index])) == 1);
            assert(first[index].base.base.status == BENCHSET_FAILED);
            assert(first[index].base.has_pi_equivalent == 0);
            for (unsigned repeat = 0; repeat < 32; ++repeat) {
                BenchsetResultV3 shared{};
                // All threads also read the same prepared handle concurrently.
                assert(benchset_result_v3(handles[0], &shared, sizeof(shared)) == 1);
                assert(shared.reference_machine == first[index].reference_machine);
                assert(shared.reference_run == first[index].reference_run);
                assert_views(shared.reference_machine, shared.reference_run);
                BenchsetReferenceBenchmarkResult entry{};
                assert(benchset_reference_result(1, &entry, sizeof(entry)) == 1);
                assert(std::strcmp(entry.benchmark, "multi_core") == 0);
                assert(entry.score == 181953.17419139884);
            }
        });
    }
    {
        std::unique_lock<std::mutex> lock(mutex);
        barrier.wait(lock, [&] { return waiting == count; });
        released = true;
    }
    barrier.notify_all();
    for (auto &reader : readers) reader.join();
    const auto machine = first[0].reference_machine;
    const auto run = first[0].reference_run;
    for (const auto &result : first) {
        assert(result.reference_machine == machine);
        assert(result.reference_run == run);
    }
    first.fill(BenchsetResultV3{});
    for (auto handle : handles) benchset_free(handle);
    assert_views(machine, run);
    return {machine, run};
}

int main() {
    // This must precede every other metadata getter in this process.
    const auto canonical = cold_concurrent_reads();
    BenchsetReferenceMachine machine{};
    BenchsetReferenceRun reference{};
    BenchsetReferenceBenchmarkResult calibration{};
    assert(benchset_abi_version() == BENCHSET_ABI_VERSION);
    assert(benchset_reference_machine(&machine, sizeof(machine)) == 1);
    assert(benchset_reference_run(&reference, sizeof(reference)) == 1);
    assert(benchset_reference_result(0, &calibration, sizeof(calibration)) == 1);
    assert(std::strcmp(machine.short_name, "pi") == 0);
    assert(machine.memory_bytes == UINT64_C(8589934592));
    assert(reference.warmup_seconds == 1248.0);
    assert(calibration.score == 47231.227029007096);
    BenchsetHandle *handle = benchset_create();
    assert(handle != nullptr);
    BenchsetConfig config{};
    assert(benchset_default_config(&config) == 1);
    config.warmup_seconds = 0;
    config.measurement_seconds = 0.025;
    const auto status = benchset_run(handle, &config, nullptr, nullptr);
    assert(status == BENCHSET_COMPLETED || status == BENCHSET_FAILED);
    BenchsetResultV3 result{};
    assert(benchset_result_v3(handle, &result, sizeof(result)) == 1);
    assert(result.base.base.status == status);
    assert(result.base.has_pi_equivalent == (status == BENCHSET_COMPLETED));
    if (status == BENCHSET_COMPLETED) {
        assert(result.base.base.score > 0);
        assert(result.base.pi_equivalent == result.base.base.score / calibration.score);
        assert(std::strcmp(benchset_reason(handle), "") == 0);
    } else {
        // Descheduling may consume the entire short measurement window.
        assert(std::strcmp(benchset_reason(handle), "measurement window was too short to complete any work") == 0);
        assert(result.base.base.iterations == 0 || result.base.base.elapsed_seconds == 0);
        assert(result.base.base.score == 0 && result.base.pi_equivalent == 0);
    }
    assert(result.reference_machine == canonical.first);
    assert(result.reference_run == canonical.second);
    benchset_free(handle);
    result = BenchsetResultV3{};
    assert_views(canonical.first, canonical.second);
    assert(std::strcmp(machine.name, "Raspberry Pi 5 Model B Rev 1.0") == 0);
    std::cout << "C++ ABI 1 reference smoke passed\n";
}
