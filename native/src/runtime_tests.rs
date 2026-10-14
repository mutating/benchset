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
fn assert_recovered() {
    assert!(!ACTIVE.load(Ordering::Acquire));
    assert_eq!(
        run(
            Benchmark::SingleCore,
            options(),
            &Cancellation::default(),
            |_| {}
        )
        .status,
        Status::Completed
    );
}

#[test]
fn public_defaults_names_and_shared_cancellation() {
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
        assert_eq!(events[0].phase, Phase::Warmup);
        assert!(events.iter().any(|p| p.phase == Phase::Measurement));
        let final_event = events.last().unwrap();
        assert_eq!(final_event.phase, Phase::Completed);
        assert_eq!(final_event.fraction, 1.0);
        assert_eq!(final_event.iterations, result.iterations);
        for p in &events {
            assert!((0.0..=1.0).contains(&p.fraction));
            if matches!(p.phase, Phase::Warmup | Phase::Measurement) {
                assert!(p.phase_elapsed_seconds <= p.phase_duration_seconds);
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

#[test]
fn zero_warmup_multiple_samples_and_late_cancellation() {
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
    assert!(result.samples.len() >= 2);
    assert_eq!(
        result.samples.iter().map(|s| s.iterations).sum::<u64>(),
        result.iterations
    );
    assert_eq!(events.last().unwrap().phase, Phase::Cancelled);
    // Also exercise statistics on an actual multi-sample completed window.
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
    assert!(result.variation_percent.is_some());
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
        assert_eq!(result.reason, "cancelled before starting");
    }
    for phase in [Phase::Warmup, Phase::Measurement] {
        let token = Cancellation::default();
        let mut last = Phase::Failed;
        let result = run(
            Benchmark::MultiCore,
            Options {
                warmup: Duration::from_millis(30),
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
        assert_eq!(last, Phase::Cancelled);
        assert!(result.score.is_none());
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
    assert_eq!(result.reason, "another benchmark is already running");
    loser.cancel();
    let busy = run(Benchmark::SingleCore, options(), &loser, |_| {});
    assert_eq!(busy.reason, "another benchmark is already running");
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
    assert!(!token.is_cancelled());
    token.cancel();
    release_tx.send(()).unwrap();
    assert_eq!(handle.join().unwrap().status, Status::Cancelled);
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
                    warmup: Duration::from_millis(20),
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

#[test]
fn callback_delay_is_excluded_from_throughput_elapsed() {
    let _serial = TEST_LOCK.lock().unwrap();
    let (finished_tx, finished_rx) = mpsc::channel();
    let before = Instant::now();
    let mut delayed = false;
    let result = run_with(
        Benchmark::SingleCore,
        options(),
        &Cancellation::default(),
        &mut |p| {
            if p.phase == Phase::Measurement && !delayed {
                delayed = true;
                // Wait for an observed worker exit before delaying the callback;
                // this makes the assertion independent of hardware throughput.
                finished_rx.recv_timeout(Duration::from_secs(5)).unwrap();
                thread::sleep(Duration::from_millis(20));
            }
        },
        Arc::new(ObservedRuntime(finished_tx)),
    );
    assert_eq!(result.status, Status::Completed);
    assert!(before.elapsed().as_secs_f64() - result.elapsed_seconds >= 0.02);
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
}
struct ThreadGuard(Arc<AtomicUsize>);
impl Drop for ThreadGuard {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::SeqCst);
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
        System.spawn(
            index,
            Box::new(move || {
                active.fetch_add(1, Ordering::SeqCst);
                let _guard = ThreadGuard(active);
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
        let result = run_with(
            Benchmark::MultiCore,
            options(),
            &Cancellation::default(),
            &mut |_| {},
            Arc::new(FaultRuntime {
                fault,
                active: active.clone(),
            }),
        );
        assert_eq!(result.status, Status::Failed, "{fault:?}");
        assert!(result.reason.contains(reason), "{}", result.reason);
        assert!(result.score.is_none());
        assert_eq!(
            active.load(Ordering::SeqCst),
            0,
            "workers still alive after {fault:?}"
        );
        assert_recovered();
    }
}

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
        assert!(result.reason.contains(reason));
        assert_recovered();
    }
}

#[test]
fn sample_deltas_and_counter_selection() {
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
    assert_eq!(result.score, Some(110.0));
    assert!((result.variation_percent.unwrap() - 300.0_f64.sqrt() / 110.0 * 100.0).abs() < 1e-12);
    assert!(result.unstable);
    let boundary = measured(&[(1.0, 90), (1.0, 110)]);
    assert_eq!(boundary.variation_percent, Some(10.0));
    assert!(!boundary.unstable);
    assert!(measured(&[(1.0, 89), (1.0, 111)]).unstable);
}

#[test]
fn failed_cancelled_and_no_work_statistics_keep_diagnostics() {
    for (panicked, cancelled, reason, status) in [
        (true, true, "worker panicked", Status::Failed),
        (false, true, "cancelled", Status::Cancelled),
    ] {
        let mut result = fresh_result();
        result.samples.push(Sample {
            elapsed_seconds: 1.0,
            duration_seconds: 1.0,
            iterations: 8,
            iterations_per_second: 8.0,
        });
        result.iterations = 8;
        result.elapsed_seconds = 1.0;
        finish_result(&mut result, panicked, cancelled);
        assert_eq!(result.status, status);
        assert!(result.reason.contains(reason));
        assert!(result.score.is_none());
        assert_eq!(result.samples.len(), 1);
    }
    for (iterations, elapsed) in [(0, 1.0), (8, 0.0)] {
        let mut result = fresh_result();
        result.iterations = iterations;
        result.elapsed_seconds = elapsed;
        finish_result(&mut result, false, false);
        assert_eq!(result.status, Status::Failed);
        assert!(result.reason.contains("too short"));
    }
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
    }
}
