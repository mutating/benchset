# Pi equivalent: aggregated test plan

Round 1: five independent reviews of Rust, C ABI, Python, Swift and CLI/builds.
Round 2: five additional reviews search this deduplicated list for missing cases.

## Contract

- Rust owns immutable `PI_REFERENCE_MACHINE` and `PI_REFERENCE_RUN` values.
  Python exports frozen equivalents without loading native code at import;
  integration tests prevent the lazy Python metadata from drifting from Rust.
  Swift exposes independent immutable value snapshots from the C API.
- Machine: Raspberry Pi 5 Model B Rev 1.0; short name `pi`; Ubuntu Server,
  version `26.04 LTS`; `aarch64`; nominal installed RAM 8589934592 bytes
  (8 GiB, distinct from Linux MemTotal); four CPU cores.
- The reference run describes the **six-run final reference series**, starting
  with its first warmup and ending with its last measurement. Preparation,
  profiling and final cooling are excluded. Start: Unix UTC
  `1792767590.517391` (`2026-10-23T14:59:50.517391Z`), start temperature
  41.35 C from run_start, end temperature 66.65 C from the final completed
  progress callback. This interval ends on 23 October in Moscow; only the
  larger preparation/cooling session crossed midnight.
- Historical warmup is 1248 seconds before each measurement. Runtime defaults
  remain 120/60 seconds and explicit options remain unchanged.
- Scores are pooled across three repeats each: single_core =
  47231.227029007096 = 42508112 / 900.000162475; multi_core =
  181953.17419139884 = 163757952 / 900.000523364.
- `BenchmarkResult.pi_equivalent` / Swift `piEquivalent` is optional f64/Double:
  score divided by the reference for that benchmark, without worker-count
  normalization. Present on completed results, including unstable results;
  absent on failures/cancellation and Python callback-induced failure.
- C ABI 1: preserve the existing configurations and result getter/layout and
  behavior; expose additive extended results via size-checked versioned getters. Metadata
  getters are handle-independent, size-checked; strings live for the process.
  Python checks ABI/version before resolving new required symbols and reports
  missing symbols as a failed result before allocating a handle; Swift
  rejects incompatible ABI before creating/running a benchmark handle.
- CLI renders the existing native ratio and identifies the machine (`pi`).
  This feature does not add a JSON CLI mode, editable references, automatic
  warmup tuning or runtime SSH/sensor/network access.

## Accepted scenarios, deduplicated

| ID | Required checks |
|---|---|
| PI01 | Public types/globals visible from normal Rust/Python/Swift consumer imports; all machine fields golden, RAM UInt64, host-independent. |
| PI02 | All reference-run fields golden; UTC date conversion, source-consistent series boundaries/temperatures, historical warmup, exactly two unique benchmark names. |
| PI03 | Full f64 reference scores equal independent fixture and pooled raw totals; no profile/best/median/rounded score substitution. |
| PI04 | Global metadata stable before/after success/failure/cancel/free; reads without benchmark handles, active lock, callbacks or native loading at Python import. |
| PI05 | Frozen Python nested dataclasses and tuple; Swift value copies/arrays and copied strings survive backing-buffer changes and runner free; metadata asdict/JSON uses primitive epoch seconds, aware datetime property. |
| PI06 | Both benchmarks: 0.5, 1, 2, nontrivial f64 fraction, very small and >100 ratios; correct direction and denominator, no clipping/percent/worker normalization. |
| PI07 | Ratio derives from final actual iterations/elapsed and weighted total throughput, not average sample ratios or rounded scores; short live runs verify both paths. |
| PI08 | Completed unstable and exact stability boundary retain score/ratio; terminal cancellation preserves completed ratio. |
| PI09 | All existing early failure, busy/reentry, native panic, no work/time, duration/init/thread failure, pre/warmup/measurement cancellation paths have no ratio; partial diagnostics preserved; recovery gets fresh ratio. |
| PI10 | Python callback exceptions (including completed and BaseException) suppress ratio and score, preserve cleanup/reuse and notification suppression. |
| PI11 | ctypes/Swift map native ratio directly at full double precision, distinguish None/nil from 0.0, honor any nonzero presence flag; failed/cancelled ignore populated raw ratio. All old fields/u64 extremes preserved. |
| PI12 | Python real field/default None/asdict and positional constructor compatibility; run_all order, per-benchmark ratios and early stopping; options/callback forwarding unchanged. |
| PI13 | ABI/header/ctypes/Swift version, sizes, alignment, offsets and symbols agree; incompatible ABI/version and same-version ABI-1 binaries missing required new symbols fail before native allocation. |
| PI14 | Legacy 88-byte result getter canary remains intact; extended getter rejects wrong output sizes/null/before-run without modifying output, accepts correct size. |
| PI15 | Metadata getters reject null/incorrect size with unchanged output; reference indexes 0/last valid, count/UINT64_MAX invalid; strings valid UTF-8/NUL and survive handle free. |
| PI16 | Concurrent metadata reads and reads inside progress callback succeed without deadlock, changing cancellation or blocking active benchmark. |
| PI17 | New wrapper getter/config/result/sample failures preserve exactly-once handle cleanup, callback lifetime and reuse; Swift metadata-loader failures return explicit error. |
| PI18 | CLI displays 0, 0.5, 1, 2, tiny/large fractions and unavailable; preserves score/unit/samples/Unicode/large counters and prints native ratio, not a recalculation. |
| PI19 | CLI all/selected benchmark, success/failure/cancel streams/exits and early stop remain correct; help/version/validation remain lazy. |
| PI20 | Native CLI and installed module/console entry point smoke from outside checkout show pi and finite ratio; full metadata works without external experiment folder or sensors/network/SSH. |
| PI21 | C and C++ public smoke consumers compile/link new header/symbols; public Rust/Swift examples compile and access metadata/ratio without internal imports. |
| PI22 | Wheel/sdist/crate/native archive include required sources/metadata; XCFramework header equals current include/benchset.h and Swift links current binary. |
| PI23 | README explains score vs multiplier, precise reference identity/provenance, aggregation, series boundaries, RAM units and historical vs runtime warmup. |
| PI24 | Existing complete Python branch coverage and Rust/Swift production-source coverage gates include all new sources, without exclusions or long warmup benchmarks. |
| PI25 | Real old C declarations (without current header) continue to run with ABI-1 configurations, preserve the 88-byte output canary and all legacy result ABI fields, and retain reason/free behavior. Fresh handles also support the additive extended getters. |
| PI26 | Keep declared minimum versions: new Python tests run unskipped on the existing Python 3.8 CI matrix; Rust 1.74 checks the library and public consumer; Swift consumer uses the normal deployment target. |

Round 2 also refined existing groups: PI06 multi-core with a single worker still
uses the multi-core denominator; PI07 delayed callbacks and repaired final
samples use actual worker elapsed; PI09 panic takes priority over cancellation;
PI13 checks ABI before separate Swift metadata loading; PI17 rejects a failed
second metadata entry atomically and permits retry; PI05 copies strings before
the next metadata getter; PI20–22 execute consumers after unpacking archives,
including both XCFramework architectures and installed source/wheel CLI paths.

## Implemented checks

| Groups | Test sources |
|---|---|
| PI01–05 | Rust `reference_tests.rs`; C `ffi_reference_tests.rs` and `tests/native/reference.c`; Python `test_reference.py`; Swift `ReferenceTests.swift`. Python also compares every public and C metadata field with `native/reference-calibration.json` and independently checks pooled raw totals. |
| PI06–10 | Rust `reference_tests.rs` and `runtime_tests.rs`; Python `test_reference.py`, `test_semantics.py`, `test_errors.py`, `test_resources.py`; Swift `ReferenceTests.swift` and `BenchsetTests.swift`. |
| PI11–12 | Python `test_reference.py` and `test_semantics.py`; Swift `ReferenceTests.swift`; Rust `ffi_reference_tests.rs`. |
| PI13–17, PI25 | Rust `ffi_reference_tests.rs` and `ffi_tests.rs`; real C/C++/ABI-1 consumers under `tests/native`, executed by `test_reference_native.py`; Python loader/cleanup tests; Swift injected-loader and ownership tests. |
| PI18–20 | Python `test_reference.py`, existing CLI tests, `scripts/check_source_install.py`, and installed module/console smoke in `test_reference_artifacts.py`. |
| PI21–22 | `test_reference_native.py` compiles C/C++; `test_reference_artifacts.py` executes the unpacked crate's unit tests and public Rust consumer, builds a wheel from the unpacked sdist, links the unpacked native ZIP, and runs a public consumer of the offline Swift ZIP with both XCFramework symbol inventories. |
| PI23 | README documents the public API and recorded calibration, linked to the packaged raw-total/provenance JSON. |
| PI24 | Existing Python coverage jobs and `scripts/check_native_coverage.py` cover all runtime files, including the new reference modules. |
| PI26 | Existing Python 3.8 CI matrix; dedicated Rust 1.74 library/example CI check; Swift archive consumer with the package's normal deployment target. |

## Excluded after review

- Duplicate copies of the same invariant, snapshot-only prose/README tests,
  speed thresholds for the current host, real 1248-second test warmups.
- Undefined C behavior (dangling/misaligned pointers, double-free, invalid Rust
  enum discriminants, mutating borrowed immutable strings), genuine OOM.
- Editing/corrupting a configurable reference: references are immutable and
  no such input/API exists. Invalid arbitrary scores need a policy only if a
  public arbitrary-score conversion helper is introduced (not planned).
- A new JSON CLI or automatic warmup selection, both outside this feature.
- Requiring the final reference measurement to be dated 24 October: that
  incorrectly confuses the full cooling session with the reference series.
