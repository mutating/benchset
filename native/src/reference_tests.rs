use super::*;

const SINGLE_SCORE: f64 = 47231.227029007096;
const MULTI_SCORE: f64 = 181953.17419139884;

pub(super) fn assert_reference_links(result: &BenchmarkResult) {
    let machine: &'static ReferenceMachine = result.reference_machine;
    let calibration: &'static ReferenceRun = result.reference_run;
    assert!(std::ptr::eq(machine, &PI_REFERENCE_MACHINE));
    assert!(std::ptr::eq(calibration, &PI_REFERENCE_RUN));
}

fn assert_live_result(result: &BenchmarkResult, denominator: f64) {
    assert_reference_links(result);
    if result.status == Status::Completed {
        assert!(result.score.unwrap() > 0.0);
        assert_eq!(
            result.pi_equivalent,
            Some(result.score.unwrap() / denominator)
        );
    } else {
        // A worker may be descheduled for the entire short measurement window.
        assert_eq!(result.status, Status::Failed, "{}", result.reason);
        assert_eq!(
            result.reason,
            "measurement window was too short to complete any work"
        );
        assert!(result.iterations == 0 || result.elapsed_seconds == 0.0);
        assert!(result.score.is_none());
        assert!(result.pi_equivalent.is_none());
    }
}

#[test]
fn public_reference_machine_has_recorded_installed_hardware() {
    let machine: ReferenceMachine = PI_REFERENCE_MACHINE;
    assert_eq!(machine.name, "Raspberry Pi 5 Model B Rev 1.0");
    assert_eq!(machine.short_name, "pi");
    assert_eq!(machine.os_name, "Ubuntu Server");
    assert_eq!(machine.os_version, "26.04 LTS");
    assert_eq!(machine.cpu_architecture, "aarch64");
    let installed_bytes: u64 = machine.memory_bytes;
    assert_eq!(installed_bytes, 8_589_934_592);
    assert_ne!(installed_bytes, 8_317_272_064); // Linux MemTotal is not installed RAM.
    assert_eq!(machine.cpu_cores, 4);
    assert!(format!("{machine:?}").contains("Raspberry Pi 5"));

    let mut copy = machine;
    copy.memory_bytes = 1;
    copy.short_name = "another";
    assert_ne!(copy, PI_REFERENCE_MACHINE);
    assert_eq!(PI_REFERENCE_MACHINE, machine);
}

#[test]
fn public_reference_run_identifies_only_the_final_six_run_series() {
    let calibration: ReferenceRun = PI_REFERENCE_RUN;
    assert_eq!(calibration.started_at_unix_seconds, 1_792_767_590.517391);
    // 2026-10-23T00:00:00Z plus the recorded 14:59:50.517391.
    assert_eq!(
        calibration.started_at_unix_seconds,
        1_792_713_600.0 + 14.0 * 3600.0 + 59.0 * 60.0 + 50.517391
    );
    assert_eq!(calibration.start_temperature_c, 41.35);
    assert_eq!(calibration.end_temperature_c, 66.65);
    assert_eq!(calibration.warmup_seconds, 1248.0);
    assert_eq!(calibration.results.len(), 2);
    assert_eq!(calibration.results[0].benchmark, "single_core");
    assert_eq!(calibration.results[1].benchmark, "multi_core");
    assert_ne!(
        calibration.results[0].benchmark,
        calibration.results[1].benchmark
    );
    let entry: ReferenceBenchmarkResult = calibration.results[0];
    assert!(format!("{entry:?}").contains("single_core"));
    assert!(format!("{calibration:?}").contains("1248"));

    // The calibration settings do not silently replace runtime defaults.
    assert_eq!(DEFAULT_WARMUP_SECONDS, 120.0);
    assert_eq!(DEFAULT_MEASUREMENT_SECONDS, 60.0);
    assert_eq!(Options::default().warmup, Duration::from_secs(120));
    assert_eq!(Options::default().measurement, Duration::from_secs(60));
    let mut copy = calibration;
    copy.warmup_seconds = 0.0;
    assert_ne!(copy, PI_REFERENCE_RUN);
    assert_eq!(PI_REFERENCE_RUN, calibration);
}

#[test]
fn reference_scores_are_full_precision_pooled_iterations_over_actual_time() {
    // Independent totals from the three final raw measurements per benchmark.
    for (benchmark, iterations, elapsed, expected) in [
        (
            Benchmark::SingleCore,
            42_508_112_u64,
            900.000162475,
            SINGLE_SCORE,
        ),
        (
            Benchmark::MultiCore,
            163_757_952_u64,
            900.000523364,
            MULTI_SCORE,
        ),
    ] {
        let recorded = PI_REFERENCE_RUN
            .results
            .iter()
            .find(|entry| entry.benchmark == benchmark.name())
            .unwrap();
        assert_eq!(recorded.score.to_bits(), expected.to_bits());
        assert_eq!(recorded.score, iterations as f64 / elapsed);
        assert_eq!(benchmark.reference_score(), expected);
        assert!(recorded.score.is_finite() && recorded.score > 0.0);
        assert_ne!(recorded.score, recorded.score.round());
    }
}

#[test]
fn ratios_have_the_correct_direction_and_preserve_double_precision_and_range() {
    for (benchmark, denominator) in [
        (Benchmark::SingleCore, SINGLE_SCORE),
        (Benchmark::MultiCore, MULTI_SCORE),
    ] {
        for multiplier in [0.5, 1.0, 2.0] {
            assert_eq!(
                pi_equivalent(benchmark, denominator * multiplier),
                multiplier
            );
        }
        for score in [0.000000123456789, 12_345.6789012345, 9876543210.12345] {
            let actual = pi_equivalent(benchmark, score);
            let expected = score / denominator;
            assert_eq!(actual.to_bits(), expected.to_bits());
            assert!(actual.is_finite() && actual > 0.0);
        }
        assert!(pi_equivalent(benchmark, 0.000000123456789) < 1.0e-10);
        assert!(pi_equivalent(benchmark, 9876543210.12345) > 100.0);
    }
}

#[test]
fn completed_ratios_use_the_final_score_and_benchmark_not_the_worker_count() {
    for (benchmark, denominator) in [
        (Benchmark::SingleCore, SINGLE_SCORE),
        (Benchmark::MultiCore, MULTI_SCORE),
    ] {
        for workers in [1, 4, 99] {
            let mut result = BenchmarkResult::failed(benchmark, Options::default(), String::new());
            assert_reference_links(&result);
            assert!(result.pi_equivalent.is_none());
            result.workers = workers;
            result.iterations = 123_456_789;
            result.elapsed_seconds = std::f64::consts::PI;
            finish_result(&mut result, false, false);
            let score = 123_456_789.0 / std::f64::consts::PI;
            assert_eq!(result.status, Status::Completed);
            assert_eq!(result.score, Some(score));
            assert_eq!(result.pi_equivalent, Some(score / denominator));
            assert_reference_links(&result);
            let recorded = result
                .reference_run
                .results
                .iter()
                .find(|entry| entry.benchmark == result.benchmark.name())
                .unwrap();
            assert_eq!(result.pi_equivalent, Some(score / recorded.score));
            assert_eq!(result.warmup_seconds, 120.0);
            assert_eq!(result.measurement_seconds, 60.0);
        }
    }
}

#[derive(Clone)]
struct OneCpu;
impl Runtime for OneCpu {
    fn available_cpus(&self) -> std::io::Result<usize> {
        Ok(1)
    }
    fn spawn(
        &self,
        index: usize,
        task: Box<dyn FnOnce() + Send>,
    ) -> std::io::Result<JoinHandle<()>> {
        System.spawn(index, task)
    }
    fn kernel(&self, index: usize) -> Result<Kernel, String> {
        System.kernel(index)
    }
}

#[test]
fn one_worker_multicore_keeps_its_denominator_and_metadata_is_readable_while_active() {
    let _serial = tests::TEST_LOCK.lock().unwrap();
    let retained =
        BenchmarkResult::failed(Benchmark::SingleCore, Options::default(), String::new());
    let mut machine: &'static ReferenceMachine = retained.reference_machine;
    let mut calibration: &'static ReferenceRun = retained.reference_run;
    drop(retained);
    for (benchmark, denominator) in [
        (Benchmark::SingleCore, SINGLE_SCORE),
        (Benchmark::MultiCore, MULTI_SCORE),
    ] {
        let mut read_concurrently = false;
        let result = run_with(
            benchmark,
            Options {
                warmup: Duration::ZERO,
                measurement: Duration::from_millis(40),
            },
            &Cancellation::default(),
            &mut |_| {
                assert!(ACTIVE.load(Ordering::Acquire));
                assert!(std::ptr::eq(machine, &PI_REFERENCE_MACHINE));
                assert!(std::ptr::eq(calibration, &PI_REFERENCE_RUN));
                if !read_concurrently {
                    read_concurrently = true;
                    let readers: Vec<_> = (0..4)
                        .map(|_| {
                            thread::spawn(move || {
                                for _ in 0..50 {
                                    assert!(std::ptr::eq(machine, &PI_REFERENCE_MACHINE));
                                    assert!(std::ptr::eq(calibration, &PI_REFERENCE_RUN));
                                    assert_eq!(machine.short_name, "pi");
                                    assert_eq!(calibration.results[0].score, SINGLE_SCORE);
                                    assert_eq!(calibration.results[1].score, MULTI_SCORE);
                                    assert_eq!(
                                        Benchmark::SingleCore.reference_score(),
                                        SINGLE_SCORE
                                    );
                                    assert_eq!(Benchmark::MultiCore.reference_score(), MULTI_SCORE);
                                }
                            })
                        })
                        .collect();
                    for reader in readers {
                        reader.join().unwrap();
                    }
                }
            },
            Arc::new(OneCpu),
        );
        assert!(read_concurrently);
        assert_live_result(&result, denominator);
        assert_eq!(result.workers, 1);
        // The next run reads references retained after this result is dropped.
        machine = result.reference_machine;
        calibration = result.reference_run;
        drop(result);
    }
    assert!(std::ptr::eq(machine, &PI_REFERENCE_MACHINE));
    assert!(std::ptr::eq(calibration, &PI_REFERENCE_RUN));
    assert_eq!(machine.short_name, "pi");
    assert_eq!(calibration.results[0].score, SINGLE_SCORE);
    assert_eq!(calibration.results[1].score, MULTI_SCORE);
}

#[test]
fn metadata_survives_failed_cancelled_and_successful_runs_without_changing() {
    let _serial = tests::TEST_LOCK.lock().unwrap();
    let machine = PI_REFERENCE_MACHINE;
    let calibration = PI_REFERENCE_RUN;
    let options = Options {
        warmup: Duration::ZERO,
        measurement: Duration::from_millis(40),
    };
    let token = Cancellation::default();
    token.cancel();
    let cancelled = run(Benchmark::SingleCore, options, &token, |_| {
        panic!("pre-cancel")
    });
    assert_eq!(cancelled.status, Status::Cancelled);
    assert!(cancelled.pi_equivalent.is_none());
    assert_reference_links(&cancelled);
    let cloned = cancelled.clone();
    assert_reference_links(&cloned);
    let retained_machine: &'static ReferenceMachine = cloned.reference_machine;
    let retained_run: &'static ReferenceRun = cloned.reference_run;
    drop((cancelled, cloned, token));
    assert_eq!(*retained_machine, machine);
    assert_eq!(*retained_run, calibration);
    assert_eq!(PI_REFERENCE_MACHINE, machine);
    assert_eq!(PI_REFERENCE_RUN, calibration);
    let failed = run(
        Benchmark::SingleCore,
        Options {
            measurement: Duration::ZERO,
            ..options
        },
        &Cancellation::default(),
        |_| panic!("invalid duration"),
    );
    assert_eq!(failed.status, Status::Failed);
    assert!(failed.pi_equivalent.is_none());
    assert_reference_links(&failed);
    assert_eq!(PI_REFERENCE_MACHINE, machine);
    assert_eq!(PI_REFERENCE_RUN, calibration);
    let live = run(
        Benchmark::SingleCore,
        options,
        &Cancellation::default(),
        |_| {},
    );
    assert_live_result(&live, SINGLE_SCORE);
    drop((failed, live));
    assert!(std::ptr::eq(retained_machine, &PI_REFERENCE_MACHINE));
    assert!(std::ptr::eq(retained_run, &PI_REFERENCE_RUN));
    assert_eq!(*retained_machine, machine);
    assert_eq!(*retained_run, calibration);
    assert_eq!(PI_REFERENCE_MACHINE, machine);
    assert_eq!(PI_REFERENCE_RUN, calibration);
}
