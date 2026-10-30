use super::*;

fn completed(benchmark: Benchmark, ratio: f64) -> BenchmarkResult {
    let mut result = BenchmarkResult::failed(benchmark, Options::default(), String::new());
    result.status = Status::Completed;
    result.pi_equivalent = Some(ratio);
    result
}

/// Uses the original ratios, including unstable results, and keeps zero present.
#[test]
fn aggregate_is_geometric_mean_of_ratios() {
    let mut single = completed(Benchmark::SingleCore, 0.5);
    single.score = Some(999.0);
    single.unstable = true;
    let multi = completed(Benchmark::MultiCore, 2.0);
    let suite = SuiteResult::new(vec![single, multi]);
    assert_eq!(suite.results.len(), 2);
    assert_eq!(suite.aggregated_scores.pi_equivalent, Some(1.0));
    let zeros = SuiteResult::new(BENCHMARKS.map(|b| completed(b, 0.0)).into());
    assert_eq!(zeros.aggregated_scores.pi_equivalent, Some(0.0));
    assert_eq!(AggregatedScores::default().pi_equivalent, None);
}

/// Covers log-space range/zeros and rejects invalid values at either position or early rounding.
/// Unit coefficients must keep the calibration baseline at one and retain equal weights.
#[test]
fn geometric_mean_handles_zeros_and_extreme_ratios_without_multiplication() {
    for (single, multi, expected, tolerance) in [
        (1.0, 1.0, 1.0, 1e-15),
        (1.0, 4.0, 2.0, 1e-15),
        (4.0, 1.0, 2.0, 1e-15),
        (0.0, 2.0, 0.0, 1e-15),
        (2.0, 0.0, 0.0, 1e-15),
        (1e-300, 1e300, 1.0, 1e-12),
        (1e300, 1e300, 1e300, 1e-12),
        (1e-300, 1e-300, 1e-300, 1e-12),
        (1.0 / 3.0, 2.0 / 3.0, 2.0_f64.sqrt() / 3.0, 1e-15),
    ] {
        let suite = SuiteResult::new(vec![
            completed(Benchmark::SingleCore, single),
            completed(Benchmark::MultiCore, multi),
        ]);
        let actual = suite.aggregated_scores.pi_equivalent.unwrap();
        if expected == 0.0 || expected == 1.0 {
            assert_eq!(actual, expected);
        } else {
            assert!((actual / expected - 1.0).abs() < tolerance);
        }
    }
    for invalid in [-1.0, f64::INFINITY, f64::NEG_INFINITY, f64::NAN] {
        for other in [0.0, 1.0] {
            for (single, multi) in [(invalid, other), (other, invalid)] {
                let suite = SuiteResult::new(vec![
                    completed(Benchmark::SingleCore, single),
                    completed(Benchmark::MultiCore, multi),
                ]);
                assert!(suite.aggregated_scores.pi_equivalent.is_none());
            }
        }
    }
}

/// Requires exactly the complete ordered suite with every usable coefficient present.
#[test]
fn incomplete_invalid_or_failed_suites_have_no_full_score() {
    let single = completed(Benchmark::SingleCore, 0.5);
    let multi = completed(Benchmark::MultiCore, 2.0);
    for results in [
        vec![],
        vec![single.clone()],
        vec![multi.clone(), single.clone()],
        vec![single.clone(), single.clone()],
        vec![single.clone(), multi.clone(), multi.clone()],
    ] {
        assert!(SuiteResult::new(results)
            .aggregated_scores
            .pi_equivalent
            .is_none());
    }
    for status in [Status::Failed, Status::Cancelled, Status::Completed] {
        let mut missing = multi.clone();
        missing.status = status;
        missing.pi_equivalent = None;
        assert!(SuiteResult::new(vec![single.clone(), missing])
            .aggregated_scores
            .pi_equivalent
            .is_none());
    }
}

/// Each unsuccessful position stops later work without publishing an aggregate.
#[test]
fn suite_order_and_stop_after_every_unsuccessful_position() {
    for stop in 0..2 {
        for status in [Status::Failed, Status::Cancelled, Status::Completed] {
            let mut calls = Vec::new();
            let suite = run_suite(|benchmark| {
                let mut result = completed(benchmark, if calls.is_empty() { 0.5 } else { 2.0 });
                if calls.len() == stop {
                    result.status = status;
                }
                calls.push(benchmark);
                result
            });
            let count = if status == Status::Completed {
                2
            } else {
                stop + 1
            };
            assert_eq!(calls, BENCHMARKS[..count]);
            assert_eq!(suite.results.len(), count);
            assert_eq!(
                suite.aggregated_scores.pi_equivalent,
                if status == Status::Completed {
                    Some(1.0)
                } else {
                    None
                }
            );
        }
    }
}

/// Checks public workload dispatch, timing windows and every completed callback field.
/// Comparing callbacks to final snapshots catches lossy forwarding through run_all.
/// Cancellation after the first completed event must prevent second-benchmark work.
#[test]
fn public_suite_progress_cancellation_and_invalid_duration() {
    let _guard = tests::TEST_LOCK.lock().unwrap();
    let cancellation = Cancellation::default();
    let mut on_progress = |benchmark, progress: Progress| {
        assert_eq!(benchmark, Benchmark::SingleCore);
        if matches!(progress.phase, Phase::Warmup | Phase::Measurement) {
            cancellation.cancel();
        }
    };
    let result = run_all(Options::default(), &cancellation, &mut on_progress);
    assert_eq!(result.results.len(), 1);
    assert_eq!(result.results[0].status, Status::Cancelled);
    assert!(result.aggregated_scores.pi_equivalent.is_none());
    let result = run_all(
        Options {
            warmup: Duration::ZERO,
            measurement: Duration::ZERO,
        },
        &Cancellation::default(),
        &mut on_progress,
    );
    assert_eq!(result.results.len(), 1);
    assert_eq!(result.results[0].status, Status::Failed);

    let options = Options {
        warmup: Duration::from_millis(25),
        measurement: Duration::from_millis(125),
    };
    let mut completed = Vec::new();
    let result = run_all(options, &Cancellation::default(), |benchmark, progress| {
        if progress.phase == Phase::Completed {
            completed.push((benchmark, progress));
        }
    });
    assert!(result
        .results
        .iter()
        .all(|result| result.status == Status::Completed));
    assert_eq!(
        completed
            .iter()
            .map(|(benchmark, _)| *benchmark)
            .collect::<Vec<_>>(),
        BENCHMARKS
    );
    assert_eq!(
        result
            .results
            .iter()
            .map(|result| result.benchmark)
            .collect::<Vec<_>>(),
        BENCHMARKS
    );
    assert!(result.aggregated_scores.pi_equivalent.is_some());
    for ((_, progress), result) in completed.iter().zip(&result.results) {
        assert_eq!(result.warmup_seconds, options.warmup.as_secs_f64());
        assert_eq!(
            result.measurement_seconds,
            options.measurement.as_secs_f64()
        );
        assert_eq!(progress.workers, result.workers);
        assert_eq!(progress.fraction, 1.0);
        assert_eq!(progress.phase_elapsed_seconds, result.elapsed_seconds);
        assert_eq!(progress.phase_duration_seconds, result.measurement_seconds);
        assert_eq!(progress.iterations, result.iterations);
    }

    let token = Cancellation::default();
    let result = run_all(options, &token, |benchmark, progress| {
        if benchmark == Benchmark::SingleCore && progress.phase == Phase::Completed {
            token.cancel();
        }
    });
    assert_eq!(result.results.len(), 2);
    assert_eq!(result.results[0].status, Status::Completed);
    assert_eq!(result.results[1].status, Status::Cancelled);
    assert_eq!(result.results[1].iterations, 0);
    assert!(result.aggregated_scores.pi_equivalent.is_none());
}
