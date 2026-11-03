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
/* Read once on the calling thread and copy its double without arithmetic.
 * reader/user are borrowed only during the call; user may be NULL if supported
 * by reader. reader must not unwind. NULL reader/out returns 0 without calling
 * reader or modifying output; otherwise aligned writable out receives the value
 * and the function returns 1. out may alias caller data used by reader. */
typedef double (*BenchsetDoubleReader)(void *);
int32_t benchset_copy_double(BenchsetDoubleReader, void *user, double *out);
int32_t benchset_default_config(BenchsetConfig *out);
/* UTF-8 byte length, including any embedded NUL. Unknown names return 0. */
uint32_t benchset_benchmark_id(const uint8_t *name, uint64_t size);
/* Compare caller-owned host names with borrowed catalog UTF-8 names. A positive
 * return matches, zero continues, negative stops. Execution mode compares every
 * entry except the final fallback; validation mode checks the full catalog. */
typedef int32_t (*BenchsetNameMatcher)(const uint8_t *, uint64_t, void *);
uint32_t benchset_benchmark_match(BenchsetNameMatcher, void *user, uint32_t execution);
/* Validation codes: 0 valid, 1 unknown benchmark, 2 warmup, 3 measurement.
 * Duration validation is independent of representability as a Rust Duration. */
uint32_t benchset_duration_error(double warmup, double measurement);
uint32_t benchset_validation_error(uint32_t benchmark, double warmup, double measurement);
/* Immutable library-lifetime message; nonzero cli selects CLI option names. */
const char *benchset_validation_message(uint32_t error, uint32_t cli);
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

/* Full-suite API. Existing single-benchmark ABI layouts remain unchanged. */
typedef struct BenchsetSuiteHandle BenchsetSuiteHandle;
typedef struct {
    double pi_equivalent; /* Geometric mean: exp(mean(log(Pi equivalent))); zero if any value is zero. */
    uint32_t has_pi_equivalent; /* Absent unless the complete suite succeeded. */
} BenchsetAggregatedScores;
typedef struct {
    uint32_t benchmark, status;
    double pi_equivalent;
    uint32_t has_pi_equivalent;
} BenchsetAggregateInput;
/* Aggregate caller-owned snapshots. Invalid/incomplete suites have no score.
 * Any nonzero presence flag means present. NULL inputs is valid only at count 0.
 * Non-NULL pointers must be aligned and valid; out must be writable, and a
 * complete-suite input must contain count readable entries. Only NULL is checked.
 * Returns 0 for invalid NULL pointers, leaving out unchanged; otherwise returns 1. */
int32_t benchset_aggregate(const BenchsetAggregateInput *, uint64_t count, BenchsetAggregatedScores *out);
typedef struct {
    BenchsetAggregateInput value;
    const uint8_t *name;
    uint64_t name_size;
} BenchsetAggregateRequest;
/* Lazy host primitives: 0 reads status; 1 reads ratio/presence; 2 requests >=;
 * 3 requests ==; 4 requests log(value.pi_equivalent) for a nonpositive argument;
 * 5 compares the caller's name with the borrowed UTF-8 name/name_size operand;
 * 6 reads a fresh ratio in host-name mode, where 1 reads only its presence;
 * 7 requests exp(value.pi_equivalent) only for finite host-mode overflow.
 * Operators 2/3 receive an integer operand in value.status. Comparisons return
 * their truth value in value.has_pi_equivalent; fields 4/7 return a double.
 * Every nonzero presence/comparison flag and reader return is accepted.
 * The callback-borrowed buffer must retain its size; only requested fields are read.
 * Host-name mode compares the shared prefix before checking count, then the core
 * requests status/ratio/>= for each entry, followed by short-circuit == and logs.
 * Each host-mode log separately requests presence, then a fresh ratio if present.
 * With host_names=0, ordered raw IDs and count are checked before host reads.
 * A zero reader result makes the aggregate unavailable. Reader is required;
 * other NULL/output rules match benchset_aggregate. */
typedef int32_t (*BenchsetAggregateReader)(uint64_t index, uint32_t field, BenchsetAggregateRequest *, void *);
int32_t benchset_aggregate_read(const uint32_t *benchmarks, uint64_t count,
    BenchsetAggregateReader, void *user, BenchsetAggregatedScores *out, uint32_t host_names);
typedef struct {
    const BenchsetResultV3 *results; /* Immutable array, borrowed until suite_free. */
    uint64_t result_count;
    BenchsetAggregatedScores aggregated_scores;
} BenchsetSuiteResult;
typedef struct {
    uint32_t benchmark;
    BenchsetProgress progress;
} BenchsetSuiteProgress;
typedef void (*BenchsetSuiteProgressCallback)(const BenchsetSuiteProgress *, void *);
BenchsetSuiteHandle *benchset_suite_create(void);
/* Single-use handle; config.benchmark is ignored. Runs single_core then
 * multi_core, stopping after failure/cancellation. Same callback and lifetime
 * rules as benchset_run. Returns the final individual result's status. */
uint32_t benchset_run_all(BenchsetSuiteHandle *, const BenchsetConfig *, BenchsetSuiteProgressCallback, void *);
/* Called synchronously after each result and before starting the next benchmark.
 * The child handle is borrowed only during this callback; use single-run getters
 * to copy its result, samples and reason. Never free it. Nonzero stops the suite. */
typedef uint32_t (*BenchsetResultCallback)(uint32_t benchmark, const BenchsetHandle *, void *);
uint32_t benchset_run_all_observed(BenchsetSuiteHandle *, const BenchsetConfig *, BenchsetSuiteProgressCallback, BenchsetResultCallback, void *);
/* Called on the calling thread before each child, after checking and copying the
 * config prefix. The writable copy is borrowed only during this callback; its
 * benchmark is ignored. Nonzero proceeds to normal validation/execution; zero
 * aborts with BENCHSET_FAILED and retains the completed prefix without adding a
 * result. NULL/incompatible configs still publish their normal failed result
 * without calling this callback. The caller's original config is unchanged. */
typedef uint32_t (*BenchsetConfigCallback)(uint32_t benchmark, BenchsetConfig *, void *);
uint32_t benchset_run_all_configured(BenchsetSuiteHandle *, const BenchsetConfig *, BenchsetSuiteProgressCallback, BenchsetResultCallback, BenchsetConfigCallback, void *);
void benchset_suite_cancel(BenchsetSuiteHandle *);
/* Getters return 1 on success, 0 on NULL/wrong size/unavailable index, leaving
 * output unchanged on failure. All indices are zero-based. */
int32_t benchset_suite_result(const BenchsetSuiteHandle *, BenchsetSuiteResult *, uint32_t out_size);
int32_t benchset_suite_result_at(const BenchsetSuiteHandle *, uint64_t index, BenchsetResultV3 *, uint32_t out_size);
int32_t benchset_suite_sample(const BenchsetSuiteHandle *, uint64_t result_index, uint64_t sample_index, BenchsetSample *);
/* Borrowed until suite_free; calibration pointers keep their global lifetime. */
const char *benchset_suite_reason(const BenchsetSuiteHandle *, uint64_t index);
void benchset_suite_free(BenchsetSuiteHandle *);
#ifdef __cplusplus
}
#endif
#endif
