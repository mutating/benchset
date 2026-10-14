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
 * meaningful only when has_variation != 0. Strings are borrowed until free. */
int32_t benchset_result(const BenchsetHandle *, BenchsetResult *out);
int32_t benchset_sample(const BenchsetHandle *, uint64_t index, BenchsetSample *out);
const char *benchset_reason(const BenchsetHandle *);
void benchset_free(BenchsetHandle *);
#ifdef __cplusplus
}
#endif
#endif
