//! Sustained scalar CPU throughput, available through Rust and a C ABI.
mod ffi;
mod kernel;
mod reference;

pub use reference::{
    ReferenceBenchmarkResult, ReferenceMachine, ReferenceRun, PI_REFERENCE_MACHINE,
    PI_REFERENCE_RUN,
};

use kernel::Kernel;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{mpsc, Arc, OnceLock};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
pub const DEFAULT_WARMUP_SECONDS: f64 = 120.0;
pub const DEFAULT_MEASUREMENT_SECONDS: f64 = 60.0;
pub const BENCHMARKS: [Benchmark; 2] = [Benchmark::SingleCore, Benchmark::MultiCore];
static ACTIVE: AtomicBool = AtomicBool::new(false);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u32)]
pub enum Benchmark {
    SingleCore = 1,
    MultiCore = 2,
}

impl Benchmark {
    pub fn name(self) -> &'static str {
        match self {
            Self::SingleCore => "single_core",
            Self::MultiCore => "multi_core",
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub struct Options {
    pub warmup: Duration,
    pub measurement: Duration,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            warmup: Duration::from_secs_f64(DEFAULT_WARMUP_SECONDS),
            measurement: Duration::from_secs_f64(DEFAULT_MEASUREMENT_SECONDS),
        }
    }
}

#[derive(Clone, Default)]
pub struct Cancellation(Arc<AtomicBool>);

impl Cancellation {
    pub fn cancel(&self) {
        self.0.store(true, Ordering::Relaxed);
    }
    pub fn is_cancelled(&self) -> bool {
        self.0.load(Ordering::Relaxed)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u32)]
pub enum Phase {
    Warmup = 1,
    Measurement = 2,
    Completed = 3,
    Cancelled = 4,
    Failed = 5,
}

#[derive(Clone, Copy, Debug)]
pub struct Progress {
    pub phase: Phase,
    pub workers: u32,
    pub fraction: f64,
    pub phase_elapsed_seconds: f64,
    pub phase_duration_seconds: f64,
    pub iterations: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u32)]
pub enum Status {
    Completed = 0,
    Cancelled = 1,
    Failed = 2,
}

#[derive(Clone, Debug)]
pub struct Sample {
    pub elapsed_seconds: f64,
    pub duration_seconds: f64,
    pub iterations: u64,
    pub iterations_per_second: f64,
}

#[derive(Clone, Debug)]
pub struct BenchmarkResult {
    pub benchmark: Benchmark,
    pub version: &'static str,
    pub status: Status,
    pub reason: String,
    pub score: Option<f64>,
    pub workers: u32,
    pub warmup_seconds: f64,
    pub measurement_seconds: f64,
    pub elapsed_seconds: f64,
    pub iterations: u64,
    pub checksum: u64,
    pub samples: Vec<Sample>,
    pub variation_percent: Option<f64>,
    pub unstable: bool,
    /// `score / benchmark.reference_score()`, using the same workload's Pi score.
    /// Present for completed results, including unstable ones; otherwise `None`.
    pub pi_equivalent: Option<f64>,
    /// Shared calibration, available for every status and independent of this run.
    pub reference_machine: &'static ReferenceMachine,
    pub reference_run: &'static ReferenceRun,
}

impl BenchmarkResult {
    pub(crate) fn failed(benchmark: Benchmark, options: Options, reason: String) -> Self {
        Self {
            benchmark,
            version: VERSION,
            status: Status::Failed,
            reason,
            score: None,
            workers: 0,
            warmup_seconds: options.warmup.as_secs_f64(),
            measurement_seconds: options.measurement.as_secs_f64(),
            elapsed_seconds: 0.0,
            iterations: 0,
            checksum: 0,
            samples: Vec::new(),
            variation_percent: None,
            unstable: false,
            pi_equivalent: None,
            reference_machine: &PI_REFERENCE_MACHINE,
            reference_run: &PI_REFERENCE_RUN,
        }
    }
}

/// Scores for the complete suite. Unfinished suites have no aggregate score.
#[derive(Clone, Debug, Default)]
pub struct AggregatedScores {
    /// Equal-weight geometric mean of the individual Pi equivalents, computed
    /// as exp(mean(ln(value))). A zero value makes the aggregate zero.
    pub pi_equivalent: Option<f64>,
}

#[derive(Clone, Debug)]
pub struct SuiteResult {
    pub results: Vec<BenchmarkResult>,
    pub aggregated_scores: AggregatedScores,
}

impl SuiteResult {
    pub fn new(results: Vec<BenchmarkResult>) -> Self {
        let complete = results.len() == BENCHMARKS.len()
            && results.iter().zip(BENCHMARKS).all(|(result, benchmark)| {
                result.benchmark == benchmark
                    && result.status == Status::Completed
                    && result
                        .pi_equivalent
                        .is_some_and(|value| value.is_finite() && value >= 0.0)
            });
        let pi_equivalent = complete.then(|| {
            if results
                .iter()
                .any(|result| result.pi_equivalent == Some(0.0))
            {
                return 0.0;
            }
            (results
                .iter()
                .map(|result| result.pi_equivalent.unwrap().ln())
                .sum::<f64>()
                / results.len() as f64)
                .exp()
        });
        Self {
            results,
            aggregated_scores: AggregatedScores { pi_equivalent },
        }
    }
}

/// Run every benchmark in order, stopping after failure or cancellation.
/// Progress includes the benchmark being measured and runs on the calling thread.
pub fn run_all(
    options: Options,
    cancellation: &Cancellation,
    mut on_progress: impl FnMut(Benchmark, Progress),
) -> SuiteResult {
    run_suite(|benchmark| {
        run(benchmark, options, cancellation, |progress| {
            on_progress(benchmark, progress)
        })
    })
}

fn run_suite(mut runner: impl FnMut(Benchmark) -> BenchmarkResult) -> SuiteResult {
    let mut results = Vec::new();
    for benchmark in BENCHMARKS {
        let result = runner(benchmark);
        let completed = result.status == Status::Completed;
        results.push(result);
        if !completed {
            break;
        }
    }
    SuiteResult::new(results)
}

struct ActiveGuard;
impl Drop for ActiveGuard {
    fn drop(&mut self) {
        ACTIVE.store(false, Ordering::Release);
    }
}

#[repr(align(64))]
#[derive(Default)]
struct Counters {
    warmup: AtomicU64,
    measurement: AtomicU64,
    finish_ns: AtomicU64,
    checksum: AtomicU64,
}

#[derive(Clone, Copy, Debug)]
struct Timing {
    start: Instant,
    warm_end: Instant,
    end: Instant,
}

impl Timing {
    fn is_warmup(self, now: Instant) -> bool {
        now < self.warm_end
    }

    fn sample_elapsed(self, now: Instant, last_elapsed: f64) -> Option<f64> {
        if self.is_warmup(now) || now >= self.end {
            return None;
        }
        let elapsed = now.duration_since(self.warm_end).as_secs_f64();
        (elapsed - last_elapsed >= 1.0).then_some(elapsed)
    }
}

struct Workers {
    handles: Vec<JoinHandle<()>>,
    stop: Arc<AtomicBool>,
}

impl Drop for Workers {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        for handle in self.handles.drain(..) {
            let _ = handle.join();
        }
    }
}

/// Run one benchmark. Callbacks run on the calling thread, at most about 10 Hz
/// plus phase transitions. Cancellation can be requested from any thread.
/// A cancellation token is sticky; create a fresh token for a new run.
/// Only one run is allowed at a time in this loaded instance of the library.
pub fn run(
    benchmark: Benchmark,
    options: Options,
    cancellation: &Cancellation,
    mut on_progress: impl FnMut(Progress),
) -> BenchmarkResult {
    run_with(
        benchmark,
        options,
        cancellation,
        &mut on_progress,
        Arc::new(System),
    )
}

// Initialization dependencies are replaceable in unit tests. The benchmark's
// timed kernel and production scheduling remain the same.
trait Runtime: Send + Sync + 'static {
    fn available_cpus(&self) -> std::io::Result<usize>;
    fn spawn(
        &self,
        index: usize,
        task: Box<dyn FnOnce() + Send>,
    ) -> std::io::Result<JoinHandle<()>>;
    fn kernel(&self, index: usize) -> Result<Kernel, String>;
}

#[derive(Clone)]
struct System;
impl Runtime for System {
    fn available_cpus(&self) -> std::io::Result<usize> {
        thread::available_parallelism().map(|count| count.get())
    }
    fn spawn(
        &self,
        index: usize,
        task: Box<dyn FnOnce() + Send>,
    ) -> std::io::Result<JoinHandle<()>> {
        thread::Builder::new()
            .name(format!("benchset-{index}"))
            .spawn(task)
    }
    fn kernel(&self, index: usize) -> Result<Kernel, String> {
        Kernel::new(index)
    }
}

fn run_with(
    benchmark: Benchmark,
    options: Options,
    cancellation: &Cancellation,
    on_progress: &mut dyn FnMut(Progress),
    runtime: Arc<dyn Runtime>,
) -> BenchmarkResult {
    let mut result = BenchmarkResult::failed(benchmark, options, String::new());
    if options.measurement.is_zero() {
        result.reason = "measurement duration must be positive".into();
        return result;
    }
    if ACTIVE
        .compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed)
        .is_err()
    {
        result.reason = "another benchmark is already running".into();
        return result;
    }
    let _active = ActiveGuard;
    let count = match benchmark {
        Benchmark::SingleCore => 1,
        Benchmark::MultiCore => match runtime.available_cpus() {
            Ok(count) => count,
            Err(error) => {
                result.reason = format!("cannot determine available CPUs: {error}");
                return result;
            }
        },
    };
    result.workers = count as u32;
    if cancellation.is_cancelled() {
        result.status = Status::Cancelled;
        result.reason = "cancelled before starting".into();
        return result;
    }

    let counters: Vec<_> = (0..count).map(|_| Arc::new(Counters::default())).collect();
    let timing = Arc::new(OnceLock::<Timing>::new());
    // A channel releases initialized workers. Unlike a barrier it also permits
    // cleanup when thread creation or initialization fails partway through.
    let start_gate = Arc::new((std::sync::Mutex::new(false), std::sync::Condvar::new()));
    let stop = Arc::new(AtomicBool::new(false));
    let mut workers = Workers {
        handles: Vec::new(),
        stop: stop.clone(),
    };
    let (ready_tx, ready_rx) = mpsc::channel();
    for (index, counter) in counters.iter().enumerate() {
        let counter = counter.clone();
        let token = cancellation.clone();
        let worker_stop = stop.clone();
        let timing = timing.clone();
        let gate = start_gate.clone();
        let ready = ready_tx.clone();
        let worker_runtime = runtime.clone();
        let handle = runtime.spawn(
            index,
            Box::new(move || {
                let kernel = worker_runtime.kernel(index);
                let _ = ready.send(kernel.as_ref().map(|_| ()).map_err(Clone::clone));
                drop(ready);
                let mut kernel = match kernel {
                    Ok(kernel) => kernel,
                    Err(_) => return,
                };
                let (lock, cv) = &*gate;
                let mut started = lock.lock().unwrap();
                while !*started {
                    started = cv.wait(started).unwrap();
                }
                drop(started);
                if worker_stop.load(Ordering::Relaxed) {
                    return;
                }
                let times = *timing.get().unwrap();
                let mut checksum = 0;
                loop {
                    let now = Instant::now();
                    if worker_stop.load(Ordering::Relaxed)
                        || token.is_cancelled()
                        || now >= times.end
                    {
                        break;
                    }
                    let warmup = times.is_warmup(now);
                    // Publish once per eight iterations, never contend on a shared counter.
                    for _ in 0..8 {
                        checksum = kernel.step();
                    }
                    let phase_counter = if warmup {
                        &counter.warmup
                    } else {
                        &counter.measurement
                    };
                    phase_counter.fetch_add(8, Ordering::Relaxed);
                }
                let end = Instant::now();
                let elapsed = end.saturating_duration_since(times.warm_end).as_nanos();
                counter
                    .finish_ns
                    .store(elapsed.min(u128::from(u64::MAX)) as u64, Ordering::Relaxed);
                counter.checksum.store(checksum, Ordering::Relaxed);
            }),
        );
        match handle {
            Ok(handle) => workers.handles.push(handle),
            Err(error) => {
                stop.store(true, Ordering::Relaxed);
                release(&start_gate);
                result.reason = format!("cannot start worker: {error}");
                return result;
            }
        }
    }
    drop(ready_tx);
    for _ in 0..count {
        match ready_rx.recv() {
            Ok(Ok(())) => (),
            other => {
                stop.store(true, Ordering::Relaxed);
                release(&start_gate);
                result.reason = format!("worker initialization failed: {other:?}");
                return result;
            }
        }
    }
    let start = Instant::now();
    let times = match start.checked_add(options.warmup).and_then(|warm_end| {
        warm_end.checked_add(options.measurement).map(|end| Timing {
            start,
            warm_end,
            end,
        })
    }) {
        Some(times) => times,
        None => {
            stop.store(true, Ordering::Relaxed);
            release(&start_gate);
            result.reason = "requested duration exceeds the monotonic clock range".into();
            return result;
        }
    };
    timing
        .set(times)
        .expect("timing is initialized exactly once");
    release(&start_gate);
    let total_seconds = options.warmup.as_secs_f64() + options.measurement.as_secs_f64();
    let mut last_progress = start.checked_sub(Duration::from_secs(1)).unwrap_or(start);
    let mut last_phase = Phase::Failed;
    let mut last_sample_elapsed = 0.0;
    let mut last_sample_iterations = 0;
    loop {
        let now = Instant::now();
        let warmup = times.is_warmup(now);
        let (phase, phase_start, phase_duration) = if warmup {
            (Phase::Warmup, times.start, options.warmup)
        } else {
            (Phase::Measurement, times.warm_end, options.measurement)
        };
        let iterations = sum(&counters, warmup);
        if let Some(elapsed) = times.sample_elapsed(now, last_sample_elapsed) {
            append_sample(
                &mut result.samples,
                elapsed,
                iterations,
                &mut last_sample_elapsed,
                &mut last_sample_iterations,
            );
        }
        if last_phase != phase || now.duration_since(last_progress) >= Duration::from_millis(100) {
            on_progress(Progress {
                phase,
                workers: count as u32,
                fraction: (now.duration_since(times.start).as_secs_f64() / total_seconds).min(1.0),
                phase_elapsed_seconds: now
                    .saturating_duration_since(phase_start)
                    .as_secs_f64()
                    .min(phase_duration.as_secs_f64()),
                phase_duration_seconds: phase_duration.as_secs_f64(),
                iterations,
            });
            last_phase = phase;
            last_progress = now;
        }
        if cancellation.is_cancelled() || now >= times.end {
            break;
        }
        thread::sleep(
            Duration::from_millis(10).min(times.end.saturating_duration_since(Instant::now())),
        );
    }
    stop.store(true, Ordering::Relaxed);
    let mut panicked = false;
    for handle in workers.handles.drain(..) {
        panicked |= handle.join().is_err();
    }
    collect_counters(&mut result, &counters);
    finish_samples(&mut result);
    finish_result(&mut result, panicked, cancellation.is_cancelled());
    on_progress(Progress {
        phase: match result.status {
            Status::Completed => Phase::Completed,
            Status::Cancelled => Phase::Cancelled,
            Status::Failed => Phase::Failed,
        },
        workers: count as u32,
        fraction: if result.status == Status::Completed {
            1.0
        } else {
            (Instant::now().duration_since(start).as_secs_f64() / total_seconds).min(1.0)
        },
        phase_elapsed_seconds: result.elapsed_seconds,
        phase_duration_seconds: options.measurement.as_secs_f64(),
        iterations: result.iterations,
    });
    result
}

fn release(gate: &(std::sync::Mutex<bool>, std::sync::Condvar)) {
    *gate.0.lock().unwrap() = true;
    gate.1.notify_all();
}

fn collect_counters(result: &mut BenchmarkResult, counters: &[Arc<Counters>]) {
    result.iterations = sum(counters, false);
    result.elapsed_seconds = counters
        .iter()
        .map(|c| c.finish_ns.load(Ordering::Relaxed))
        .max()
        .unwrap_or(0) as f64
        / 1e9;
    result.checksum = counters
        .iter()
        .fold(0, |value, c| value ^ c.checksum.load(Ordering::Relaxed));
}

fn sum(counters: &[Arc<Counters>], warmup: bool) -> u64 {
    counters
        .iter()
        .map(|counter| {
            if warmup {
                counter.warmup.load(Ordering::Relaxed)
            } else {
                counter.measurement.load(Ordering::Relaxed)
            }
        })
        .sum()
}

fn append_sample(
    samples: &mut Vec<Sample>,
    elapsed: f64,
    iterations: u64,
    last_elapsed: &mut f64,
    last_iterations: &mut u64,
) {
    let duration = elapsed - *last_elapsed;
    if duration <= 0.0 {
        return;
    }
    let completed = iterations.saturating_sub(*last_iterations);
    samples.push(Sample {
        elapsed_seconds: elapsed,
        duration_seconds: duration,
        iterations: completed,
        iterations_per_second: completed as f64 / duration,
    });
    *last_elapsed = elapsed;
    *last_iterations = iterations;
}

fn finish_samples(result: &mut BenchmarkResult) {
    // A worker can finish between the coordinator's clock read and counter
    // snapshot during cancellation. Rebuild any tail extending past its actual
    // finish time, keeping samples consistent with the final measurement.
    while result
        .samples
        .last()
        .is_some_and(|sample| sample.elapsed_seconds >= result.elapsed_seconds)
    {
        result.samples.pop();
    }
    let mut last_elapsed = result
        .samples
        .last()
        .map_or(0.0, |sample| sample.elapsed_seconds);
    let mut last_iterations = result.samples.iter().map(|sample| sample.iterations).sum();
    append_sample(
        &mut result.samples,
        result.elapsed_seconds,
        result.iterations,
        &mut last_elapsed,
        &mut last_iterations,
    );
}

fn finish_result(result: &mut BenchmarkResult, panicked: bool, cancelled: bool) {
    if panicked {
        result.reason = "a benchmark worker panicked".into();
    } else if cancelled {
        result.status = Status::Cancelled;
        result.reason = "cancelled".into();
    } else if result.iterations == 0 || result.elapsed_seconds == 0.0 {
        result.reason = "measurement window was too short to complete any work".into();
    } else {
        result.status = Status::Completed;
        let score = result.iterations as f64 / result.elapsed_seconds;
        result.score = Some(score);
        result.pi_equivalent = Some(pi_equivalent(result.benchmark, score));
        if result.samples.len() >= 2 {
            let variance = result
                .samples
                .iter()
                .map(|sample| {
                    sample.duration_seconds * (sample.iterations_per_second - score).powi(2)
                })
                .sum::<f64>()
                / result.elapsed_seconds;
            let variation_percent = variance.sqrt() / score * 100.0;
            result.variation_percent = Some(variation_percent);
            result.unstable = variation_percent > 10.0;
        }
    }
}

fn pi_equivalent(benchmark: Benchmark, score: f64) -> f64 {
    score / benchmark.reference_score()
}

#[cfg(test)]
#[path = "runtime_tests.rs"]
mod tests;

#[cfg(test)]
mod reference_tests;

#[cfg(test)]
mod suite_tests;
