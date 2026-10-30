use super::*;
use crate::{tests::TEST_LOCK, Phase};
use std::ffi::CStr;
use std::mem::{size_of, MaybeUninit};

struct Suite(*mut SuiteHandle);
impl Drop for Suite {
    fn drop(&mut self) {
        unsafe { benchset_suite_free(self.0) }
    }
}

fn config() -> Config {
    let mut config = MaybeUninit::uninit();
    unsafe {
        assert_eq!(benchset_default_config(config.as_mut_ptr()), 1);
        config.assume_init()
    }
}

unsafe fn summary(handle: *const SuiteHandle) -> CSuiteResult {
    let mut out = MaybeUninit::uninit();
    assert_eq!(
        benchset_suite_result(handle, out.as_mut_ptr(), size_of::<CSuiteResult>() as u32),
        1
    );
    out.assume_init()
}

unsafe extern "C" fn cancel_on_progress(progress: *const CSuiteProgress, user: *mut c_void) {
    let progress = &*progress;
    assert_eq!(progress.benchmark, Benchmark::SingleCore as u32);
    if matches!(progress.progress.phase, 1 | 2) {
        benchset_suite_cancel(user.cast());
    }
}

type CapturedProgress = (u32, (u32, u32, f64, f64, f64, u64), std::thread::ThreadId);

unsafe extern "C" fn capture_progress(progress: *const CSuiteProgress, user: *mut c_void) {
    let event = &*progress;
    let value = &event.progress;
    let events = &mut *user.cast::<Vec<CapturedProgress>>();
    events.push((
        event.benchmark,
        (
            value.phase,
            value.workers,
            value.fraction,
            value.phase_elapsed_seconds,
            value.phase_duration_seconds,
            value.iterations,
        ),
        std::thread::current().id(),
    ));
}

unsafe extern "C" fn cancel_between_benchmarks(progress: *const CSuiteProgress, user: *mut c_void) {
    let progress = &*progress;
    if progress.benchmark == Benchmark::SingleCore as u32
        && progress.progress.phase == Phase::Completed as u32
    {
        benchset_suite_cancel(user.cast());
    }
}

/// Invalid getters preserve output before and after publication, including exact-size checks.
/// Byte canaries expose writes through failed getters; short configs and poison fail safely.
/// A failed run retains its original diagnostic even after a rejected rerun.
/// A weak token observer detects a leaked handle when the failed run is freed.
#[test]
fn nulls_unavailable_outputs_and_invalid_configs() {
    unsafe {
        benchset_suite_cancel(ptr::null_mut());
        benchset_suite_free(ptr::null_mut());
        assert_eq!(
            benchset_run_all(ptr::null_mut(), ptr::null(), None, ptr::null_mut()),
            2
        );
        let h = Suite(benchset_suite_create());
        let ownership = std::sync::Arc::downgrade(&(*h.0).cancellation.0);
        let _ = catch_unwind(AssertUnwindSafe(|| {
            let _guard = (*h.0).result.lock().unwrap();
            panic!("controlled suite mutex poison");
        }));
        let mut out = CSuiteResult {
            results: ptr::null(),
            result_count: 123,
            aggregated_scores: CAggregatedScores {
                pi_equivalent: 456.0,
                has_pi_equivalent: 7,
            },
        };
        let size = size_of::<CSuiteResult>() as u32;
        for (handle, output, bytes) in [
            (ptr::null(), &mut out as *mut _, size),
            (h.0.cast_const(), ptr::null_mut(), size),
            (h.0.cast_const(), &mut out, size - 1),
            (h.0.cast_const(), &mut out, size),
        ] {
            assert_eq!(benchset_suite_result(handle, output, bytes), 0);
        }
        assert_eq!(out.result_count, 123);
        assert_eq!(out.aggregated_scores.pi_equivalent, 456.0);
        let mut entry = MaybeUninit::<CResultV3>::uninit();
        ptr::write_bytes(
            entry.as_mut_ptr().cast::<u8>(),
            0xa5,
            size_of::<CResultV3>(),
        );
        let size = size_of::<CResultV3>() as u32;
        for (handle, output, bytes) in [
            (ptr::null(), entry.as_mut_ptr(), size),
            (h.0.cast_const(), ptr::null_mut(), size),
            (h.0.cast_const(), entry.as_mut_ptr(), size + 1),
            (h.0.cast_const(), entry.as_mut_ptr(), size),
        ] {
            assert_eq!(benchset_suite_result_at(handle, 0, output, bytes), 0);
        }
        let mut sample = MaybeUninit::<CSample>::uninit();
        ptr::write_bytes(sample.as_mut_ptr().cast::<u8>(), 0xa5, size_of::<CSample>());
        assert_eq!(
            benchset_suite_sample(ptr::null(), 0, 0, sample.as_mut_ptr()),
            0
        );
        assert_eq!(benchset_suite_sample(h.0, 0, 0, ptr::null_mut()), 0);
        assert_eq!(benchset_suite_sample(h.0, 0, 0, sample.as_mut_ptr()), 0);
        assert_eq!(
            std::slice::from_raw_parts(sample.as_ptr().cast::<u8>(), size_of::<CSample>()),
            &[0xa5; size_of::<CSample>()]
        );
        assert!(benchset_suite_reason(ptr::null(), 0).is_null());
        assert!(benchset_suite_reason(h.0, 0).is_null());
        assert_eq!(benchset_run_all(h.0, ptr::null(), None, ptr::null_mut()), 2);
        assert_eq!(summary(h.0).result_count, 1);
        assert_eq!(summary(h.0).aggregated_scores.has_pi_equivalent, 0);
        let summary_size = size_of::<CSuiteResult>() as u32;
        for bad_size in [0, summary_size - 1, summary_size + 1, u32::MAX] {
            assert_eq!(benchset_suite_result(h.0, &mut out, bad_size), 0);
            assert!(out.results.is_null());
            assert_eq!(out.result_count, 123);
            assert_eq!(out.aggregated_scores.pi_equivalent, 456.0);
            assert_eq!(out.aggregated_scores.has_pi_equivalent, 7);
        }
        for bad_size in [0, size - 1, size + 1, u32::MAX] {
            assert_eq!(
                benchset_suite_result_at(h.0, 0, entry.as_mut_ptr(), bad_size),
                0
            );
            assert_eq!(
                std::slice::from_raw_parts(entry.as_ptr().cast::<u8>(), size as usize),
                &[0xa5; size_of::<CResultV3>()]
            );
        }
        // MAX stays invalid after narrowing; 2^32 instead aliases slot zero.
        for index in [1, 1_u64 << 32, u64::MAX] {
            assert_eq!(
                benchset_suite_result_at(h.0, index, entry.as_mut_ptr(), size),
                0
            );
            assert_eq!(
                std::slice::from_raw_parts(entry.as_ptr().cast::<u8>(), size as usize),
                &[0xa5; size_of::<CResultV3>()]
            );
            assert!(benchset_suite_reason(h.0, index).is_null());
        }
        assert_eq!(benchset_run_all(h.0, ptr::null(), None, ptr::null_mut()), 2);
        let reason = benchset_suite_reason(h.0, 0);
        assert!(!reason.is_null());
        assert_eq!(CStr::from_ptr(reason).to_bytes(), b"config is null");
        assert!(benchset_suite_reason(h.0, 1).is_null());
        assert_eq!(
            benchset_suite_result_at(h.0, 1, entry.as_mut_ptr(), size),
            0
        );
        assert_eq!(benchset_suite_sample(h.0, 1, 0, sample.as_mut_ptr()), 0);
        assert_eq!(benchset_suite_sample(h.0, 0, 0, sample.as_mut_ptr()), 0);
        assert_eq!(
            std::slice::from_raw_parts(sample.as_ptr().cast::<u8>(), size_of::<CSample>()),
            &[0xa5; size_of::<CSample>()]
        );
        drop(h);
        assert!(ownership.upgrade().is_none());
        for field in 0..3 {
            let h = Suite(benchset_suite_create());
            let mut c = config();
            match field {
                0 => c.abi_version += 1,
                1 => c.struct_size -= 1,
                _ => c.reserved = 1,
            }
            assert_eq!(benchset_run_all(h.0, &c, None, ptr::null_mut()), 2);
        }
    }
}

/// Benchmark IDs, timing windows and caller-thread progress survive the suite adapter.
/// Recording thread IDs before checking them avoids unwinding across the C callback boundary.
/// Also protects sticky cancellation and the lifetime of a rejected handle's publication.
#[test]
fn actual_suite_getters_progress_cancellation_and_owned_results() {
    let _guard = TEST_LOCK.lock().unwrap();
    let caller = std::thread::current().id();
    unsafe {
        let h = Suite(benchset_suite_create());
        let mut c = config();
        c.benchmark = 999;
        c.warmup_seconds = 0.025;
        c.measurement_seconds = 0.125;
        let mut events: Vec<CapturedProgress> = Vec::new();
        let mut context = CallbackContext {
            benchmark: Benchmark::MultiCore,
            callback: Some(capture_progress),
            user: (&mut events as *mut Vec<CapturedProgress>).cast(),
        };
        let progress = CProgress {
            phase: 5,
            workers: u32::MAX,
            fraction: 0.12345678901234567,
            phase_elapsed_seconds: 1.2345678901234567,
            phase_duration_seconds: 2.345678901234567,
            iterations: u64::MAX,
        };
        progress_callback(&progress, (&mut context as *mut CallbackContext).cast());
        assert_eq!(
            events,
            vec![(
                2,
                (
                    5,
                    u32::MAX,
                    0.12345678901234567,
                    1.2345678901234567,
                    2.345678901234567,
                    u64::MAX
                ),
                caller
            )]
        );
        context.callback = None;
        progress_callback(&progress, (&mut context as *mut CallbackContext).cast());
        assert_eq!(events.len(), 1);
        events.clear();
        assert_eq!(
            benchset_run_all(
                h.0,
                &c,
                Some(capture_progress),
                (&mut events as *mut Vec<CapturedProgress>).cast()
            ),
            0
        );
        let completed: Vec<_> = events
            .iter()
            .filter(|(_, progress, _)| progress.0 == Phase::Completed as u32)
            .collect();
        assert_eq!(
            completed.iter().map(|event| event.0).collect::<Vec<_>>(),
            vec![1, 2]
        );
        let out = summary(h.0);
        // A short warmup can be missed by the coordinator; validate any observed
        // phase without requiring that scheduling happen before its deadline.
        for (_, progress, thread) in &events {
            assert_eq!(*thread, caller);
            assert_eq!(
                progress.4,
                if progress.0 == Phase::Warmup as u32 {
                    c.warmup_seconds
                } else {
                    c.measurement_seconds
                }
            );
        }
        assert_eq!(out.result_count, 2);
        assert!(!out.results.is_null());
        assert_eq!(out.aggregated_scores.has_pi_equivalent, 1);
        let mut product = 1.0;
        for index in 0..out.result_count {
            let mut entry = MaybeUninit::<CResultV3>::uninit();
            assert_eq!(
                benchset_suite_result_at(
                    h.0,
                    index,
                    entry.as_mut_ptr(),
                    size_of::<CResultV3>() as u32
                ),
                1
            );
            let entry = entry.assume_init();
            assert_eq!(entry.base.base.warmup_seconds, c.warmup_seconds);
            assert_eq!(entry.base.base.measurement_seconds, c.measurement_seconds);
            let event = completed[index as usize];
            assert_eq!(
                event.1,
                (
                    Phase::Completed as u32,
                    entry.base.base.workers,
                    1.0,
                    entry.base.base.elapsed_seconds,
                    c.measurement_seconds,
                    entry.base.base.iterations
                )
            );
            assert_eq!(
                (*out.results.add(index as usize)).base.pi_equivalent,
                entry.base.pi_equivalent
            );
            assert_eq!(entry.base.base.benchmark, index as u32 + 1);
            product *= entry.base.pi_equivalent;
            let mut sample = MaybeUninit::<CSample>::uninit();
            assert_eq!(benchset_suite_sample(h.0, index, 0, sample.as_mut_ptr()), 1);
            assert!(sample.assume_init_ref().iterations > 0);
            ptr::write_bytes(sample.as_mut_ptr().cast::<u8>(), 0xa5, size_of::<CSample>());
            assert_eq!(
                benchset_suite_sample(h.0, index, u64::MAX, sample.as_mut_ptr()),
                0
            );
            assert_eq!(
                std::slice::from_raw_parts(sample.as_ptr().cast::<u8>(), size_of::<CSample>()),
                &[0xa5; size_of::<CSample>()]
            );
            assert!(!entry.reference_machine.is_null());
        }
        assert!((out.aggregated_scores.pi_equivalent / product.sqrt() - 1.0).abs() < 1e-12);
        let reason = benchset_suite_reason(h.0, 0);
        assert_eq!(
            benchset_run_all(h.0, ptr::null(), None, ptr::null_mut()),
            Status::Failed as u32
        );
        let retained = summary(h.0);
        assert_eq!(retained.result_count, out.result_count);
        assert_eq!(retained.results, out.results);
        assert_eq!(
            retained.aggregated_scores.has_pi_equivalent,
            out.aggregated_scores.has_pi_equivalent
        );
        assert_eq!(
            retained.aggregated_scores.pi_equivalent,
            out.aggregated_scores.pi_equivalent
        );
        assert_eq!(benchset_suite_reason(h.0, 0), reason);

        let between = Suite(benchset_suite_create());
        assert_eq!(
            benchset_run_all(
                between.0,
                &c,
                Some(cancel_between_benchmarks),
                between.0.cast()
            ),
            Status::Cancelled as u32
        );
        let partial = summary(between.0);
        assert_eq!(partial.result_count, 2);
        assert_eq!(partial.aggregated_scores.has_pi_equivalent, 0);
        assert_eq!(
            (*partial.results).base.base.status,
            Status::Completed as u32
        );
        let last = &*partial.results.add(1);
        assert_eq!(last.base.base.status, Status::Cancelled as u32);
        assert_eq!(last.base.base.iterations, 0);
        assert_eq!(
            CStr::from_ptr(benchset_suite_reason(between.0, 0))
                .to_str()
                .unwrap(),
            ""
        );
        assert_eq!(
            CStr::from_ptr(benchset_suite_reason(between.0, 1))
                .to_str()
                .unwrap(),
            "cancelled before starting"
        );
        let cancelled = Suite(benchset_suite_create());
        assert_eq!(
            benchset_run_all(
                cancelled.0,
                &config(),
                Some(cancel_on_progress),
                cancelled.0.cast()
            ),
            1
        );
        assert_eq!(summary(cancelled.0).result_count, 1);
        assert_eq!(summary(cancelled.0).aggregated_scores.has_pi_equivalent, 0);
        let cancelled = Suite(benchset_suite_create());
        benchset_suite_cancel(cancelled.0);
        assert_eq!(
            benchset_run_all(cancelled.0, &config(), None, ptr::null_mut()),
            1
        );
        assert_eq!(Phase::Cancelled as u32, 4);
    }
}

/// Keeps a present zero aggregate and every indexed sample field in C getters.
/// Distinct fixtures expose index swaps, zeroed times and u64 index/counter truncation.
/// Synthetic records exercise transport exactly without waiting for a long measurement.
#[test]
fn suite_getters_preserve_zero_aggregate_and_indexed_samples() {
    unsafe {
        let h = Suite(benchset_suite_create());
        let mut expected = Vec::new();
        let results = crate::BENCHMARKS
            .into_iter()
            .map(|benchmark| {
                let mut result =
                    BenchmarkResult::failed(benchmark, Options::default(), String::new());
                result.status = Status::Completed;
                result.pi_equivalent = Some(0.0);
                result.samples = (0..2)
                    .map(|index| {
                        let offset = benchmark as u64 * 4 + index;
                        crate::Sample {
                            elapsed_seconds: offset as f64 + 10.5,
                            duration_seconds: offset as f64 + 0.25,
                            iterations: u64::MAX - offset,
                            iterations_per_second: offset as f64 + 100.125,
                        }
                    })
                    .collect();
                expected.push(result.samples.clone());
                result
            })
            .collect();
        let result = SuiteResult::new(results);
        let views = result.results.iter().map(linked_result).collect();
        *(*h.0).result.lock().unwrap() = Some(StoredSuite {
            result,
            views,
            reasons: vec![CString::new("").unwrap(), CString::new("").unwrap()],
        });
        let out = summary(h.0);
        assert_eq!(out.aggregated_scores.pi_equivalent, 0.0);
        assert_eq!(out.aggregated_scores.has_pi_equivalent, 1);
        for (result_index, samples) in expected.iter().enumerate() {
            for (sample_index, expected) in samples.iter().enumerate() {
                let mut sample = MaybeUninit::<CSample>::uninit();
                assert_eq!(
                    benchset_suite_sample(
                        h.0,
                        result_index as u64,
                        sample_index as u64,
                        sample.as_mut_ptr()
                    ),
                    1
                );
                let sample = sample.assume_init();
                assert_eq!(sample.elapsed_seconds, expected.elapsed_seconds);
                assert_eq!(sample.duration_seconds, expected.duration_seconds);
                assert_eq!(sample.iterations, expected.iterations);
                assert_eq!(sample.iterations_per_second, expected.iterations_per_second);
            }
        }
        for (result_index, sample_index) in [
            (2, 0),
            (0, 2),
            (1_u64 << 32, 0),
            (0, 1_u64 << 32),
            (u64::MAX, 0),
            (0, u64::MAX),
        ] {
            let mut output = MaybeUninit::<CSample>::uninit();
            ptr::write_bytes(output.as_mut_ptr().cast::<u8>(), 0xa5, size_of::<CSample>());
            assert_eq!(
                benchset_suite_sample(h.0, result_index, sample_index, output.as_mut_ptr()),
                0
            );
            assert_eq!(
                std::slice::from_raw_parts(output.as_ptr().cast::<u8>(), size_of::<CSample>()),
                &[0xa5; size_of::<CSample>()]
            );
        }
    }
}
