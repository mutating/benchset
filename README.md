# benchset

Sustained scalar CPU throughput, with an embeddable Rust core, a public C ABI,
a Python library, a CLI, and a macOS Swift package. No Python interpreter is
needed by applications embedding the native library.

The initial suite contains **`single_core`** (one computational worker) and
**`multi_core`** (all available computational workers). Both execute the same
fixed mixed workload and return a score in **completed iterations per second**.
Higher is faster. There is no reference machine or score normalization.

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
or to install a wheel. Installing from a source distribution requires Cargo
and a platform linker/SDK.

| CLI option | Default | Meaning |
|---|---:|---|
| `--bench` | all | `single_core` or `multi_core`; omitted runs both in this order |
| `--warmup` | 120 | Seconds of continuous workload excluded from the score; zero is allowed |
| `--duration` | 60 | Seconds of measurement after warmup; must be positive |
| `--help` | | Show syntax |
| `--version` | | Show the library/benchmark version |

`--bench` is the public spelling of the selector. CLI settings are read through
`skelet`; configuration files and environment settings are not used.

A default full suite takes approximately six minutes. Each benchmark has its
own warmup. Warmup transitions directly into measurement without stopping the
workload. Terminal progress is shown on stdout only when attached to a TTY.
Completed result fields, including all measurement samples, are printed to
stdout. Failed/cancelled results go to stderr. Exit codes: 0 completed, 1
execution failure, 2 invalid CLI arguments, 130 interrupted/cancelled. The
suite stops after a failed or cancelled benchmark.

## Python API

Python 3.8–3.15, including free-threaded CPython, is supported. The wrapper uses
standard-library `ctypes` and loads the version-matched bundled native library.

```python
from benchset import run, run_all

result = run('single_core', warmup_seconds=120, measurement_seconds=60)
if result.success:
    print(result.score, result.unit)
else:
    print(result.status, result.reason)

results = run_all(warmup_seconds=120, measurement_seconds=60)
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
second and on phase transitions. They receive the benchmark name, phase,
worker count, overall fraction, phase elapsed/target time and completed
iterations in that phase. Dispatch UI updates to the UI thread yourself and
return promptly. A Python callback exception cancels the workload and produces
a failed result; it does not propagate through C.

`BenchmarkRunner.cancel()` is thread-safe while a call is active. After it
returns, all native workers are joined and the runner can be reused. Concurrent
runs are rejected. The native core enforces one active run per loaded library
instance, including calls from separate Python runners. Separate OS processes
or separately linked copies of the library can still compete for CPU time.

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
never produces a usable score.

The native library and Python package share one version. Every release,
including a workload or default change, updates `Cargo.toml`, `pyproject.toml`
and `benchset/models.py` together; the build checks consistency. Consumers
decide which versions and measurement settings are comparable.

## Embed in Rust

```toml
[dependencies]
benchset = "0.0.1"
# Before registry publication, use:
# benchset = { git = "https://github.com/mutating/benchset.git" }
```

```rust
use benchset::{run, Benchmark, Cancellation, Options};
let cancellation = Cancellation::default();
let result = run(Benchmark::MultiCore, Options::default(), &cancellation,
                 |progress| println!("{:?}: {}", progress.phase, progress.fraction));
println!("{:?}", result.score);
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
BenchsetConfig config;
benchset_default_config(&config);
config.benchmark = BENCHSET_MULTI_CORE;
BenchsetHandle *handle = benchset_create();
if (handle) {
    benchset_run(handle, &config, callback, context);
    BenchsetResult result;
    if (benchset_result(handle, &result) && result.status == BENCHSET_COMPLETED) {
        /* result.score is iterations/s */
    }
    benchset_free(handle);
}
```

Call `benchset_cancel(handle)` from another thread or a progress callback.
Handles are single-use. Getters are used after `benchset_run` returns. Strings
are borrowed until the handle is freed; progress pointers are borrowed only
during the callback. Do not free a handle while it is in use or throw/unwind
through a C callback. Invalid configuration produces a failed result.

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
.package(url: "https://github.com/mutating/benchset.git", .exact("0.0.1"))
```

Add `.product(name: "Benchset", package: "benchset")` to your target's
dependencies. Pinning a version also pins the benchmark implementation.

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
version, update `Cargo.toml`, `pyproject.toml` and `benchset/models.py` together,
run `cargo generate-lockfile` to update `Cargo.lock`, commit the changes and
push to `main`. Use an `X.Y.Z` version. Do not create or move release tags
manually.

The workflow performs these steps:

1. Test the Python API/CLI and release tooling on Linux, macOS and Windows;
   test Rust and build/install wheels across all five platform targets.
2. Build native archives, the offline Swift package and the universal
   XCFramework. Release compilation uses Rust **1.94.1** throughout, including
   the Linux containers; update the pin and package version together when
   changing the release toolchain.
3. Compute the XCFramework archive checksum and create a detached release
   commit that changes only `Package.swift`. Its parent is the tested source
   commit. `main` keeps its local development manifest.
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
