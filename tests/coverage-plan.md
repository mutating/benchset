# Benchmark test matrix

Merged from five independent reviews of the whole Python/Rust/Swift library.
Duplicate scenarios, README/string snapshots, random performance thresholds,
real memory/thread exhaustion and undefined C-pointer operations were removed.
The following groups are exercised directly in each applicable language, rather
than relying on Python tests to exercise uninstrumented Rust or Swift code.

| Group | Scenarios | Tests |
| --- | --- | --- |
| Public contract | Names, version, units, defaults, one/all workers, immutable Python models, every result/progress/sample field, unsigned 64-bit limits | Rust `runtime_tests`, `ffi_tests`; Python `test_benchset`, `test_semantics`; Swift `resultAccountingAndProgress`, `everyNativeFieldAndOptionalMapping` |
| Durations | Zero/negative/negative-zero, NaN, both infinities, finite overflow, subnanosecond rounding, monotonic-clock overflow, invalid inputs do not reserve runner | Rust `invalid_and_overflow_durations`, `invalid_config_matrix`; Python validation/limits/type tests; Swift `invalidDurationsAndRecovery` |
| Python type contract | Numeric conversion errors raise before native initialization; runner remains reusable | Python `test_invalid_python_types_raise_before_start_and_do_not_poison_runner` |
| Phases and callbacks | Observed phase order (short phases may be skipped), zero warmup, monotonic bounded fraction, phase durations, caller-thread execution, terminal iteration count | Real-run tests in all three languages; controlled ctypes callbacks verify each Python phase |
| Measurement accounting | Actual elapsed throughput, warmup exclusion, short/long measurements, sample sums, positive sample intervals | Real-run tests in all three languages; Rust counter/sample tests |
| Statistics | Unequal time weights, zero/one/multiple samples, identical speeds, exact 10% boundary and values above it, zero work/time, failure priority | Rust `weighted_statistics_and_exact_stability_boundary`, `failed_cancelled_and_no_work_statistics_keep_diagnostics` |
| Cancellation samples | Late cancellation preserves completed work; final snapshot later/equal/earlier than worker finish is normalized | Rust `zero_warmup_multiple_samples_and_late_cancellation`, `final_sample_repairs_snapshot_after_worker_finish`; Swift late-cancel test; Python phase-cancel tests |
| Cancellation lifecycle | Before start, during loading/allocation, warmup, measurement, external thread, repeated/idle cancel, fresh-token reuse, sticky Rust clones | Rust cancellation/concurrency tests; Python cancellation/loading/creation tests; Swift cancellation/allocation/external-thread tests |
| Terminal cancellation | Cancellation from completed callback leaves an already completed native result completed | Rust/Swift terminal-cancel tests |
| Runner exclusion | Concurrent callers, same-runner reentry, native-global busy, losing token cannot cancel winner, subsequent run succeeds | Rust concurrency test; Python busy/reentry tests; Swift reentry/external-thread tests |
| Callback failure | Rust panic in warmup/measurement/completed joins workers and releases ACTIVE; Python ValueError/KeyboardInterrupt/SystemExit/GeneratorExit in each phase becomes failed, suppresses repeated/final notifications, frees handles exactly once and allows reuse | Rust `callback_panics_release_workers_and_active_guard`; Python parameterized tests drive actual ctypes callbacks with a controlled C-function table, plus live Rust callback-failure/reuse tests with and without warmup |
| Blocking callbacks | Worker completion timing excludes callback delay, synchronized on observed worker exit | Rust `callback_delay_is_excluded_from_throughput_elapsed` |
| Initialization failures | CPU discovery, first/partial spawn failure, explicit initialization error, disconnected ready channel, worker panic; all started threads joined | Rust `initialization_faults_join_partial_workers_and_recover` with private runtime dependency |
| Kernel identity | Fixed golden vectors, independent workers, persistent state, changed table, extreme seeds and wrapping arithmetic, crafted parser transitions | Rust `kernel_tests` |
| Allocation failure | Capacity overflow without allocating huge memory; kernel construction error propagation; C construction panic boundary; Swift/Python missing handle | Rust kernel/FFI tests; Python allocation/cleanup tests; Swift native-error tests |
| C ABI validation | Defaults/layout, null handle/config/output, getters before run, short-prefix ABI rejection, wrong ABI/size/name/reserved, invalid durations | Rust `ffi_tests`; Python direct ctypes ABI tests |
| C ABI lifecycle | No callback/non-null user context, caller thread, callback cancellation, single-use after success/cancel/failure, original borrowed reason preserved, unavailable/out-of-range sample does not alter output | Rust `ffi_tests` |
| C ABI recovery/mapping | Native panic converted to failure; poisoned mutex recovery; Unicode/NUL sanitization; absent/present score/CV, instability and all sample fields | Rust `panic_boundary_create_and_poison_recovery`, `complete_result_mapping_unicode_nul_and_large_unsigned_fields` |
| Python loader | DLL/dylib/SO selection; native ABI and package version independently checked; ctypes signatures | Python loader/version tests |
| Wrapper cleanup | Constructor/create/execute/config/result/sample failure; free exactly once; no calls on freed handles; reuse after failure | Python managed-native/getter/cleanup-race tests; Swift `nativeErrorsFreeExactlyOnceAndRunnerIsReusable` |
| Swift callback ownership | Null progress/context ignored, context/closure captures alive during native call and released afterwards | Swift `nullableCallbacksAndContextLifetime` with private C-function table |
| Suite | Single then multi; parameters/callback forwarded; stops after failure/cancel on either step | Python suite tests |
| CLI parameters | Help/version/unknown options, missing values, invalid floats/names, selected benchmark/default suite, repeated parser calls, original argv restored | Python CLI tests |
| CLI completion | 0/stdout on success; 1/stderr on failure; 130/stderr on cancellation/interruption; stop suite; repeated Ctrl+C; interruption before/after worker start/join; signal handler restored on exit/startup failure; calls outside main thread; interrupted completion; terminal progress/output failure; semantic rendering of optional fields/samples | Python CLI tests |

## Source installation

`test_build.py` separately checks the PEP 517 bootstrap: an existing Cargo needs
no downloads; automatic setup uses a pinned SDK in a private cache; concurrent
builds install it once; incomplete caches are repaired; failed installs clean up
and can retry; installer checksums are verified before execution; platform cache
paths and Windows executable suffixes are preserved. Parent environment values
are checked for accidental mutation. `scripts/check_source_install.py` exercises
an actual pip install in a new venv with Cargo hidden from PATH and runs the
installed CLI outside the source checkout. CI runs this on Linux, Windows and
both Mac architectures. These build utilities stay outside runtime coverage.

## Coverage gates

Production scope is `benchset/*.py`, all runtime `.rs` files in `native/src`, and
all `.swift` files under `Sources`. Tests, examples and build/release utilities
are not counted as benchmark runtime. Existing release-tool tests remain in the
Python suite, but do not inflate benchmark coverage.

- Python: 100% statements and branches, including CLI subprocesses.
- Rust: 100% lines, functions, regions and instantiations using the pinned
  compiler and its matching `llvm-tools-preview` component.
- Swift: 100% lines, functions, regions and instantiations using Swift Testing
  and Apple LLVM coverage tools. The test executable targets macOS 13; the
  published library deployment target stays as specified in `Package.swift`.
- LLVM branch counters currently report no data for Rust/Swift. They are not
  presented as 100% branch coverage. All supported failure and decision
  scenarios above are nevertheless tested explicitly.

Coverage is measured independently for each language. The native checker
verifies the complete production file inventory and fails on any uncovered
counter; no production-source exclusion or coverage-suppression pragma is used.
Reports are saved under `target/coverage` and uploaded by CI.

Outside the supported contract: dangling/misaligned C pointers, double-free,
free during run, throwing through an `extern C` callback, corrupt output from a
foreign implementation, process termination and genuine allocator abort/OOM.
Recoverable initialization failures are tested with safe dependency injection.
