![logo](https://raw.githubusercontent.com/mutating/benchset/develop/docs/assets/logo_1.svg)

Sustained scalar CPU throughput, with an embeddable Rust core, a public C ABI,
a Python library, a CLI, and a macOS Swift package. No Python interpreter is
needed by applications embedding the native library.

The initial suite contains **`single_core`** (one computational worker) and
**`multi_core`** (all available computational workers). Both execute the same
fixed mixed workload and return a score in **completed iterations per second**.
Higher is faster. Each completed result also includes a **Pi equivalent**:
its throughput divided by the same benchmark's sustained Raspberry Pi 5 score.
`0.5` means half the Pi's throughput, `1.0` equal throughput, and `2.0` twice
the Pi's throughput. The CLI prints both iterations/s and `Pi equivalent`.

This README describes the current source tree. For changes not yet published,
use `pip install .` or the local Rust/Swift dependencies described below.

## Install and run

```sh
pip install benchset
benchset
benchset --bench single_core
benchset --bench multi_core --warmup 120 --duration 60
python -m benchset --warmup 0 --duration 1
```

The last command is a smoke test, not a sustained performance measurement.
Supported wheels include the native library: Rust is not required at runtime
or to install a wheel. `pip install .` and source distributions build the Rust
core automatically. If Cargo is not on PATH, the build downloads the pinned Rust
version with [rustup](https://rust-lang.github.io/rustup/installation/other.html)
into a private user cache and reuses it on later builds. No shell profile or
system PATH changes are made, and no manual environment setup is required.
The first such build needs internet access. Source compilation still requires
the platform linker/SDK: macOS Command Line Tools, a Linux C toolchain, or
Windows MSVC Build Tools with the Windows SDK. Prebuilt wheels need none of
these build tools.

| CLI option | Default | Meaning |
|---|---:|---|
| `--bench` | all | `all`, `single_core` or `multi_core`; `all` runs both in this order |
| `--warmup` | 120 | Seconds of continuous workload excluded from the score; zero is allowed |
| `--duration` | 60 | Seconds of measurement after warmup; must be positive |
| `-h`, `--help` | | Show options, defaults, examples and Pi equivalent interpretation |
| `--version` | | Show the library/benchmark version |

`--bench` is the public spelling of the selector. CLI settings are read through
`skelet`; configuration files and environment settings are not used.
Options accept both `--duration 0.5` and `--duration=0.5`, in any order;
durations accept decimal and scientific notation. A supplied option requires
a value. Repeating an option uses its last value.

A default full suite takes approximately six minutes. Each benchmark has its
own warmup. Warmup transitions directly into measurement without stopping the
workload. Terminal progress is shown on stdout only when attached to a TTY.
Completed result fields, including all measurement samples, are printed to
stdout. Failed/cancelled results go to stderr. Exit codes: 0 completed, 1
execution failure, 2 invalid CLI arguments, 130 interrupted/cancelled. The
suite stops after a failed or cancelled benchmark.
Each result is flushed immediately, including when stdout is redirected.
Progress fits the terminal width and is cleared before the result. Output
uses `× pi` and `—` when the stream supports them, with `x pi` and `-` as
fallbacks for restrictive encodings. A closed stdout pipe exits with code 1
without a traceback.

For example, a completed result at half the reference throughput includes
these lines (illustrative values; other fields and samples are omitted):

```text
Benchmark                  : single_core
Score                      : 23,615.614
Unit                       : iterations/s
Reference machine          : Raspberry Pi 5 Model B Rev 1.0 (pi)
Pi equivalent              : 0.5 × pi
```

The multiplier uses the full-precision native score, before display rounding.
An unavailable multiplier is displayed as `—` (`-` in the fallback format).

## Python API

Python 3.8–3.15, including free-threaded CPython, is supported. The wrapper uses
standard-library `ctypes` and loads the version-matched bundled native library.

```python
from benchset import run, run_all

result = run('single_core', warmup_seconds=120, measurement_seconds=60)
if result.success:
    print(f'{result.score} {result.unit}, {result.pi_equivalent} x {result.reference_machine.short_name}')
else:
    print(result.status, result.reason)

results = run_all(warmup_seconds=120, measurement_seconds=60)
print(result.reference_run.started_at, result.reference_run.results)
```

For a GUI or cancellable task, run a reusable runner on a background thread:

```python
from concurrent.futures import ThreadPoolExecutor
from benchset import BenchmarkRunner

runner = BenchmarkRunner()
with ThreadPoolExecutor(max_workers=1) as pool:
    future = pool.submit(
        runner.run, 'multi_core', warmup_seconds=120, measurement_seconds=60,
        on_progress=lambda p: print(p.phase, p.fraction),
    )
    # A UI handler on another thread may call runner.cancel().
    result = future.result()
```

Callbacks execute on the thread calling `run`, approximately ten times per
second and on observed phase transitions. A very short phase can finish before
the coordinator reports it, so callback sequences may omit that phase.
Callbacks receive the benchmark name, phase,
worker count, overall fraction, phase elapsed/target time and completed
iterations in that phase. Dispatch UI updates to the UI thread yourself and
return promptly. A Python callback exception cancels the workload and produces
a failed result; it does not propagate through C.

`BenchmarkRunner.cancel()` is thread-safe and requests cancellation without
waiting for the active call to finish. After `run()` returns, all native workers
are joined and the runner can be reused. Concurrent
runs are rejected. The native core enforces one active run per loaded library
instance, including calls from separate Python runners. Separate OS processes
or separately linked copies of the library can still compete for CPU time.

With Python's default SIGINT handler, Ctrl+C during a main-thread call cancels
the native work and raises `KeyboardInterrupt` after cleanup. Custom signal
handlers are preserved.

## What one iteration does

This is a fixed scalar integer workload, not a prediction of arbitrary
application execution time. Every complete iteration consists of:

1. **Integer arithmetic:** 256 dependent rounds of wrapping 64-bit addition,
   multiplication, XOR, shifts and rotation.
2. **Stateful parsing:** a pass over 4,096 bytes of deterministic structured
   input, using data-dependent transitions, digit accumulation, punctuation,
   signs, exponents and invalid-token handling.
3. **Indexed data access:** 1,024 dependent indexed reads and modifications of
   a 32,768-element `u64` table (256 KiB). The next index depends on the current
   data and accumulator.

The table and parser input belong to each worker. Initialization and allocation
occur before warmup; initialization time is excluded. The initial worker seeds
are fixed and distinct. State continues evolving during warmup and measurement.
Checksums make the computed values observable. The implementation uses explicit
wrapping operations so overflow has identical semantics across builds.

There is one fixed package of each kind per iteration. These are **not equal
percentages of elapsed time or CPU instructions**. The recipe and data sizes
remain identical across machines. Single-core uses one worker; multi-core sums
complete iterations from independent workers without shared data locks in the
measured kernel. Multicore throughput is measured, not extrapolated from the
single-core score.

Normal release compiler optimization is enabled. There is no `target-cpu=native`,
inline assembly, manual SIMD, hardware cryptography or CPU-specific dispatch.
The dependent operations prevent the main loops from becoming independent
vector workloads. Floating-point, SIMD, GPU, disk and network benchmarks are
outside this initial suite. Memory capacity is not measured.

## Timing and result interpretation

The default is 120 seconds of continuous warmup followed by 60 seconds of
measurement. Both durations are configurable in Rust, C, Swift, Python and CLI.
This is a starting point for sustained measurements; it cannot guarantee
thermal equilibrium on every device. Stable turbo operation is part of the
measured performance. The library does not change power settings, priorities,
CPU affinity, background activity or cooling conditions.

`multi_core` queries Rust's [`available_parallelism`](https://doc.rust-lang.org/std/thread/fn.available_parallelism.html).
The returned count is recorded in the result. This portable API is an estimate
of process-available parallelism; its documented limitations include Windows
processor groups above 64 logical CPUs and some affinity/quota configurations.
The library does not reserve cores or model sharing between isolates.

The primary score is `iterations / elapsed_seconds`, using monotonic wall time
after warmup. Workers publish counters in batches of eight iterations; a final
batch can slightly extend the requested interval. The actual measured duration
is included and used as the denominator. Worker setup and teardown are excluded.
Cancellation is checked between batches; completion waits for active workers.

The immutable Python result contains:

| Field | Meaning |
|---|---|
| `benchmark`, `version` | Name and benchmark/library version |
| `status`, `success`, `reason` | `completed`, `cancelled` or `failed`; explanation on failure |
| `score`, `unit` | Iterations/s; `score=None` unless completed |
| `pi_equivalent` | Double-precision `score / reference_score` for this benchmark; `None` unless completed |
| `reference_machine`, `reference_run` | Calibration machine and series, present for every result status |
| `workers` | Number of computational workers |
| `warmup_seconds`, `measurement_seconds` | Requested windows |
| `elapsed_seconds`, `iterations` | Actual measured wall time and completed work |
| `checksum` | XOR of final per-worker checksums; diagnostic, not a score |
| `samples` | Approximately one-second intervals plus the final partial interval |
| `variation_percent`, `unstable` | Time-weighted coefficient of variation, and whether it exceeds 10% |

Each sample stores cumulative measured elapsed time, interval duration,
interval iterations and iterations/s. Variation is `None` with fewer than two
samples. It reports within-run variability, not repeat-to-repeat accuracy.
An unstable run retains its score and completed status; the consumer decides
whether to use it. Cancellation/failure can retain partial diagnostics but
never produces a usable score or Pi equivalent. An unstable completed run
retains its Pi equivalent. Multi-core compares total measured throughput with
the multi-core reference; it does not normalize either score by worker count.

## The Pi reference

The immutable global machine record is `PI_REFERENCE_MACHINE` in Rust/Python
and `piReferenceMachine` in Swift. It describes **Raspberry Pi 5 Model B Rev
1.0**, abbreviated **`pi`**, with **Ubuntu Server 26.04 LTS**, **aarch64**, four
CPU cores and **8 GiB (8,589,934,592 bytes) of installed RAM**. Installed
capacity differs from the OS's usable `MemTotal` (8,317,272,064 bytes during
calibration). The metadata describes the reference machine, independently of
the device running the library.

The public names are:

| Interface | Result multiplier | Reference machine | Reference series |
|---|---|---|---|
| Python | `result.pi_equivalent: Optional[float]` | `result.reference_machine: ReferenceMachine` | `result.reference_run: ReferenceRun` |
| Rust | `result.pi_equivalent: Option<f64>` | `result.reference_machine: &'static ReferenceMachine` | `result.reference_run: &'static ReferenceRun` |
| Swift | `result.piEquivalent: Double?` | `result.referenceMachine: ReferenceMachine` | `result.referenceRun: ReferenceRun` |
| C / C++ V3 | `result.base.pi_equivalent`, when `result.base.has_pi_equivalent != 0` | `result.reference_machine` | `result.reference_run` + `benchset_reference_result` |

Every returned result carries these calibration records, including completed,
unstable, failed, cancelled, busy and validation-error results. They describe
the same fixed Pi reference for both workloads. Python fields default to the
existing globals (`result.reference_machine is PI_REFERENCE_MACHINE` and
`result.reference_run is PI_REFERENCE_RUN`); the new fields follow all previous
constructor arguments. `dataclasses.asdict(result)` includes both records as
nested data with numeric `started_at_unix_seconds`. Rust fields borrow the
public statics. Swift computed properties return the global structs with ordinary
value semantics. The globals
remain available without running a benchmark; C also retains its handle-free
`benchset_reference_machine` and `benchset_reference_run` getters.

Rust callers that construct `BenchmarkResult` with a struct literal must add
the new `pi_equivalent`, `reference_machine` and `reference_run` fields. This
source change follows from extending the public result struct; callers that
obtain results from benchmark functions keep their existing call signatures.

Every multiplier is a double-precision value. It is available for completed
results, including unstable ones. Failed/cancelled results have no multiplier:
`None` in Python/Rust, `nil` in Swift, or a zero presence flag in C.

`PI_REFERENCE_RUN` / Swift `piReferenceRun` describes the final six-run
calibration series, starting at its first warmup and ending with the final
measurement, before final cooling. It began on **2026-10-23 at
14:59:50.517391 UTC** (Unix seconds `1792767590.517391`); CPU temperature was
**41.35 °C** at the beginning and **66.65 °C** at the end. Preparation, plateau
profiling and the later cooling period are outside these boundaries. The
reference series ended on 23 October, including in Moscow; the larger session
with preparation and cooling crossed midnight.

Each benchmark had three independent measurements of 300 seconds, with cooling
verified by temperature readings between experiments. The stored score pools
all three measurements as **total iterations / total actual elapsed seconds**;
it is neither the best repeat nor a rounded display value:

| Benchmark | Iterations | Actual measurement seconds | Reference iterations/s |
|---|---:|---:|---:|
| `single_core` | 42,508,112 | 900.000162475 | 47231.227029007096 |
| `multi_core` | 163,757,952 | 900.000523364 | 181953.17419139884 |

The historical calibration used **1248 seconds (20 minutes 48 seconds) of
warmup before every measurement**. This is the conservative 960-second plateau
boundary plus 30%, based on both throughput and thermal establishment. There
was no substantial prolonged throughput decline during profiling. The
library's runtime defaults remain **120 seconds warmup / 60 seconds
measurement**; callers can choose their own windows. The reference warmup is
recorded provenance, not an automatic runtime setting.

The metadata types are `ReferenceMachine`, `ReferenceRun` and
`ReferenceBenchmarkResult` in Rust, Python and Swift. In Rust/Python their
fields are:

- `ReferenceMachine`: `name`, `short_name`, `os_name`, `os_version`,
  `cpu_architecture`, `memory_bytes` and `cpu_cores`.
- `ReferenceRun`: `started_at_unix_seconds`, `start_temperature_c`,
  `end_temperature_c`, `warmup_seconds` and `results`. Temperatures are in
  Celsius; the warmup is in seconds before **each** reference measurement.
- `ReferenceBenchmarkResult`: `benchmark` and `score` (iterations/s).
  `results` contains two pooled entries, one per benchmark, rather than six
  separate repeats.

Swift uses camelCase fields, such as `shortName`, `startedAtUnixSeconds` and
`startTemperatureC`. Python additionally offers the timezone-aware UTC
`started_at` datetime; Swift offers `startedAt: Date` for the same instant.
The frozen Python records use a tuple for results; Swift returns immutable
value snapshots. Reading the Python globals does not load the native library.

[Calibration values and provenance](native/reference-calibration.json) include
the original kernel commit, binary SHA-256, raw pooled totals and series
boundaries. The calibration used the unchanged scalar kernel at commit
`1b6771c1fe47514edfe72384b76a1d595cb2ff10`. Ambient conditions were not
controlled; the measured idle temperature shifted, so later cooling also
accepted a documented stable idle plateau. Ratios describe these workloads
and this reference configuration. The library reads these fixed values
locally; it does not connect to the Pi, query sensors or require the original
experiment directory.

The only editable package version is `[project].version` in `pyproject.toml`.
The version values in `Cargo.toml`, the package's `Cargo.lock` entry and the
Rust/Swift dependency pins below are generated copies. `benchset.__version__`
and result objects read installed Python package metadata through
`importlib.metadata`; no Python source version constant needs updating.
Rust, C and Swift read the version compiled from the generated Cargo metadata.
Python builds (including metadata, sdist, wheel and editable hooks),
`scripts/build_native.py` and `scripts/package_native.py` automatically refresh
these copies before building. Consumers decide which versions and measurement
settings are comparable.

## Embed in Rust

```toml
[dependencies]
benchset = "0.0.4"
# For changes from a local source checkout, use:
# benchset = { path = "../benchset" }
```

```rust
use benchset::{run, Benchmark, Cancellation, Options};
let cancellation = Cancellation::default();
let result = run(Benchmark::MultiCore, Options::default(), &cancellation,
                 |progress| println!("{:?}: {}", progress.phase, progress.fraction));
if let (Some(score), Some(equivalent)) = (result.score, result.pi_equivalent) {
    println!("{score:.3} iterations/s, {equivalent:.3} x {}", result.reference_machine.short_name);
} else {
    println!("{:?}: {}", result.status, result.reason);
}
println!("Reference warmup: {} s", result.reference_run.warmup_seconds);
```

The cancellation token can be cloned and cancelled from another thread. It is
sticky: create a new token for a new call. A short runnable example is in
`examples/native/rust.rs`.

## Embed through C ABI

Download the archive for the target OS/architecture from GitHub Releases, or
build locally:

```sh
cargo build --release --locked
```

Artifacts include `.a`/`.lib` for static linking and `.dylib`/`.so`/`.dll` for
dynamic loading. `include/benchset.h` documents structures, status codes and
lifetime rules. `examples/native/c.c` demonstrates configuration and progress.

```c
/* Inside main(); callback/context are supplied by the host application. */
if (benchset_abi_version() != BENCHSET_ABI_VERSION) return 1;
BenchsetConfig config;
if (!benchset_default_config(&config)) return 1;
config.benchmark = BENCHSET_MULTI_CORE;
BenchsetHandle *handle = benchset_create();
if (handle) {
    benchset_run(handle, &config, callback, context);
    BenchsetResultV3 result;
    if (benchset_result_v3(handle, &result, sizeof(result))) {
        const BenchsetReferenceMachine *machine = result.reference_machine;
        const BenchsetReferenceRun *reference = result.reference_run;
        /* machine and reference are available even after failure/cancellation. */
        printf("Reference: %s, warmup %.0f s\n", machine->name, reference->warmup_seconds);
        if (result.base.base.status == BENCHSET_COMPLETED && result.base.has_pi_equivalent) {
            /* result.base.base.score is iterations/s; result.base.pi_equivalent is a multiplier. */
        }
        /* The metadata pointers remain valid after benchset_free(handle). */
    }
    benchset_free(handle);
}
```

Call `benchset_cancel(handle)` from another thread or a progress callback.
Handles are single-use. Result/sample getters are used after `benchset_run`
returns. The reason string is borrowed until the handle is freed; the version
string is valid for the loaded library's lifetime. Progress pointers are
borrowed only during the callback. Do not free a handle while it is in use or
throw/unwind through a C callback. Invalid configuration produces a failed result.

The C ABI version remains 1. The additive, size-checked
`benchset_reference_machine`, `benchset_reference_run` and indexed
`benchset_reference_result` getters need no handle. Their
immutable strings remain valid for the loaded library's lifetime. Pass the
exact output structure size to these getters, `benchset_result_v2` and
`benchset_result_v3`;
failure returns zero and leaves the output unchanged. Any nonzero
`has_pi_equivalent` flag means present, including a multiplier of `0.0`.
Existing ABI-1 configurations and the original 88-byte `BenchsetResult`
layout/getter retain their behavior, so existing consumers continue to run.
New consumers check `benchset_abi_version()` and use the extended result getter
to access the multiplier. V2 and V3 name additive result formats; they use the
same ABI-1 configuration. Older libraries can report ABI 1 while lacking the
new getters, so the Python wrapper checks its required symbols before allocating
a handle.

`BenchsetResultV3` adds a complete `BenchsetResultV2 base` followed by const
`reference_machine` and `reference_run` pointers. Both pointers are non-null
for every available result. They and their strings remain immutable and valid
after `benchset_free(handle)`, until the library is unloaded. Do not use the
freed handle or its reason string. The ABI version remains 1, and the V1
(88-byte) and V2 (104-byte) structures and getters retain their layouts and
behavior. V3 is 120 bytes on
the supported 64-bit platforms.

The C metadata types are `BenchsetReferenceMachine`, `BenchsetReferenceRun`
and `BenchsetReferenceBenchmarkResult`. `BenchsetReferenceRun.result_count`
replaces the Rust/Python/Swift `results` collection: retrieve each entry with
`benchset_reference_result(index, &entry, sizeof(entry))`, using zero-based
indices smaller than `result_count`.

When statically linking Rust into a foreign application, system libraries may
also be required. Obtain their list with
`cargo rustc --release -- --print=native-static-libs`. Dynamic consumers must
ship the matching platform library with their application. Source consumers
control their compiler/toolchain settings; use our release artifacts for a
consistent build policy.

## Embed in Swift on macOS

In Xcode, add `https://github.com/mutating/benchset.git` as a package dependency
and select a released version. Release tags contain a `Package.swift` that
downloads the universal ARM64/Intel static XCFramework from that version's
GitHub Release and verifies its SHA-256 checksum. The Swift wrapper lives in
the same repository. Rust is not required by the consumer.

For a Swift manifest, add:

```swift
.package(url: "https://github.com/mutating/benchset.git", .exact("0.0.4"))
```

Add `.product(name: "Benchset", package: "benchset")` to your target's
dependencies. Pinning a version also pins the benchmark implementation.
For unreleased changes, build the Apple package from source as shown below
and use `.package(path: "../benchset")` instead.

For offline use, download and extract **`Benchset-Swift.zip`** from the release
and add the extracted directory as a local package. It contains the wrapper,
local manifest and XCFramework. Development branches use the local artifact
path and need the build step below; release tags use the remote binary target.

```swift
import Benchset
let runner = BenchmarkRunner()
// Execute from a background queue; dispatch progress updates to the UI queue.
let result = try runner.run(.multiCore, warmupSeconds: 120, measurementSeconds: 60) {
    progress in print(progress.fraction)
}
if let score = result.score, let equivalent = result.piEquivalent {
    print("\(score) \(result.unit), \(equivalent) x \(result.referenceMachine.shortName)")
} else {
    print(result.status, result.reason as Any)
}
print(result.referenceRun.startedAt, result.referenceRun.warmupSeconds)
// Another thread may call runner.cancel() while run is active.
```

To build the Apple package from source:

```sh
rustup target add aarch64-apple-darwin x86_64-apple-darwin
python scripts/package_native.py --target aarch64-apple-darwin --apple
swift build
```

This requires a macOS SDK and `lipo`. The packager generates the XCFramework
with standard bundle metadata and C headers; no Xcode project is required.
The Swift test suite uses Swift Testing (Swift 6 or newer). Run it with
`python scripts/check_native_coverage.py swift`; it also works with current
Command Line Tools, without full Xcode. The test executable targets macOS 13
while the library keeps the deployment target declared in `Package.swift`.

## Platforms and development

Release builds target macOS ARM64/Intel, Linux x86-64/ARM64 with glibc, and
Windows x86-64 MSVC. Native baseline targets are macOS 11 ARM64 / 10.12 Intel,
Linux glibc 2.17 and Windows 10, following ordinary Rust target requirements.
Older-OS execution still needs platform testing; the installed Python version
or a host application may impose a newer minimum. Linux musl and Windows ARM64
are not part of the initial prebuilt matrix.

```sh
python -m venv .venv
# Activate the virtual environment using your shell's normal command.
pip install -r requirements_dev.txt 'skelet>=0.0.21' wheel setuptools
pip install .
python scripts/build_native.py
python -m pytest
cargo test -- --test-threads=1
python -m build
```

`scripts/build_native.py` makes the source Python package usable without an
installation. Release wheels are platform-specific but interpreter-independent
(`py3-none-<platform>`). The sdist includes the native source. Tests validate
correctness, result accounting, progress, cancellation and API/CLI behavior;
they do not enforce speed thresholds in CI.

`python -m pytest tests/test_reference_artifacts.py` also builds and runs
consumers of unpacked crates, source distributions, wheels and native ZIPs
outside the checkout. Build the Apple package first to include its unpacked
Swift consumer and symbol checks for both XCFramework architectures. All smoke
measurements use short explicit windows, never the historical calibration
warmup. The [Pi equivalent test plan](tests/pi-equivalent-plan.md) records both
rounds of independent test reviews and their deduplicated scenarios.

## Test coverage

The [test matrix](tests/coverage-plan.md) combines five independent reviews and
covers measurement accounting, statistical boundaries, cancellation, reentry,
callback failures, C ABI validation, ownership and CLI behavior. Tests use
controlled initialization failures instead of exhausting machine resources.

Python coverage includes statements and branches, including CLI subprocesses;
CI installs a coverage startup hook for those subprocesses. Rust and Swift each
have independent 100% line/function/region/instantiation gates. Their current
LLVM reports do not provide branch counters, so no separate native branch
percentage is claimed. Only benchmark runtime and wrappers are counted;
build/release utilities and tests are outside those coverage percentages.

```bash
rustup component add llvm-tools-preview
python scripts/check_native_coverage.py rust
python scripts/check_native_coverage.py swift
```

Build the local XCFramework first as described above before the Swift command.
The checker uses the pinned Rust toolchain's LLVM tools; `--llvm-tools DIR`
can point to those tools when they are unpacked separately. Reports are written
to `target/coverage/{rust,swift}`. CI runs Rust coverage on Linux, Windows and
both Mac architectures, and Swift coverage on both Mac architectures.

## Publishing

All distributions are released from this repository by
`.github/workflows/release.yml`. A push to `main` checks the common package
version. An unreleased version is built and published automatically; a version
whose managed GitHub Release is already published is skipped. To release a new
version, change only `[project].version` in `pyproject.toml`, commit it and push
to `main`. Use an `X.Y.Z` version. Builds automatically synchronize the Cargo
manifest, lockfile entry and README dependency pins; no separate version command
is needed. The prepared release commit freezes those generated values for all
published source archives and tags. `python scripts/version.py --check` remains
an optional diagnostic; it does not have to pass on an unbuilt checkout after a
version edit.

For a local native build after editing the version, use
`python scripts/build_native.py`; for native/Swift archives use
`python scripts/package_native.py --target <target>` (add `--apple` for the
universal Swift package). Cargo reads its package version before `build.rs`
runs, so direct `cargo build`/`cargo package` use the already-generated manifest.
Published crates contain that manifest and still build without Python. Do not
create or move release tags manually.

The workflow performs these steps:

1. Test the Python API/CLI and release tooling on Linux, macOS and Windows;
   test Rust and build/install wheels across all five platform targets.
2. Build native archives, the offline Swift package and the universal
   XCFramework. Release compilation uses Rust **1.94.1** throughout, including
   the Linux containers; update the pin and package version together when
   changing the release toolchain.
3. Compute the XCFramework archive checksum and create a detached release
   commit containing generated version values and `Package.swift`. Its parent
   is the tested canonical source commit; unrelated source changes are rejected.
   `main` keeps its local development manifest.
4. Build the sdist and crate from that exact release commit, then retain the
   complete, checksummed release bundle as a GitHub Actions artifact.
5. Push `v<version>` at the prepared commit and populate a draft GitHub Release
   with the wheels, sources, native archives and checksum manifest.
6. Publish to PyPI and crates.io, then publish the populated GitHub Release.
   The Swift dependency URL is this repository and the same version tag.

The tag and publications happen in the same workflow run: tags pushed using
`GITHUB_TOKEN` do not trigger another release run. Only the publication job
uses the `release` GitHub environment, OIDC `id-token: write` and
`contents: write`; no additional Swift repository or personal token is needed.
Repository tag rules must allow this workflow to create the version tag.
The Git repository and release assets must be accessible to Swift consumers.

One-time publisher setup:

* **PyPI:** configure the Trusted Publisher for owner `mutating`, repository
  `benchset`, workflow filename `release.yml` and environment `release`.
  An already configured publisher with these values continues to work.
* **crates.io:** sign in with GitHub, verify the account email, and create an
  API token with publishing permission. Store it as the **repository secret**
  `CARGO_REGISTRY_TOKEN`. Missing credentials stop publication before a tag is
  created. Rust consumers do not need an account. See the
  [Cargo publishing guide](https://doc.rust-lang.org/cargo/reference/publishing.html).

To validate packaging without publishing, run the **Release** workflow
manually with `publish` left unchecked. To retry an incomplete release, run it
from `main` with `publish` checked, or rerun the failed publication job. If its
managed tag already exists, the workflow retrieves the original frozen bundle
using the workflow run ID recorded in the release commit; it does not rebuild
or move the tag. Uploaded PyPI/crates.io files are skipped only after their
checksums match. GitHub asset replacement is limited to the draft release.
Once all channels succeed, another push of the same version performs no uploads.

Frozen Actions bundles are retained for 90 days (subject to repository retention
settings). Resume an incomplete publication while its original bundle is
available. Code changes after a frozen release require a new version; a retry
continues to publish the original source snapshot. Concurrent release runs are
serialized. Local builds and build-only workflow runs do not publish anything.
