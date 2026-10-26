# Result calibration references

Each returned benchmark result exposes its calibration machine and run, including
failed/cancelled/busy/unstable results. This does not change scores or durations.
Rust fields are static references to public statics; Python appends immutable
metadata fields with the existing globals as defaults; Swift properties read the
global structs with ordinary value semantics. C adds an exact-size V3 getter
with a V2 prefix and const pointers to immutable library-lifetime metadata.
ABI 1 configurations and all V1/V2 layouts/getters retain their behavior; V3
is an additive result format on the same ABI.

## Deduplicated first-pass test scenarios

| ID | Required check | Main location |
| --- | --- | --- |
| R1 | Rust public static reference types and pointer identity for both workloads | reference_tests |
| R2 | Default/failed, completed stable/unstable, cancelled, busy, invalid options and initialization faults all retain references | Rust reference/runtime tests |
| R3 | Result clone/drop and cancellation-token drop cannot shorten reference lifetime | reference_tests and Rust consumer |
| R4 | Concurrent reads via retained result references during active runs and after drop | reference_tests |
| C1 | ABI1 for configurations and every result prefix; V1=88, V2=104, V3=120; prefix and pointer offsets/types including const C++ pointers | ffi_reference_tests and C/C++ consumers |
| C2 | Null outputs/handles, before run, exact-size rejection and unchanged bytes/canaries | ffi_reference_tests |
| C3 | All legacy fields/widths, zero/absent ratio and unstable values match V1/V2/V3 getters | ffi_reference_tests |
| C4 | Non-null canonical pointer identity across workloads, statuses, handles and repeat getters | ffi_reference_tests and C consumer |
| C5 | Every referenced field/string matches scalar global getters and calibration, local getter copies do not mutate globals | ffi_reference_tests |
| C6 | References remain readable after output overwrite and handle free; only global pointers are used afterwards | ffi_reference_tests and public consumers |
| C7 | True cold parallel initialization and concurrent readers on prepared handles, with joins before freeing | standalone C++ consumer |
| C8 | Existing calibration entry order/count/index validation is unchanged | native metadata tests and C consumer |
| C9 | Independent frozen V1 and V2 consumers use old declarations against the new library | legacy_v1.c and legacy_v2.c |
| P1 | New fields are required public non-Optional types, actual dataclass fields defaulting to identical globals | test_reference and typing |
| P2 | Preserve complete legacy positional prefix including pi_equivalent, append new fields | test_reference |
| P3 | Native-free fresh import, default construction, validation failure, asdict and JSON have result metadata | test_reference |
| P4 | Both benchmarks and all returned native/API statuses, callback errors, initialization/getter errors and busy paths expose global identity | existing Python runtime/semantics/error tests |
| P5 | Every public entry and each actually returned run_all element, including early stop, has metadata | test_reference and semantics |
| P6 | Frozen fields/nested objects, replace identity, asdict isolation and JSON primitive metadata | test_reference |
| P7 | Metadata outlives runner/handle and is safe to read while later runs execute or after cleanup | reference tests |
| P8 | Positive/negative public type-checking examples are executed by pytest | tests/typing |
| S1 | Public required Swift property types and exact metadata field values/Double bits | ReferenceTests |
| S2 | All returned statuses/validation/busy/stable/unstable and reuse paths expose properties | Swift existing tests |
| S3 | Properties outlive runner/free and concurrent reads, local array mutation cannot modify calibration | Swift ownership/concurrency tests |
| S4 | Computed properties preserve the existing memberwise constructor; thrown errors create no fake result | Swift contract tests |
| I1 | Actual unpacked crate consumer verifies public static lifetimes and identity | Rust example/artifact tests |
| I2 | C/C++ current header and frozen V2 work from native ZIP, dynamic and static library | native artifact tests |
| I3 | Source/wheel installs outside checkout verify references, native-free construction, JSON and typing payload | check_source_install/artifact tests |
| I4 | Published wheel release gate checks result reference identity | release workflow |
| I5 | Offline Swift package consumer uses properties; both XCFramework slices export V3 | artifact tests |
| I6 | README result table/interface mapping/examples explain all-status access and C lifetime | README and executable examples |
| I7 | CLI presentation stays compact and its exact fields/encodings/exit behavior remain unchanged | CLI semantic tests |

Identity is promised for live Python default/returned fields and Rust/C global
references. Standard deepcopy/pickle/JSON reconstruction preserves data, not
Python singleton identity. Swift structs do not have reference identity and no
new Codable/Equatable/Sendable/class migration is required. Arbitrary invalid C
pointers, concurrent free of in-use handles, unloaded-library pointers, OOM and
new custom-calibration features are outside this change.

## Second-pass gap review

Five further agents rechecked the first-pass plan. Accepted additions:

| ID | Additional check | Main location |
| --- | --- | --- |
| C10 | A separate busy handle gets metadata; rerunning a used handle leaves its original V1/V2/V3 snapshot intact | ffi_reference_tests |
| C12 | Invalid requested durations retain their exact floating-point bits through every C result format | ffi_reference_tests |
| P9 | Explicit metadata survives direct construction and dataclass replacement | test_reference |
| S5 | First Swift property access is concurrent inside another run's callback in a fresh consumer process | artifact tests |
| S6 | External Swift writes to either property or its nested immutable array fail compilation | artifact tests |
| I8 | Separately released XCFramework matches the tested offline package, including V3 in both slices | artifact tests |

Rejected as outside the contract: getters called from callbacks before run
returns (C11), invalid C pointers/concurrent free, and singleton identity after
deserialization. The first-access Swift test and C cold initialization test
exercise distinct language initialization mechanisms.

## Review tracks after implementation

For each track, use rounds of three reviewers until two consecutive rounds have
no useful findings. Validate each finding; reproduce bugs and make regression
tests red before fixes. Keep runtime code compact. Focus every track on this
feature and its integrations.

1. Requirement completeness across every API and supplied artifact.
2. CLI invocation/presentation variants.
3. Bugs and behavior at valid boundaries.
4. Simplicity, duplication and abstraction quality.
5. Memory ownership and leaks.
6. Crashes and stability.
7. Unnecessary unsafe in Rust.
8. Weak tests and concrete counterexamples.
9. Documentation correctness and executable examples.
10. Uneven test coverage between languages.
11. Flaky tests, timing/load/order dependencies.
12. Data races, deadlocks and thread safety.

### Validated findings and regression evidence

- A V3-only mutation clearing cancelled workers passed the former FFI suite.
  The complete legacy-prefix fixture now covers all three statuses with nonzero
  diagnostics, and rejects that mutation.
- Swift's former nested readonly example could fail because the outer property
  was get-only. The negative consumer now mutates a local `ReferenceRun` copy,
  independently proving that its `results` is immutable.
- An inherited `CARGO_TARGET_DIR` made artifact builds succeed elsewhere and
  then fail to find their output. A red/green fixture regression now proves
  child-environment isolation without changing the parent.
- Delaying actual workers by 150 ms reproduced legitimate no-work failures in
  short metadata smoke measurements. Those checks now require either a correct
  completed score or the exact native no-work failure with absent score/ratio;
  reference checks remain unconditional. Deterministic completed/status/ratio
  fixtures stay strict. Delaying the coordinator also reproduced skipped short
  warmup notifications; cancellation lifetime tests now cancel on observed work
  progress using long cancellable windows.
- The same short-window assumption also existed in installed API/CLI probes,
  public Rust/C/Swift examples and the release wheel gate. Their checks retain
  exact success validation and reject unrelated failures. Closed-stderr success
  uses deterministic benchmark data through the real CLI entry points.
- ASCII-parent subprocess decoding disagreed with intentionally UTF-8 CLI
  children. Explicit child-output decoding is checked under an ASCII parent
  policy; UTF-8, ASCII and cp1251 output/error renderings remain covered.
- UTF-8 documentation/source reads and negative Swift compiler captures now
  explicitly use UTF-8. Permanent regressions use ASCII defaults and Unicode
  paths, rather than depending on the host locale.
- JSON ASCII escapes were invalid in Swift dependency literals and TOML emoji
  paths. Generated manifests retain UTF-8 characters; both public consumers
  execute from permanent Greek/emoji directory fixtures.

Initial scenario review used five agents, followed by five gap reviewers.
Each review track below uses three agents per round. A useful finding resets
the consecutive-clean-round count for that track.

| Track | Rounds | Final consecutive clean rounds |
| --- | --- | --- |
| Completeness | 2 | 2 |
| CLI variants/presentation | 2 | 2 |
| Bugs | 2 | 2 |
| Simplicity | 2 | 2 |
| Memory leaks | 2 | 2 |
| Crashes/stability | 2 | 2 |
| Rust unsafe | 2 | 2 |
| Test counterexamples | 3 | 2 |
| Documentation | 2 | 2 |
| Language parity | 2 | 2 |
| Flakes/load/order | 11 | 2 |
| Races/deadlocks/thread safety | 2 | 2 |

Findings outside the requested contract or without a demonstrated counterexample
were excluded. Whole-suite ASCII-locale stress also exposed two legacy CLI
captures with implicit decoding; their deterministic ASCII-parent regressions
were red before pinning child and capture encodings to UTF-8.

All 12 tracks finished with two consecutive clean rounds: 102 reviewer
assessments, plus the two groups of five scenario/gap reviewers.

## Final validation

- Python: 753 tests passed with eight workers, reversed collection order,
  ASCII locale, a one-column inherited terminal and an ignored parent SIGINT.
  Runtime statements and branches remain 100% covered.
- Rust: 46 library tests passed; runtime lines, regions, functions and
  instantiations remain 100% covered.
- Swift: 25 tests passed; runtime lines, regions, functions and instantiations
  remain 100% covered. LLVM native branch counters are unavailable.
- Public consumers passed from the crate, native static/dynamic ZIP, installed
  wheel and Swift archives. Unicode dependency/consumer paths and ASCII capture
  defaults are part of the permanent artifact fixtures.
- Worker/coordinator delay experiments, retained-reference concurrency,
  free-threaded Python checks, public typing consumers, Ruff, mypy and formatting
  checks passed. No checks depend on SSH or the calibration device.

## Whole-diff bug and compatibility review

A separate final review covered the complete change against HEAD, including
untracked sources, every public language API, CLI and release artifacts. Each
round used three reviewers; a useful finding reset the clean-round count.

| Round | Confirmed findings | Consecutive clean rounds |
| --- | --- | --- |
| 1 | Unnecessary C ABI bump; new Swift public error cases | 0 |
| 2 | Swift-only CI collection imported an uninstalled Python package | 0 |
| 3 | Release checks omitted mypy required by executable typing tests | 0 |
| 4 | None | 1 |
| 5 | None | 2 |

All four findings were reproduced and permanent regression tests failed before
their fixes:

- The frozen ABI-1 consumer passes against HEAD, failed against the preserved
  pre-fix ABI-2 library, and passes against the corrected library. Global ABI 1
  remains intact; V2/V3 are additive result formats. Both dynamic and static
  distributions execute the old consumer. The original Python wrapper also
  successfully runs both benchmarks and cancellation against the new library.
  A new Python wrapper given an old library without extension symbols now
  returns a clear missing-symbol failure before allocating a handle.
- An external Swift consumer exhaustively switches over the original three
  `BenchsetError` cases. It failed compilation with the added cases and compiles
  after restoring the enum. Metadata/ABI failures use `resultUnavailable`.
- A real pytest-only subprocess with no benchset metadata failed collection of
  the Swift artifact tests. Python-specific imports now stay in their callers;
  the permanent isolated regression proves Swift selection never imports
  benchset and needs no Python package installation.
- An environment with the exact release-check dependencies failed both typing
  fixtures with `No module named mypy`. Adding the existing development pin
  `mypy==1.14.1` makes them pass. The permanent workflow contract checks the
  executable pytest/mypy/build/filelock dependencies against development pins.

Adding the explicitly requested public Rust result fields requires updating
external struct literals; README states this source-compatibility boundary.
Benchmark call signatures remain intact. Python's original positional result
constructor and genuine HEAD pickles using protocols 2–5 remain compatible.

Final validation: 760 Python tests pass with eight workers, reversed order,
ASCII locale, one-column terminal and ignored parent SIGINT; runtime statements
and branches are 100% covered. Rust's 46 tests and Swift's 25 tests pass with
100% runtime line/region/function/instantiation coverage. LLVM native branch
counters remain unavailable. Public crate, native static/dynamic, wheel and
both Swift archive consumers pass. Ruff, mypy and Rust formatting checks pass.
Selected clean-environment/type/dependency checks also pass on Python 3.8.
