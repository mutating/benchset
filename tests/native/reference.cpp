/* C++ linkage and consumer types are checked without private Rust declarations. */
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

static_assert(BENCHSET_ABI_VERSION == 1u, "ABI version");
static_assert(std::is_same<decltype(BenchsetReferenceMachine::memory_bytes), uint64_t>::value, "RAM is uint64_t");
static_assert(std::is_same<decltype(BenchsetResultV2::pi_equivalent), double>::value, "ratio is double");
static_assert(sizeof(BenchsetResult) == 88, "old result");
static_assert(sizeof(BenchsetResultV2) == 104, "new result");
static_assert(offsetof(BenchsetResultV2, pi_equivalent) == 88, "ratio position");
static_assert(std::is_same<decltype(BenchsetResultV3::base), BenchsetResultV2>::value, "V2 prefix");
static_assert(std::is_same<decltype(BenchsetResultV3::reference_machine), const BenchsetReferenceMachine *>::value,
              "machine pointer is const");
static_assert(std::is_same<decltype(BenchsetResultV3::reference_run), const BenchsetReferenceRun *>::value,
              "run pointer is const");
static_assert(sizeof(BenchsetResultV3) == 120 && alignof(BenchsetResultV3) == 8, "V3 result");
static_assert(offsetof(BenchsetResultV3, base) == 0, "V2 prefix position");
static_assert(offsetof(BenchsetResultV3, reference_machine) == 104, "machine position");
static_assert(offsetof(BenchsetResultV3, reference_run) == 112, "run position");

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
