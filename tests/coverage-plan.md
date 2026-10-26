# Benchmark test matrix

The Pi calibration feature adds the two-round, ten-reviewer scenario matrix in
[`pi-equivalent-plan.md`](pi-equivalent-plan.md), including its implementation
map and release-archive consumer checks.

Merged from five independent reviews of the whole Python/Rust/Swift library.
Duplicate scenarios, README/string snapshots, random performance thresholds,
real memory/thread exhaustion and undefined C-pointer operations were removed.
The following groups are exercised directly in each applicable language, rather
than relying on Python tests to exercise uninstrumented Rust or Swift code.

| Group | Scenarios | Tests |
| --- | --- | --- |
| Public contract | Names, version from Python package metadata, units, defaults, one/all workers, immutable Python models, every result/progress/sample field, exact double-precision sample rates, unsigned 64-bit limits | Rust `runtime_tests`, `ffi_tests`; Python `test_benchset`, `test_semantics`; Swift `resultAccountingAndProgress`, `everyNativeFieldAndOptionalMapping` |
| Durations | Zero/negative/negative-zero, NaN, both infinities, finite overflow, subnanosecond rounding, monotonic-clock overflow, invalid inputs do not reserve runner; Python/Swift preserve requested values in failure diagnostics | Rust `invalid_and_overflow_durations`, `invalid_config_matrix`; Python validation/limits/type tests; Swift `invalidDurationsAndRecovery` |
| Python type contract | Numeric conversion errors raise before native initialization; runner remains reusable | Python `test_invalid_python_types_raise_before_start_and_do_not_poison_runner` |
| Phases and callbacks | Observed phase order (short phases may be skipped), zero warmup, monotonic bounded fraction, phase durations, caller-thread execution, terminal iteration count | Real-run tests in all three languages; controlled ctypes callbacks verify each Python phase |
| Measurement accounting | Actual elapsed throughput, warmup exclusion, short/long measurements, sample sums, positive sample intervals | Real-run tests in all three languages; Rust counter/sample tests |
| Statistics | Unequal time weights, zero/one/multiple samples, identical speeds, exact 10% boundary and values above it, zero work/time, failure priority | Rust `weighted_statistics_and_exact_stability_boundary`, `failed_cancelled_and_no_work_statistics_keep_diagnostics` |
| Cancellation samples | Late cancellation preserves completed work; final snapshot later/equal/earlier than worker finish is normalized | Rust `zero_warmup_samples_and_late_cancellation`, `final_sample_repairs_snapshot_after_worker_finish`; Swift late-cancel test; Python phase-cancel tests |
| Cancellation lifecycle | Before start, during loading/allocation, warmup, measurement, external thread, repeated/idle cancel, fresh-token reuse, sticky Rust clones; documented reuse waits for run completion, not cancel return | Rust cancellation/concurrency tests; Python cancellation/loading/creation and documented-lifetime tests; Swift cancellation/allocation/external-thread tests |
| Terminal cancellation | Cancellation from completed callback leaves an already completed native result completed | Rust/Swift terminal-cancel tests |
| Python Ctrl+C | Real SIGINT with and without callbacks under an active handle guard; native-phase callback handshake and real CLI warmup/measurement interruption; repeated signals during loading, allocation, execution and cleanup; native failure during interruption; no cancellation after free; runner reuse; default/custom/ignored/termination handlers and callback-installed handlers | Python `test_signals` |
| Runner exclusion | Concurrent callers, same-runner reentry, native-global busy, losing token cannot cancel winner, subsequent run succeeds | Rust concurrency test; Python busy/reentry tests; Swift reentry/external-thread tests |
| Python signal reentry | Custom SIGINT handler calls cancel during claim, publication, cancellation and before/after free; raising KeyboardInterrupt after claim/allocation/publication releases ownership; rejected nested/concurrent calls cannot clear the winner | Controlled real signal delivery in child processes (`test_signals`); deterministic busy-owner test (`test_semantics`); real native reproduction at all seven cancellation stages |
| Concurrent CLI parsing | Independent argument snapshots and skelet conversion; sys.argv and sys.stderr remain untouched during overlapping calls | `test_concurrent_settings_keep_arguments_and_process_streams_isolated` with controlled thread interleaving |
| Callback failure | Rust panic in warmup/measurement/completed joins workers and releases ACTIVE; Python ValueError/KeyboardInterrupt/SystemExit/GeneratorExit in each phase becomes failed, suppresses repeated/final notifications, frees handles exactly once and allows reuse | Rust `callback_panics_release_workers_and_active_guard`; Python parameterized tests drive actual ctypes callbacks with a controlled C-function table, plus live Rust callback-failure/reuse tests with and without warmup |
| Blocking callbacks | Worker completion timing excludes callback delay, synchronized on observed worker exit | Rust `callback_delay_is_excluded_from_throughput_elapsed` |
| Initialization failures | CPU discovery, first/partial spawn failure, explicit initialization error, disconnected ready channel, worker panic; all started threads joined | Rust `initialization_faults_join_partial_workers_and_recover` with private runtime dependency |
| Kernel identity | Fixed golden vectors, independent workers, persistent state, changed table, extreme seeds and wrapping arithmetic, crafted parser transitions | Rust `kernel_tests` |
| Allocation failure | Capacity overflow without allocating huge memory; kernel construction error propagation; C construction panic boundary; Swift/Python missing handle | Rust kernel/FFI tests; Python allocation/cleanup tests; Swift native-error tests |
| C ABI validation | Defaults/layout, null handle/config/output, getters before run, short-prefix ABI rejection, wrong ABI/size/name/reserved, invalid durations | Rust `ffi_tests`; Python direct ctypes ABI tests |
| C ABI lifecycle | No callback/non-null user context, caller thread, callback cancellation, single-use after success/cancel/failure, original borrowed reason preserved, unavailable/out-of-range sample does not alter output; every progress field with literal wire phase codes and full unsigned widths; public header benchmark/status/phase values and sample field offsets | Rust `ffi_tests`, including `controlled_callback_preserves_every_progress_field`; public C assertions in `tests/native/reference.c` |
| C ABI recovery/mapping | Native panic converted to failure; poisoned mutex recovery; Unicode/NUL sanitization; absent/zero/fractional CV; populated diagnostics preserved for all statuses; instability and two distinct samples retrieved in their original order | Rust `panic_boundary_create_and_poison_recovery`, `complete_result_mapping_unicode_nul_and_large_unsigned_fields` |
| Wrapper optional mapping | Python/Swift preserve fractional CV exactly for presence flags 0, 1, 7 and u32 maximum, including failure/cancellation diagnostics; unavailable CV does not imply instability | Python `test_variation_mapping_preserves_double_precision_and_nonzero_presence`, `test_unavailable_variation_does_not_imply_instability`; Swift `variationUsesEveryNonzeroPresenceFlagAtDoublePrecision`, `everyNativeFieldAndOptionalMapping` |
| Python loader | DLL/dylib/SO selection; native ABI 1 and package version independently checked before symbol lookup; same-version old binaries missing additive required getters fail before allocation; ctypes signatures | Python loader/version tests |
| Wrapper cleanup | Constructor/create/execute/config/result/sample failure; free exactly once; concurrent cancellation during free never reaches the released handle; reuse after failure/cleanup | Python managed-native/getter/cleanup-race tests; Swift `nativeErrorsFreeExactlyOnceAndRunnerIsReusable`, `cancellationDuringCleanupNeverUsesFreedHandle` |
| Resource lifetimes | Callback captures released with cyclic GC disabled after success/cancellation/callback or native failure; handle freed if publication is interrupted; Swift callback captures and runner released on every exit | Python `test_resources`; Swift `callbackCapturesAndRunnerReleaseOnAllExits` |
| Swift callback ownership | Null progress/context ignored, context/closure captures alive during native call and released afterwards | Swift `nullableCallbacksAndContextLifetime` with private C-function table |
| Suite | Single then multi; parameters/callback forwarded; stops after failure/cancel on either step | Python suite tests |
| CLI parameters | Help/version/unknown options, missing values, invalid floats/names, selected benchmark/default suite, repeated parser calls, original argv restored | Python CLI tests |
| CLI completion | 0/stdout on success; 1/stderr on failure; 130/stderr on cancellation/interruption; stop suite; repeated Ctrl+C; interruption before/after worker start/join; signal handler restored on exit/startup failure; calls outside main thread; interrupted completion; terminal progress/output failure; semantic rendering of optional fields/samples | Python CLI tests |
| CLI entry points and presentation | Both console/module entry points; explicit/implicit all and each benchmark; option order, equals syntax, repeated options, decimal/scientific notation including signed zero; complete help/defaults/units; actionable invalid-input errors; native duration limits; real Ctrl+C in warmup/measurement; aligned sample columns including u64 extremes; terminal widths 1–120 and shrink before cleanup; UTF-8/ASCII/cp1251 output; immediate result flushing; closed pipes/descriptors, full output destinations, disconnected PTYs and interrupt during rendering | Python `test_cli` plus existing CLI tests |

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

## Timing and isolation

Result-to-calibration references are covered separately in
[`result-references-plan.md`](result-references-plan.md), including public
types, every returned status, identity/value semantics, ownership, legacy ABI
consumers and actual packaged artifacts for all languages.

Real-run tests validate accounting for the samples actually observed. A delayed
coordinator can legitimately miss an intermediate one-second snapshot, leaving
one final sample and no CV. Fixed counters, elapsed times and native result
fixtures independently verify multi-sample statistics and field transport.
The callback-delay test waits for an observed worker exit and checks that the
single final sample excludes time spent blocked in the callback.

API SIGINT subprocesses hold a live handle until the parent sends the signal;
callback cases hold the first native phase callback, while no-callback cases
hold entry to native execution. This removes startup and parent-scheduling
races without replacing the real signal or adding a user callback. The child
explicitly selects the default SIGINT policy, with both default and ignored
parent handlers tested; the parent handler is restored after process creation.
PTY tests
use a controlled terminal width and an explicit progress-frame marker for
disconnection, so inherited narrow-terminal settings cannot match final output
as readiness. The disconnected run has a long window and exits on the event.
Progress-write failures target a frame rather than a phase name, with explicit
narrow and normal widths. Temporary release repositories ignore inherited Git
signing and hook configuration; monkeypatch restores the parent environment.

Recovery checks in Rust, Python and Swift start a fresh real run and cancel it
from an observed work-phase callback. They assert that the callback was reached
and the run was cancelled, so stale cancellation or a stuck active guard cannot
pass. They do not require a 10–30 ms measurement to get a CPU time slice under
parallel CI load. Dedicated throughput/accounting tests still require completed
work and a valid score; fixed fixtures retain complete result/statistics checks.

The worker-panic fixture also waits for the fault rather than assuming the
broken kernel executes within 30 ms: its test thread guard cancels coordination
while unwinding, and the joined panic must still take priority over cancellation.
