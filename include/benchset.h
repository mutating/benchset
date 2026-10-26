#ifndef BENCHSET_H
#define BENCHSET_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

#define BENCHSET_ABI_VERSION 1u
#define BENCHSET_SINGLE_CORE 1u
#define BENCHSET_MULTI_CORE 2u
#define BENCHSET_COMPLETED 0u
#define BENCHSET_CANCELLED 1u
#define BENCHSET_FAILED 2u
#define BENCHSET_WARMUP 1u
#define BENCHSET_MEASUREMENT 2u
#define BENCHSET_PHASE_COMPLETED 3u
#define BENCHSET_PHASE_CANCELLED 4u
#define BENCHSET_PHASE_FAILED 5u

typedef struct BenchsetHandle BenchsetHandle;
typedef struct {
    uint32_t abi_version, struct_size, benchmark, reserved;
    double warmup_seconds, measurement_seconds;
} BenchsetConfig;
typedef struct {
    uint32_t phase, workers;
    double fraction, phase_elapsed_seconds, phase_duration_seconds;
    uint64_t iterations;
} BenchsetProgress;
typedef struct {
    uint32_t abi_version, status, benchmark, workers;
    double score, warmup_seconds, measurement_seconds, elapsed_seconds, variation_percent;
    uint64_t iterations, checksum, sample_count;
    uint32_t unstable, has_variation;
} BenchsetResult;
/* ABI-1 result remains 88 bytes. New consumers use the size-checked V2 getter. */
typedef struct {
    BenchsetResult base;
    double pi_equivalent; /* base.score / this benchmark's reference score. */
    uint32_t has_pi_equivalent;
} BenchsetResultV2;
typedef struct {
    const char *name, *short_name, *os_name, *os_version, *cpu_architecture;
    uint64_t memory_bytes; /* Nominal installed capacity in bytes (8 GiB). */
    uint32_t cpu_cores;
} BenchsetReferenceMachine;
typedef struct {
    double started_at_unix_seconds; /* UTC Unix seconds at the first warmup. */
    double start_temperature_c, end_temperature_c; /* Whole series, in Celsius. */
    double warmup_seconds; /* Before each reference measurement; not a runtime default. */
    uint64_t result_count; /* Pooled benchmark entries, not individual repeats. */
} BenchsetReferenceRun;
typedef struct {
    const char *benchmark;
    double score; /* Sustained iterations/s, pooled across three repeats. */
} BenchsetReferenceBenchmarkResult;
/* Additive result format; the complete V2 prefix remains unchanged. */
typedef struct {
    BenchsetResultV2 base;
    const BenchsetReferenceMachine *reference_machine;
    const BenchsetReferenceRun *reference_run;
} BenchsetResultV3;
typedef struct {
    double elapsed_seconds, duration_seconds;
    uint64_t iterations;
    double iterations_per_second;
} BenchsetSample;
typedef void (*BenchsetProgressCallback)(const BenchsetProgress *, void *);

uint32_t benchset_abi_version(void);
const char *benchset_version(void);
int32_t benchset_default_config(BenchsetConfig *out);
BenchsetHandle *benchset_create(void);
/* A handle is single-use. Only one run per loaded library may be active.
 * Callbacks run on the calling thread. They must return promptly and must not
 * unwind/throw across C. The progress pointer is valid only during the callback.
 * Cancellation is thread-safe, including from a callback. Never free a handle
 * until run has returned and all users of the handle have stopped using it. */
uint32_t benchset_run(BenchsetHandle *, const BenchsetConfig *, BenchsetProgressCallback, void *);
void benchset_cancel(BenchsetHandle *);
/* Result getters are for use after run returns. Success: 1, unavailable: 0.
 * score is meaningful only for BENCHSET_COMPLETED. variation_percent is
 * meaningful only when has_variation != 0. The reason string is borrowed until
 * free; benchset_version() is valid for the loaded library's lifetime. */
int32_t benchset_result(const BenchsetHandle *, BenchsetResult *out);
/* V2 appends a double multiplier. Present for completed scores, including
 * unstable results; absent for failed/cancelled results. Any nonzero presence
 * flag means present; 0.0 is a value, not an absence sentinel. */
int32_t benchset_result_v2(const BenchsetHandle *, BenchsetResultV2 *out, uint32_t out_size);
/* V3 adds shared calibration pointers, non-NULL for every available result,
 * including failure/cancellation. They remain valid after free(handle), until
 * the library is unloaded. Treat the structures and their strings as immutable.
 * NULL/wrong exact size/unavailable result returns 0 and leaves out unchanged. */
int32_t benchset_result_v3(const BenchsetHandle *, BenchsetResultV3 *out, uint32_t out_size);
/* Global calibration: no handle or benchmark run required. All strings are
 * borrowed, immutable UTF-8, valid for the lifetime of the loaded library.
 * New getters return 1 on success, 0 for NULL/wrong exact size/unavailable
 * index; failure leaves out unchanged. Sizes include normal C ABI padding.
 * Reference result indices are zero-based and smaller than result_count. */
int32_t benchset_reference_machine(BenchsetReferenceMachine *out, uint32_t out_size);
int32_t benchset_reference_run(BenchsetReferenceRun *out, uint32_t out_size);
int32_t benchset_reference_result(uint64_t index, BenchsetReferenceBenchmarkResult *out, uint32_t out_size);
int32_t benchset_sample(const BenchsetHandle *, uint64_t index, BenchsetSample *out);
const char *benchset_reason(const BenchsetHandle *);
void benchset_free(BenchsetHandle *);
#ifdef __cplusplus
}
#endif
#endif
