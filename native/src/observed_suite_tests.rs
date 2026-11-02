use super::*;
use crate::tests::TEST_LOCK;
use std::mem::{size_of, MaybeUninit};

struct Events {
    suite: *mut SuiteHandle,
    results: Vec<(u32, u32, u64)>,
    caller: std::thread::ThreadId,
    stop: u32,
    cancel_at: usize,
}

unsafe extern "C" fn progress(value: *const CSuiteProgress, user: *mut c_void) {
    let events = &*user.cast::<Events>();
    assert_eq!(std::thread::current().id(), events.caller);
    // Seeing progress for benchmark N requires its entire preceding prefix to
    // have been published already, not replayed after the native suite finishes.
    assert_eq!(
        (*value).benchmark,
        crate::BENCHMARKS[events.results.len()] as u32
    );
}

unsafe extern "C" fn result(benchmark: u32, handle: *const Handle, user: *mut c_void) -> u32 {
    let events = &mut *user.cast::<Events>();
    assert_eq!(std::thread::current().id(), events.caller);
    let mut view = MaybeUninit::<CResultV2>::uninit();
    assert_eq!(
        benchset_result_v2(handle, view.as_mut_ptr(), size_of::<CResultV2>() as u32),
        1
    );
    let view = view.assume_init();
    assert_eq!(benchmark, view.base.benchmark);
    assert_eq!(benchmark, crate::BENCHMARKS[events.results.len()] as u32);
    assert!(!benchset_reason(handle).is_null());
    for index in 0..view.base.sample_count {
        let mut sample = MaybeUninit::<CSample>::uninit();
        assert_eq!(benchset_sample(handle, index, sample.as_mut_ptr()), 1);
    }
    events
        .results
        .push((benchmark, view.base.status, view.base.iterations));
    if events.cancel_at == events.results.len() {
        benchset_suite_cancel(events.suite);
    }
    events.stop
}

/// Observers see owned native diagnostics before later work on the calling thread.
/// Stopping keeps the completed prefix; cancellation is sticky across child handles.
#[test]
fn observation_streams_results_and_controls_later_work_without_rewriting_them() {
    let _guard = TEST_LOCK.lock().unwrap();
    unsafe {
        for (stop, cancel_at, count) in [
            (0, 0, 2),
            (1, 0, 1),
            (7, 0, 1),
            (u32::MAX, 0, 1),
            (0, 1, 2),
            (0, 2, 2),
            (7, 1, 1),
        ] {
            let suite = benchset_suite_create();
            let mut config = MaybeUninit::<Config>::uninit();
            assert_eq!(benchset_default_config(config.as_mut_ptr()), 1);
            let mut config = config.assume_init();
            config.benchmark = 999;
            config.warmup_seconds = 0.0;
            config.measurement_seconds = 0.05;
            let mut events = Events {
                suite,
                results: Vec::new(),
                caller: std::thread::current().id(),
                stop,
                cancel_at,
            };
            let status = benchset_run_all_observed(
                suite,
                &config,
                Some(progress),
                Some(result),
                (&mut events as *mut Events).cast(),
            );
            assert_eq!(config.benchmark, 999);
            assert_eq!(events.results.len(), count);
            assert_eq!(events.results[0].1, Status::Completed as u32);
            assert!(events.results[0].2 > 0);
            assert_eq!(
                status,
                if cancel_at == 1 && stop == 0 {
                    Status::Cancelled
                } else {
                    Status::Completed
                } as u32
            );
            if cancel_at == 1 && stop == 0 {
                assert_eq!(
                    events.results[1],
                    (Benchmark::MultiCore as u32, Status::Cancelled as u32, 0)
                );
            }
            let mut summary = MaybeUninit::<CSuiteResult>::uninit();
            assert_eq!(
                benchset_suite_result(
                    suite,
                    summary.as_mut_ptr(),
                    size_of::<CSuiteResult>() as u32
                ),
                1
            );
            let summary = summary.assume_init();
            assert_eq!(summary.result_count, count as u64);
            assert_eq!(
                summary.aggregated_scores.has_pi_equivalent != 0,
                stop == 0 && cancel_at != 1
            );
            assert_eq!(
                benchset_run_all_observed(
                    suite,
                    &config,
                    None,
                    Some(result),
                    (&mut events as *mut Events).cast()
                ),
                Status::Failed as u32
            );
            assert_eq!(events.results.len(), count);
            benchset_suite_free(suite);
        }
    }
}

/// Failed validation still publishes its first diagnostic, without progress or later work.
#[test]
fn observation_reports_invalid_configuration_and_rejects_null_suite() {
    unsafe {
        assert_eq!(
            benchset_run_all_observed(ptr::null_mut(), ptr::null(), None, None, ptr::null_mut()),
            Status::Failed as u32
        );
        let suite = benchset_suite_create();
        let mut events = Events {
            suite,
            results: Vec::new(),
            caller: std::thread::current().id(),
            stop: 0,
            cancel_at: 0,
        };
        assert_eq!(
            benchset_run_all_observed(
                suite,
                ptr::null(),
                Some(progress),
                Some(result),
                (&mut events as *mut Events).cast()
            ),
            Status::Failed as u32
        );
        assert_eq!(
            events.results,
            [(Benchmark::SingleCore as u32, Status::Failed as u32, 0)]
        );
        benchset_suite_free(suite);
    }
}
