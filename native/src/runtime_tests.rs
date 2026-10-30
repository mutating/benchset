use super::reference_tests::assert_reference_links;
use super::*;
use std::sync::atomic::AtomicUsize;

pub(super) static TEST_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

fn options() -> Options {
    Options {
        warmup: Duration::ZERO,
        measurement: Duration::from_millis(30),
    }
}
fn fresh_result() -> BenchmarkResult {
    BenchmarkResult::failed(Benchmark::SingleCore, options(), String::new())
}
fn sample_fields(sample: &Sample) -> (f64, f64, u64, f64) {
    (
        sample.elapsed_seconds,
        sample.duration_seconds,
        sample.iterations,
        sample.iterations_per_second,
    )
}
fn assert_recovered() {
    assert!(!ACTIVE.load(Ordering::Acquire));
    let token = Cancellation::default();
    let mut started = false;
    let result = run(
        Benchmark::SingleCore,
        Options {
            warmup: Duration::from_secs(10),
            measurement: Duration::from_secs(10),
        },
        &token,
        |p| {
            if matches!(p.phase, Phase::Warmup | Phase::Measurement) {
                started = true;
                token.cancel();
            }
        },
    );
    assert!(started, "{}", result.reason);
    assert_eq!(result.status, Status::Cancelled);
    assert_reference_links(&result);
    assert!(result.score.is_none());
    assert!(result.pi_equivalent.is_none());
    assert!(!ACTIVE.load(Ordering::Acquire));
}

#[test]
fn public_defaults_names_and_shared_cancellation() {
    assert_eq!(DEFAULT_WARMUP_SECONDS, 120.0);
    assert_eq!(DEFAULT_MEASUREMENT_SECONDS, 60.0);
    assert_eq!(Benchmark::SingleCore.name(), "single_core");
    assert_eq!(Benchmark::MultiCore.name(), "multi_core");
    assert_eq!(
        Options::default().warmup.as_secs_f64(),
        DEFAULT_WARMUP_SECONDS
    );
    assert_eq!(
        Options::default().measurement.as_secs_f64(),
        DEFAULT_MEASUREMENT_SECONDS
    );
    let token = Cancellation::default();
    assert!(!token.is_cancelled());
    token.clone().cancel();
    token.cancel();
    assert!(token.is_cancelled());
}

#[test]
fn real_runs_account_for_work_and_progress_on_caller() {
    let _serial = TEST_LOCK.lock().unwrap();
    for benchmark in [Benchmark::SingleCore, Benchmark::MultiCore] {
        let caller = thread::current().id();
        let mut events = Vec::new();
        let result = run(
            benchmark,
            Options {
                warmup: Duration::from_millis(20),
                measurement: Duration::from_millis(40),
            },
            &Cancellation::default(),
            |p| {
                assert_eq!(caller, thread::current().id());
                events.push(p);
            },
        );
        assert_eq!(result.status, Status::Completed, "{}", result.reason);
        assert_reference_links(&result);
        assert_eq!(result.version, VERSION);
        assert_eq!(
            result.workers as usize,
            if benchmark == Benchmark::SingleCore {
                1
            } else {
                thread::available_parallelism().unwrap().get()
            }
        );
        assert_eq!(
            result.score,
            Some(result.iterations as f64 / result.elapsed_seconds)
        );
        assert_eq!(
            result.pi_equivalent,
            Some(result.score.unwrap() / benchmark.reference_score())
        );
        assert!(result.iterations > 0);
        assert!(result.elapsed_seconds >= result.measurement_seconds);
        assert_eq!(result.warmup_seconds, 0.02);
        assert_eq!(result.measurement_seconds, 0.04);
        assert_eq!(
            result.samples.iter().map(|s| s.iterations).sum::<u64>(),
            result.iterations
        );
        assert!(
            (result
                .samples
                .iter()
                .map(|s| s.duration_seconds)
                .sum::<f64>()
                - result.elapsed_seconds)
                .abs()
                < 1e-12
        );
        for sample in &result.samples {
            assert_eq!(
                sample.iterations_per_second,
                sample.iterations as f64 / sample.duration_seconds
            );
        }
        assert!(result.variation_percent.is_none());
        assert!(matches!(
            events[0].phase,
            Phase::Warmup | Phase::Measurement
        ));
        assert!(events.iter().any(|p| p.phase == Phase::Measurement));
        let final_event = events.last().unwrap();
        assert_eq!(final_event.phase, Phase::Completed);
        assert_eq!(final_event.fraction, 1.0);
        assert_eq!(final_event.iterations, result.iterations);
        for p in &events {
            assert!((0.0..=1.0).contains(&p.fraction));
            if matches!(p.phase, Phase::Warmup | Phase::Measurement) {
                assert!(p.phase_elapsed_seconds <= p.phase_duration_seconds);
                let elapsed = p.phase_elapsed_seconds
                    + if p.phase == Phase::Measurement {
                        0.02
                    } else {
                        0.0
                    };
                assert!((p.fraction - (elapsed / 0.06).min(1.0)).abs() < 1e-12);
            } else {
                assert_eq!(p.phase_elapsed_seconds, result.elapsed_seconds);
            }
            assert_eq!(p.workers, result.workers);
        }
        assert!(events
            .windows(2)
            .all(|pair| pair[0].fraction <= pair[1].fraction));
    }
}

/// Replay the complete single-worker workload, including an empty measurement.
/// Waiting for the worker from the first callback exercises a naturally expired
/// window before coordinator cancellation can bypass the checksum publication.
/// The oracle accepts actual work when a platform's clock permits it, and uses
/// zero for no work, protecting against a fabricated initial checksum.
#[test]
fn single_worker_checksum_matches_all_completed_kernel_steps() {
    let _serial = TEST_LOCK.lock().unwrap();
    for measurement in [options().measurement, Duration::from_nanos(1)] {
        let (finished_tx, finished_rx) = mpsc::channel();
        let mut observed = false;
        let result = run_with(
            Benchmark::SingleCore,
            Options {
                measurement,
                ..options()
            },
            &Cancellation::default(),
            &mut |_| {
                if !observed {
                    observed = true;
                    finished_rx.recv_timeout(Duration::from_secs(5)).unwrap();
                }
            },
            Arc::new(ObservedRuntime(finished_tx)),
        );
        assert_eq!(
            result.status,
            if result.iterations > 0 {
                Status::Completed
            } else {
                Status::Failed
            },
            "{}",
            result.reason
        );
        // With no warmup, every executed step is included in the iteration count.
        let mut kernel = Kernel::new(0).unwrap();
        let mut expected = 0;
        for _ in 0..result.iterations {
            expected = kernel.step();
        }
        assert_eq!(result.checksum, expected);
    }
}

#[test]
fn zero_warmup_samples_and_late_cancellation() {
    let _serial = TEST_LOCK.lock().unwrap();
    let mut events = Vec::new();
    let token = Cancellation::default();
    let result = run(
        Benchmark::SingleCore,
        Options {
            warmup: Duration::ZERO,
            measurement: Duration::from_millis(1300),
        },
        &token,
        |p| {
            assert_ne!(p.phase, Phase::Warmup);
            if p.phase == Phase::Measurement && p.phase_elapsed_seconds > 1.1 {
                token.cancel();
            }
            events.push(p);
        },
    );
    assert_eq!(result.status, Status::Cancelled);
    assert!(result.score.is_none());
    assert!(result.pi_equivalent.is_none());
    assert!(!result.samples.is_empty());
    assert_eq!(
        result.samples.iter().map(|s| s.iterations).sum::<u64>(),
        result.iterations
    );
    assert_eq!(events.last().unwrap().phase, Phase::Cancelled);
    // Scheduling may skip intermediate snapshots. Exact multi-sample statistics
    // are covered separately with fixed counters and elapsed times.
    let result = run(
        Benchmark::SingleCore,
        Options {
            warmup: Duration::ZERO,
            measurement: Duration::from_millis(1100),
        },
        &Cancellation::default(),
        |_| {},
    );
    assert_eq!(result.status, Status::Completed);
    assert!(!result.samples.is_empty());
    assert_eq!(
        result.variation_percent.is_some(),
        result.samples.len() >= 2
    );
    assert_eq!(
        result.unstable,
        result.variation_percent.is_some_and(|value| value > 10.0)
    );
    assert_eq!(
        result.pi_equivalent,
        Some(result.score.unwrap() / 47231.227029007096)
    );
}

#[test]
fn sticky_precancel_and_cancel_in_each_phase() {
    let _serial = TEST_LOCK.lock().unwrap();
    let token = Cancellation::default();
    token.cancel();
    for _ in 0..2 {
        let result = run(Benchmark::SingleCore, options(), &token, |_| {
            panic!("pre-cancel must not invoke progress")
        });
        assert_eq!(result.status, Status::Cancelled);
        assert_reference_links(&result);
        assert_eq!(result.reason, "cancelled before starting");
        assert!(result.pi_equivalent.is_none());
    }
    for phase in [Phase::Warmup, Phase::Measurement] {
        let token = Cancellation::default();
        let mut last = Phase::Failed;
        let result = run(
            Benchmark::MultiCore,
            Options {
                warmup: if phase == Phase::Warmup {
                    Duration::from_secs(10)
                } else {
                    Duration::ZERO
                },
                measurement: Duration::from_millis(100),
            },
            &token,
            |p| {
                if p.phase == phase {
                    token.clone().cancel();
                }
                last = p.phase;
            },
        );
        assert_eq!(result.status, Status::Cancelled);
        assert_reference_links(&result);
        assert_eq!(last, Phase::Cancelled);
        assert!(result.score.is_none());
        assert!(result.pi_equivalent.is_none());
    }
    assert_recovered();
}

#[test]
fn concurrent_and_reentrant_runs_do_not_damage_winner() {
    let _serial = TEST_LOCK.lock().unwrap();
    let token = Cancellation::default();
    let child_token = token.clone();
    let (started_tx, started_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let handle = thread::spawn(move || {
        let mut first = true;
        run(
            Benchmark::SingleCore,
            Options {
                warmup: Duration::from_secs(10),
                measurement: Duration::from_secs(10),
            },
            &child_token,
            |_| {
                if first {
                    first = false;
                    let nested = run(
                        Benchmark::MultiCore,
                        options(),
                        &Cancellation::default(),
                        |_| {},
                    );
                    assert_eq!(nested.reason, "another benchmark is already running");
                    assert_reference_links(&nested);
                    assert!(nested.pi_equivalent.is_none());
                    started_tx.send(()).unwrap();
                    release_rx.recv().unwrap();
                }
            },
        )
    });
    started_rx.recv_timeout(Duration::from_secs(5)).unwrap();
    let loser = Cancellation::default();
    let result = run(Benchmark::MultiCore, options(), &loser, |_| {
        panic!("busy callback")
    });
    assert_eq!(result.status, Status::Failed);
    assert_reference_links(&result);
    assert_eq!(result.reason, "another benchmark is already running");
    assert!(result.pi_equivalent.is_none());
    loser.cancel();
    let busy = run(Benchmark::SingleCore, options(), &loser, |_| {});
    assert_eq!(busy.reason, "another benchmark is already running");
    assert_reference_links(&busy);
    assert!(busy.pi_equivalent.is_none());
    let invalid = run(
        Benchmark::SingleCore,
        Options {
            measurement: Duration::ZERO,
            ..options()
        },
        &loser,
        |_| {},
    );
    assert_eq!(invalid.reason, "measurement duration must be positive");
    assert_reference_links(&invalid);
    assert!(invalid.pi_equivalent.is_none());
    assert!(!token.is_cancelled());
    token.cancel();
    release_tx.send(()).unwrap();
    let cancelled = handle.join().unwrap();
    assert_eq!(cancelled.status, Status::Cancelled);
    assert_reference_links(&cancelled);
    assert!(cancelled.pi_equivalent.is_none());
    assert_recovered();
}

#[test]
fn callback_panics_release_workers_and_active_guard() {
    let _serial = TEST_LOCK.lock().unwrap();
    for phase in [Phase::Warmup, Phase::Measurement, Phase::Completed] {
        let result = std::panic::catch_unwind(|| {
            run(
                Benchmark::MultiCore,
                Options {
                    warmup: if phase == Phase::Warmup {
                        Duration::from_secs(10)
                    } else {
                        Duration::ZERO
                    },
                    measurement: Duration::from_millis(30),
                },
                &Cancellation::default(),
                |p| {
                    if p.phase == phase {
                        panic!("callback fault");
                    }
                },
            )
        });
        assert!(result.is_err());
        assert_recovered();
    }
}

#[test]
fn terminal_cancel_does_not_change_completed_result() {
    let _serial = TEST_LOCK.lock().unwrap();
    let token = Cancellation::default();
    let result = run(Benchmark::SingleCore, options(), &token, |p| {
        if p.phase == Phase::Completed {
            token.cancel();
        }
    });
    assert_eq!(result.status, Status::Completed);
    assert!(token.is_cancelled());
    assert_eq!(
        result.pi_equivalent,
        Some(result.score.unwrap() / 47231.227029007096)
    );
}

#[derive(Clone)]
struct ObservedRuntime(mpsc::Sender<()>);
impl Runtime for ObservedRuntime {
    fn available_cpus(&self) -> std::io::Result<usize> {
        System.available_cpus()
    }
    fn spawn(
        &self,
        index: usize,
        task: Box<dyn FnOnce() + Send>,
    ) -> std::io::Result<JoinHandle<()>> {
        let finished = self.0.clone();
        System.spawn(
            index,
            Box::new(move || {
                task();
                finished.send(()).unwrap();
            }),
        )
    }
    fn kernel(&self, index: usize) -> Result<Kernel, String> {
        System.kernel(index)
    }
}

/// A delayed callback must not inflate measured throughput time or progress.
/// Crossing both phase and final deadlines covers clamping during measurement
/// and cancellation after workers have already finished their real work.
#[test]
fn callback_delay_is_excluded_from_throughput_elapsed() {
    let _serial = TEST_LOCK.lock().unwrap();
    for (warmup, cancel_after_deadline) in
        [(false, false), (false, true), (true, false), (true, true)]
    {
        let (finished_tx, finished_rx) = mpsc::channel();
        let before = Instant::now();
        let mut delayed = false;
        let cancellation = Cancellation::default();
        let mut events = Vec::new();
        let result = run_with(
            Benchmark::SingleCore,
            Options {
                warmup: if warmup {
                    Duration::from_millis(20)
                } else {
                    Duration::ZERO
                },
                ..options()
            },
            &cancellation,
            &mut |p| {
                events.push(p);
                if matches!(p.phase, Phase::Warmup | Phase::Measurement) && !delayed {
                    delayed = true;
                    // Wait for an observed worker exit before delaying the callback;
                    // this makes the assertion independent of hardware throughput.
                    finished_rx.recv_timeout(Duration::from_secs(5)).unwrap();
                    thread::sleep(Duration::from_millis(20));
                    if cancel_after_deadline {
                        cancellation.cancel();
                    }
                }
            },
            Arc::new(ObservedRuntime(finished_tx)),
        );
        assert_eq!(
            result.status,
            if cancel_after_deadline {
                Status::Cancelled
            } else {
                Status::Completed
            }
        );
        assert!(events
            .iter()
            .all(|event| (0.0..=1.0).contains(&event.fraction)));
        assert_eq!(events.last().unwrap().fraction, 1.0);
        assert!(before.elapsed().as_secs_f64() - result.elapsed_seconds >= 0.02);
        assert_eq!(result.samples.len(), 1);
        assert!(result.variation_percent.is_none());
        assert!(!result.unstable);
        if cancel_after_deadline {
            assert!(result.score.is_none() && result.pi_equivalent.is_none());
        } else {
            assert_eq!(
                result.pi_equivalent,
                Some((result.iterations as f64 / result.elapsed_seconds) / 47231.227029007096)
            );
        }
    }
}

#[derive(Clone, Copy, Debug)]
enum Fault {
    Cpu,
    Spawn(usize),
    Init,
    Disconnected,
    WorkerPanic,
}
#[derive(Clone)]
struct FaultRuntime {
    fault: Fault,
    active: Arc<AtomicUsize>,
    cancellation: Cancellation,
}
struct ThreadGuard(Arc<AtomicUsize>, Cancellation);
impl Drop for ThreadGuard {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::SeqCst);
        if thread::panicking() {
            // End coordination after the fault; joining still reports the panic.
            self.1.cancel();
        }
    }
}
impl Runtime for FaultRuntime {
    fn available_cpus(&self) -> std::io::Result<usize> {
        if matches!(self.fault, Fault::Cpu) {
            Err(std::io::Error::other("cpu fault"))
        } else {
            Ok(2)
        }
    }
    fn spawn(
        &self,
        index: usize,
        task: Box<dyn FnOnce() + Send>,
    ) -> std::io::Result<JoinHandle<()>> {
        if matches!(self.fault, Fault::Spawn(i) if i == index) {
            return Err(std::io::Error::other("spawn fault"));
        }
        let active = self.active.clone();
        let cancellation = self.cancellation.clone();
        System.spawn(
            index,
            Box::new(move || {
                active.fetch_add(1, Ordering::SeqCst);
                let _guard = ThreadGuard(active, cancellation);
                task();
            }),
        )
    }
    fn kernel(&self, index: usize) -> Result<Kernel, String> {
        match self.fault {
            Fault::Init if index == 1 => Err("kernel fault".into()),
            Fault::Disconnected if index == 1 => panic!("initialization panic"),
            Fault::WorkerPanic if index == 0 => Ok(Kernel::broken_for_test()),
            _ => Kernel::new(index),
        }
    }
}

#[test]
fn initialization_faults_join_partial_workers_and_recover() {
    let _serial = TEST_LOCK.lock().unwrap();
    for (fault, reason) in [
        (Fault::Cpu, "cannot determine available CPUs"),
        (Fault::Spawn(0), "cannot start worker"),
        (Fault::Spawn(1), "cannot start worker"),
        (Fault::Init, "worker initialization failed"),
        (Fault::Disconnected, "worker initialization failed"),
        (Fault::WorkerPanic, "a benchmark worker panicked"),
    ] {
        let active = Arc::new(AtomicUsize::new(0));
        let cancellation = Cancellation::default();
        let result = run_with(
            Benchmark::MultiCore,
            Options {
                measurement: Duration::from_secs(10),
                ..options()
            },
            &cancellation,
            &mut |_| {},
            Arc::new(FaultRuntime {
                fault,
                active: active.clone(),
                cancellation: cancellation.clone(),
            }),
        );
        assert_eq!(result.status, Status::Failed, "{fault:?}");
        assert_reference_links(&result);
        assert!(result.reason.contains(reason), "{}", result.reason);
        assert!(result.score.is_none());
        assert!(result.pi_equivalent.is_none());
        assert_eq!(
            active.load(Ordering::SeqCst),
            0,
            "workers still alive after {fault:?}"
        );
        assert_recovered();
    }
}

/// Invalid requests must report no performed work or elapsed measurement time.
/// This protects diagnostics from carrying fabricated defaults before workers start.
#[test]
fn invalid_and_overflow_durations_do_not_leave_active_workers() {
    let _serial = TEST_LOCK.lock().unwrap();
    for (opts, reason) in [
        (
            Options {
                measurement: Duration::ZERO,
                ..options()
            },
            "must be positive",
        ),
        (
            Options {
                warmup: Duration::MAX,
                ..options()
            },
            "clock range",
        ),
        (
            Options {
                measurement: Duration::MAX,
                ..options()
            },
            "clock range",
        ),
    ] {
        let result = run(
            Benchmark::SingleCore,
            opts,
            &Cancellation::default(),
            |_| panic!("invalid options emitted progress"),
        );
        assert_eq!(result.status, Status::Failed);
        assert_reference_links(&result);
        assert!(result.reason.contains(reason));
        assert!(result.pi_equivalent.is_none());
        assert_eq!((result.iterations, result.checksum), (0, 0));
        assert_eq!(result.elapsed_seconds, 0.0);
        assert!(result.samples.is_empty());
        if opts.measurement.is_zero() {
            assert_eq!(result.workers, 0);
        }
        assert_recovered();
    }
}

/// Keep independent worker counters on separate minimum-size cache lines.
/// Losing alignment/padding introduces false sharing and changes measured
/// parallel throughput even when every numeric result remains self-consistent.
#[test]
fn sample_deltas_and_counter_selection() {
    assert!(std::mem::align_of::<Counters>() >= 64);
    assert_eq!(std::mem::size_of::<Counters>() % 64, 0);
    let counters: Vec<_> = [(13, 17), (23, 29)]
        .iter()
        .map(|&(warmup, measurement)| {
            Arc::new(Counters {
                warmup: AtomicU64::new(warmup),
                measurement: AtomicU64::new(measurement),
                ..Counters::default()
            })
        })
        .collect();
    assert_eq!(sum(&[], true), 0);
    assert_eq!(sum(&counters, true), 36);
    assert_eq!(sum(&counters, false), 46);
    let (mut samples, mut elapsed, mut iterations) = (Vec::new(), 0.0, 0);
    append_sample(&mut samples, 2.0, 10, &mut elapsed, &mut iterations);
    assert_eq!(samples[0].iterations_per_second, 5.0);
    for invalid_elapsed in [2.0, 1.0] {
        append_sample(
            &mut samples,
            invalid_elapsed,
            999,
            &mut elapsed,
            &mut iterations,
        );
    }
    assert_eq!(samples.len(), 1);
    assert_eq!((elapsed, iterations), (2.0, 10));
    append_sample(&mut samples, 3.0, 10, &mut elapsed, &mut iterations);
    assert_eq!(samples[1].iterations, 0);
    append_sample(&mut samples, 4.0, 8, &mut elapsed, &mut iterations);
    assert_eq!(samples[2].iterations, 0);
    assert_eq!((elapsed, iterations), (4.0, 8));
}

fn measured(samples: &[(f64, u64)]) -> BenchmarkResult {
    let mut result = fresh_result();
    let (mut elapsed, mut iterations) = (0.0, 0);
    for &(duration, count) in samples {
        let (next_elapsed, next_iterations) = (elapsed + duration, iterations + count);
        append_sample(
            &mut result.samples,
            next_elapsed,
            next_iterations,
            &mut elapsed,
            &mut iterations,
        );
    }
    result.elapsed_seconds = elapsed;
    result.iterations = iterations;
    finish_result(&mut result, false, false);
    result
}

#[test]
fn weighted_statistics_and_exact_stability_boundary() {
    for samples in [&[][..], &[(1.0, 100)][..]] {
        assert!(measured(samples).variation_percent.is_none());
    }
    assert_eq!(
        measured(&[(1.0, 100), (3.0, 300)]).variation_percent,
        Some(0.0)
    );
    let result = measured(&[(1.0, 80), (3.0, 360)]);
    assert_reference_links(&result);
    assert_eq!(result.status, Status::Completed);
    assert_eq!(result.score, Some(110.0));
    assert_eq!(result.pi_equivalent, Some(110.0 / 47231.227029007096));
    assert_ne!(
        result.pi_equivalent,
        Some(((80.0 / 47231.227029007096) + (120.0 / 47231.227029007096)) / 2.0)
    );
    assert!((result.variation_percent.unwrap() - 300.0_f64.sqrt() / 110.0 * 100.0).abs() < 1e-12);
    assert!(result.unstable);
    let boundary = measured(&[(1.0, 90), (1.0, 110)]);
    assert_reference_links(&boundary);
    assert_eq!(boundary.status, Status::Completed);
    assert_eq!(boundary.variation_percent, Some(10.0));
    assert!(!boundary.unstable);
    assert_eq!(boundary.pi_equivalent, Some(100.0 / 47231.227029007096));
    let unstable = measured(&[(1.0, 89), (1.0, 111)]);
    assert_reference_links(&unstable);
    assert_eq!(unstable.status, Status::Completed);
    assert!(unstable.unstable);
    assert_eq!(unstable.pi_equivalent, Some(100.0 / 47231.227029007096));
}

#[test]
fn failed_cancelled_and_no_work_statistics_keep_diagnostics() {
    for (panicked, cancelled, reason, status) in [
        (true, true, "worker panicked", Status::Failed),
        (false, true, "cancelled", Status::Cancelled),
    ] {
        let mut result = fresh_result();
        result.samples = vec![
            Sample {
                elapsed_seconds: 0.75,
                duration_seconds: 0.75,
                iterations: 30,
                iterations_per_second: 40.0,
            },
            Sample {
                elapsed_seconds: 1.25,
                duration_seconds: 0.5,
                iterations: 40,
                iterations_per_second: 80.0,
            },
        ];
        result.iterations = 70;
        result.elapsed_seconds = 1.25;
        result.checksum = 0x123456789abcdef0;
        finish_result(&mut result, panicked, cancelled);
        assert_eq!(result.status, status);
        assert_reference_links(&result);
        assert!(result.reason.contains(reason));
        assert!(result.score.is_none());
        assert!(result.pi_equivalent.is_none());
        assert_eq!(result.iterations, 70);
        assert_eq!(result.elapsed_seconds, 1.25);
        assert_eq!(result.checksum, 0x123456789abcdef0);
        assert_eq!(
            result.samples.iter().map(sample_fields).collect::<Vec<_>>(),
            vec![(0.75, 0.75, 30, 40.0), (1.25, 0.5, 40, 80.0)]
        );
    }
    for (iterations, elapsed) in [(0, 1.0), (8, 0.0)] {
        let mut result = fresh_result();
        result.iterations = iterations;
        result.elapsed_seconds = elapsed;
        finish_result(&mut result, false, false);
        assert_eq!(result.status, Status::Failed);
        assert_reference_links(&result);
        assert!(result.reason.contains("too short"));
        assert!(result.score.is_none());
        assert!(result.pi_equivalent.is_none());
    }
}

#[test]
fn sample_repair_retains_valid_history_and_rebuilds_only_the_tail() {
    let mut result = fresh_result();
    result.samples = vec![
        Sample {
            elapsed_seconds: 0.75,
            duration_seconds: 0.75,
            iterations: 30,
            iterations_per_second: 40.0,
        },
        Sample {
            elapsed_seconds: 1.5,
            duration_seconds: 0.75,
            iterations: 90,
            iterations_per_second: 120.0,
        },
    ];
    result.elapsed_seconds = 1.25;
    result.iterations = 70;
    finish_samples(&mut result);
    assert_eq!(
        result.samples.iter().map(sample_fields).collect::<Vec<_>>(),
        vec![(0.75, 0.75, 30, 40.0), (1.25, 0.5, 40, 80.0)]
    );
}

#[test]
fn final_sample_repairs_snapshot_after_worker_finish() {
    for elapsed in [0.0, 0.99, 1.0, 1.99, 2.0, 2.01] {
        let mut result = fresh_result();
        result.samples = vec![
            Sample {
                elapsed_seconds: 1.0,
                duration_seconds: 1.0,
                iterations: 100,
                iterations_per_second: 100.0,
            },
            Sample {
                elapsed_seconds: 2.0,
                duration_seconds: 1.0,
                iterations: 100,
                iterations_per_second: 100.0,
            },
        ];
        result.elapsed_seconds = elapsed;
        result.iterations = 208;
        finish_samples(&mut result);
        if elapsed == 0.0 {
            assert!(result.samples.is_empty());
        } else {
            assert_eq!(
                result.samples.iter().map(|s| s.iterations).sum::<u64>(),
                208
            );
            assert!(
                (result
                    .samples
                    .iter()
                    .map(|s| s.duration_seconds)
                    .sum::<f64>()
                    - elapsed)
                    .abs()
                    < 1e-12
            );
            assert!(result
                .samples
                .iter()
                .all(|s| s.duration_seconds > 0.0 && s.elapsed_seconds <= elapsed));
            assert_eq!(result.samples.last().unwrap().elapsed_seconds, elapsed);
        }
        finish_result(&mut result, false, false);
        if elapsed == 0.0 {
            assert!(result.pi_equivalent.is_none());
        } else {
            assert_eq!(result.status, Status::Completed);
            assert_eq!(
                result.pi_equivalent,
                Some((208.0 / elapsed) / 47231.227029007096)
            );
        }
    }
}

#[test]
fn timing_uses_half_open_phases_and_periodic_measurement_samples() {
    let start = Instant::now();
    let times = Timing {
        start,
        warm_end: start + Duration::from_secs(1),
        end: start + Duration::from_secs(5),
    };
    assert!(times.is_warmup(times.warm_end - Duration::from_nanos(1)));
    assert!(!times.is_warmup(times.warm_end));
    assert!(!times.is_warmup(times.end));
    assert!(!Timing {
        warm_end: start,
        ..times
    }
    .is_warmup(start));
    for (offset_ns, last_elapsed, expected) in [
        (0, 0.0, None),
        (1_000_000_000, 0.0, None),
        (1_999_999_999, 0.0, None),
        (2_000_000_000, 0.0, Some(1.0)),
        (2_100_000_000, 1.0, None),
        (2_500_000_000, 0.5, Some(1.5)),
        (3_000_000_000, 1.0, Some(2.0)),
        (5_000_000_000, 0.0, None),
        (5_000_000_001, 0.0, None),
    ] {
        assert_eq!(
            times.sample_elapsed(start + Duration::from_nanos(offset_ns), last_elapsed),
            expected
        );
    }
}

#[test]
fn cancelled_warmup_excludes_work_and_reports_elapsed_fraction() {
    let _serial = TEST_LOCK.lock().unwrap();
    let token = Cancellation::default();
    let before = Instant::now();
    let mut events = Vec::new();
    let mut observed_work = false;
    let result = run(
        Benchmark::SingleCore,
        Options {
            warmup: Duration::from_secs(10),
            measurement: Duration::from_secs(20),
        },
        &token,
        |p| {
            events.push(p);
            if p.phase == Phase::Warmup && p.iterations > 0 {
                observed_work = true;
                token.cancel();
            } else if p.phase == Phase::Measurement {
                token.cancel();
            }
        },
    );
    assert!(observed_work, "warmup must publish its completed work");
    assert_eq!(result.status, Status::Cancelled);
    assert_eq!(
        result.iterations, 0,
        "warmup work must not enter measurement"
    );
    assert_eq!(result.elapsed_seconds, 0.0);
    let final_event = events.last().unwrap();
    assert_eq!(final_event.phase, Phase::Cancelled);
    assert!(final_event.fraction > 0.0);
    assert!(final_event.fraction <= before.elapsed().as_secs_f64() / 30.0 + 1e-12);
    assert!(result.score.is_none() && result.pi_equivalent.is_none());
}

/// Compare the first periodic sample to the exact counter snapshot sent to progress.
/// Delaying the first callback forces sampling and progress into the same next
/// coordinator iteration; this avoids a timing approximation or a batch-size oracle.
/// Tail repair must not conceal a missing initial iteration or corrupt its rate.
#[test]
fn observed_measurement_time_produces_periodic_samples_before_the_tail() {
    let _serial = TEST_LOCK.lock().unwrap();
    let token = Cancellation::default();
    let mut observed_snapshot = None;
    let mut previous_progress = None;
    let result = run(
        Benchmark::SingleCore,
        Options {
            warmup: Duration::ZERO,
            measurement: Duration::from_secs(10),
        },
        &token,
        |p| {
            if p.phase == Phase::Measurement {
                if let Some(previous) = previous_progress {
                    assert!(
                        p.phase_elapsed_seconds - previous >= 0.1 - 1e-9,
                        "same-phase progress must be throttled to 10 Hz"
                    );
                }
                previous_progress = Some(p.phase_elapsed_seconds);
            }
            if p.phase == Phase::Measurement && p.phase_elapsed_seconds >= 1.0 {
                observed_snapshot = Some(p);
                token.cancel();
            } else if p.phase == Phase::Measurement {
                thread::sleep(Duration::from_secs_f64(1.05 - p.phase_elapsed_seconds));
            }
        },
    );
    assert_eq!(result.status, Status::Cancelled);
    assert!(
        result.samples.len() >= 2,
        "an observed snapshot must precede the cancellation tail"
    );
    let observed = observed_snapshot.unwrap();
    assert_eq!(
        result.samples[0].elapsed_seconds,
        observed.phase_elapsed_seconds
    );
    assert_eq!(
        result.samples[0].duration_seconds,
        observed.phase_elapsed_seconds
    );
    assert_eq!(result.samples[0].iterations, observed.iterations);
    assert!(result.samples[..result.samples.len() - 1]
        .iter()
        .all(|s| s.duration_seconds >= 1.0));
}

#[test]
fn callback_panic_stops_already_running_workers_before_the_deadline() {
    let _serial = TEST_LOCK.lock().unwrap();
    let token = Cancellation::default();
    let cleanup = token.clone();
    let (done_tx, done_rx) = mpsc::channel();
    let worker = thread::spawn(move || {
        let outcome = std::panic::catch_unwind(|| {
            run(
                Benchmark::SingleCore,
                Options {
                    warmup: Duration::from_secs(60),
                    measurement: Duration::from_secs(60),
                },
                &token,
                |p| {
                    if p.phase == Phase::Warmup && p.iterations > 0 {
                        panic!("callback fault after observed work");
                    }
                },
            )
        });
        done_tx.send(outcome.is_err()).unwrap();
    });
    let stopped = done_rx.recv_timeout(Duration::from_secs(5));
    // Always unblock a broken worker before asserting, including on a mutant.
    cleanup.cancel();
    worker.join().unwrap();
    assert!(stopped.unwrap());
    assert_recovered();
}

#[test]
fn counter_aggregation_preserves_measurement_work_max_finish_and_xor() {
    let counters: Vec<_> = [
        (50, 8, 2_000_000_000, 0b1100),
        (500, 16, 1_500_000_000, 0b1010),
    ]
    .into_iter()
    .map(|(warmup, measurement, finish, checksum)| {
        Arc::new(Counters {
            warmup: AtomicU64::new(warmup),
            measurement: AtomicU64::new(measurement),
            finish_ns: AtomicU64::new(finish),
            checksum: AtomicU64::new(checksum),
        })
    })
    .collect();
    let mut result = fresh_result();
    collect_counters(&mut result, &counters);
    assert_eq!(result.iterations, 24);
    assert_eq!(result.elapsed_seconds, 2.0);
    assert_eq!(result.checksum, 0b0110);
    collect_counters(&mut result, &[]);
    assert_eq!(
        (result.iterations, result.elapsed_seconds, result.checksum),
        (0, 0.0, 0)
    );
}
