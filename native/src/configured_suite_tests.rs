use super::*;
use std::mem::{size_of, MaybeUninit};

struct Suite(*mut SuiteHandle);
impl Drop for Suite {
    fn drop(&mut self) {
        unsafe { benchset_suite_free(self.0) }
    }
}

struct Events {
    suite: *mut SuiteHandle,
    calls: Vec<(char, u32)>,
    results: Vec<(u32, u32, f64, f64)>,
    abort_at: u32,
    invalid_at: u32,
    stop: u32,
    cancel_after: u32,
    caller: std::thread::ThreadId,
}

impl Events {
    fn new(suite: *mut SuiteHandle) -> Self {
        Self {
            suite,
            calls: Vec::new(),
            results: Vec::new(),
            abort_at: 0,
            invalid_at: 0,
            stop: 0,
            cancel_after: 0,
            caller: std::thread::current().id(),
        }
    }

    fn user(&mut self) -> *mut c_void {
        (self as *mut Self).cast()
    }
}

fn config() -> Config {
    Config {
        abi_version: ABI_VERSION,
        struct_size: size_of::<Config>() as u32,
        benchmark: 999,
        reserved: 0,
        warmup_seconds: 10.0,
        measurement_seconds: 20.0,
    }
}

unsafe extern "C" fn configure(benchmark: u32, config: *mut Config, user: *mut c_void) -> u32 {
    let events = &mut *user.cast::<Events>();
    assert_eq!(std::thread::current().id(), events.caller);
    events.calls.push(('c', benchmark));
    if events.abort_at == benchmark {
        return 0;
    }
    let config = &mut *config;
    // Every callback receives a fresh copy; neither an earlier callback's
    // changes nor the suite's benchmark override may mutate the caller's config.
    assert_eq!(config.benchmark, 999);
    assert_eq!(
        (config.warmup_seconds, config.measurement_seconds),
        (10.0, 20.0)
    );
    config.benchmark = 1234;
    config.warmup_seconds = f64::from(benchmark) / 4.0;
    config.measurement_seconds = if events.invalid_at == benchmark {
        0.0
    } else {
        f64::from(benchmark) / 2.0
    };
    u32::MAX
}

unsafe extern "C" fn observed(benchmark: u32, handle: *const Handle, user: *mut c_void) -> u32 {
    let events = &mut *user.cast::<Events>();
    assert_eq!(std::thread::current().id(), events.caller);
    let mut result = MaybeUninit::<CResultV2>::uninit();
    assert_eq!(
        benchset_result_v2(handle, result.as_mut_ptr(), size_of::<CResultV2>() as u32),
        1
    );
    let result = result.assume_init().base;
    assert_eq!(result.benchmark, benchmark);
    events.calls.push(('r', benchmark));
    events.results.push((
        benchmark,
        result.status,
        result.warmup_seconds,
        result.measurement_seconds,
    ));
    if events.cancel_after == benchmark {
        benchset_suite_cancel(events.suite);
    }
    events.stop
}

fn completed(
    benchmark: Benchmark,
    options: Options,
    cancellation: &Cancellation,
) -> BenchmarkResult {
    let mut result = BenchmarkResult::failed(benchmark, options, String::new());
    result.status = if cancellation.is_cancelled() {
        Status::Cancelled
    } else {
        Status::Completed
    };
    result.pi_equivalent = (result.status == Status::Completed).then_some(1.0);
    result
}

unsafe fn summary(suite: *const SuiteHandle) -> CSuiteResult {
    let mut result = MaybeUninit::uninit();
    assert_eq!(
        benchset_suite_result(suite, result.as_mut_ptr(), size_of::<CSuiteResult>() as u32),
        1
    );
    result.assume_init()
}

/// Config callbacks run in catalog order between prior publication and later
/// execution, using independent copies and preserving the Rust benchmark ID.
#[test]
fn configured_windows_reach_each_child_without_mutating_the_original() {
    unsafe {
        let suite = Suite(benchset_suite_create());
        let mut events = Events::new(suite.0);
        let config = config();
        let mut runs = Vec::new();
        assert_eq!(
            execute_suite_ffi(
                suite.0,
                &config,
                None,
                Some(observed),
                Some(configure),
                events.user(),
                &mut |benchmark, options, cancellation, _| {
                    runs.push((
                        benchmark,
                        options.warmup.as_secs_f64(),
                        options.measurement.as_secs_f64(),
                    ));
                    completed(benchmark, options, cancellation)
                }
            ),
            Status::Completed as u32
        );
        assert_eq!(
            runs,
            [
                (Benchmark::SingleCore, 0.25, 0.5),
                (Benchmark::MultiCore, 0.5, 1.0)
            ]
        );
        assert_eq!(events.calls, [('c', 1), ('r', 1), ('c', 2), ('r', 2)]);
        assert_eq!(events.results, [(1, 0, 0.25, 0.5), (2, 0, 0.5, 1.0)]);
        assert_eq!(
            (
                config.benchmark,
                config.warmup_seconds,
                config.measurement_seconds
            ),
            (999, 10.0, 20.0)
        );
        let result = summary(suite.0);
        assert_eq!(result.result_count, 2);
        assert_eq!(result.aggregated_scores.has_pi_equivalent, 1);
    }
}

/// Aborting before any child publishes an available empty suite. Aborting
/// later retains the completed prefix and reports failure without inventing a child.
#[test]
fn configuration_abort_preserves_empty_or_completed_prefix() {
    unsafe {
        for abort_at in [1, 2] {
            let suite = Suite(benchset_suite_create());
            let mut events = Events::new(suite.0);
            events.abort_at = abort_at;
            assert_eq!(
                execute_suite_ffi(
                    suite.0,
                    &config(),
                    None,
                    Some(observed),
                    Some(configure),
                    events.user(),
                    &mut |b, o, c, _| completed(b, o, c)
                ),
                Status::Failed as u32
            );
            assert_eq!(events.results.len(), (abort_at - 1) as usize);
            let result = summary(suite.0);
            assert_eq!(result.result_count, u64::from(abort_at - 1));
            assert_eq!(result.aggregated_scores.has_pi_equivalent, 0);
            assert!(benchset_suite_reason(suite.0, result.result_count).is_null());
            // A broken single-use guard must fail quickly rather than start
            // the real benchmark during this regression check.
            let mut repeated = config();
            repeated.reserved = 1;
            assert_eq!(
                benchset_run_all_configured(suite.0, &repeated, None, None, None, ptr::null_mut()),
                Status::Failed as u32
            );
            let preserved = summary(suite.0);
            assert_eq!(preserved.result_count, result.result_count);
            if preserved.result_count != 0 {
                assert_eq!(
                    (*preserved.results).base.base.status,
                    Status::Completed as u32
                );
            }
        }
    }
}

/// A callback may provide invalid numeric windows, but shared execute validation
/// must publish the actual failed child and never call its benchmark runner.
#[test]
fn invalid_reconfigured_windows_publish_failure_and_stop() {
    unsafe {
        for invalid_at in [1, 2] {
            let suite = Suite(benchset_suite_create());
            let mut events = Events::new(suite.0);
            events.invalid_at = invalid_at;
            let mut runs = 0;
            assert_eq!(
                execute_suite_ffi(
                    suite.0,
                    &config(),
                    None,
                    Some(observed),
                    Some(configure),
                    events.user(),
                    &mut |b, o, c, _| {
                        runs += 1;
                        completed(b, o, c)
                    }
                ),
                Status::Failed as u32
            );
            assert_eq!(runs, invalid_at - 1);
            assert_eq!(events.results.len(), invalid_at as usize);
            assert_eq!(events.results.last().unwrap().1, Status::Failed as u32);
            assert_eq!(events.results.last().unwrap().3, 0.0);
            assert_eq!(summary(suite.0).aggregated_scores.has_pi_equivalent, 0);
        }
    }
}

/// Observer stops preserve their legacy final status, while cancellation remains
/// sticky and still requests the next child's configuration before execution.
#[test]
fn observer_stop_and_cancellation_retain_their_existing_semantics() {
    unsafe {
        for stop in [0, 7] {
            let suite = Suite(benchset_suite_create());
            let mut events = Events::new(suite.0);
            events.stop = stop;
            events.cancel_after = 1;
            let status = execute_suite_ffi(
                suite.0,
                &config(),
                None,
                Some(observed),
                Some(configure),
                events.user(),
                &mut |b, o, c, _| completed(b, o, c),
            );
            assert_eq!(
                status,
                if stop == 0 {
                    Status::Cancelled
                } else {
                    Status::Completed
                } as u32
            );
            assert_eq!(events.results.len(), if stop == 0 { 2 } else { 1 });
            assert_eq!(events.calls.len(), if stop == 0 { 4 } else { 2 });
            assert_eq!(summary(suite.0).aggregated_scores.has_pi_equivalent, 0);
        }
    }
}

/// The additive entrypoint must retain NULL/bad-prefix failures without calling
/// config callbacks or reading a whole incompatible structure.
#[test]
fn invalid_prefixes_skip_configuration_and_keep_normal_failed_results() {
    unsafe {
        assert_eq!(
            benchset_run_all_configured(
                ptr::null_mut(),
                ptr::null(),
                None,
                None,
                None,
                ptr::null_mut()
            ),
            Status::Failed as u32
        );
        let bad_prefix = [ABI_VERSION, 8];
        for config in [ptr::null(), bad_prefix.as_ptr().cast::<Config>()] {
            let suite = Suite(benchset_suite_create());
            let mut events = Events::new(suite.0);
            assert_eq!(
                benchset_run_all_configured(
                    suite.0,
                    config,
                    None,
                    Some(observed),
                    Some(configure),
                    events.user()
                ),
                Status::Failed as u32
            );
            assert_eq!(events.calls, [('r', 1)]);
            assert_eq!(events.results.len(), 1);
            assert_eq!(events.results[0].1, Status::Failed as u32);
        }
    }
}

/// The exported wrapper forwards configuration and observer callbacks before a
/// pre-cancelled run, without requiring timed work or worker initialization.
#[test]
fn public_entrypoint_forwards_configuration_and_observation() {
    let _serial = crate::tests::TEST_LOCK.lock().unwrap();
    unsafe {
        let suite = Suite(benchset_suite_create());
        let mut events = Events::new(suite.0);
        benchset_suite_cancel(suite.0);
        assert_eq!(
            benchset_run_all_configured(
                suite.0,
                &config(),
                None,
                Some(observed),
                Some(configure),
                events.user()
            ),
            Status::Cancelled as u32
        );
        assert_eq!(events.calls, [('c', 1), ('r', 1)]);
        assert_eq!(events.results, [(1, Status::Cancelled as u32, 0.25, 0.5)]);
    }
}

/// An observer may update the caller's config through its context between
/// children. The next child must read that update, not reuse an earlier snapshot.
#[test]
fn suite_reads_config_again_after_observation() {
    unsafe extern "C" fn update(benchmark: u32, _: *const Handle, user: *mut c_void) -> u32 {
        if benchmark == Benchmark::SingleCore as u32 {
            (*user.cast::<Config>()).measurement_seconds = 0.75;
        }
        0
    }

    let suite = Suite(benchset_suite_create());
    let mut config = config();
    let config = ptr::addr_of_mut!(config);
    let mut windows = Vec::new();
    let status = unsafe {
        execute_suite_ffi(
            suite.0,
            config,
            None,
            Some(update),
            None,
            config.cast(),
            &mut |b, o, c, _| {
                windows.push(o.measurement.as_secs_f64());
                completed(b, o, c)
            },
        )
    };
    assert_eq!(status, Status::Completed as u32);
    assert_eq!(windows, [20.0, 0.75]);
}

/// A successful configuration callback can still invalidate either header
/// field. Reject its copy before interpreting any duration or running a child.
#[test]
fn configured_suite_revalidates_the_copied_header() {
    unsafe extern "C" fn invalidate(_: u32, config: *mut Config, user: *mut c_void) -> u32 {
        if *user.cast::<bool>() {
            (*config).abi_version += 1;
        } else {
            (*config).struct_size -= 1;
        }
        1
    }

    for mut change_abi in [true, false] {
        let suite = Suite(benchset_suite_create());
        unsafe {
            benchset_suite_cancel(suite.0);
            assert_eq!(
                benchset_run_all_configured(
                    suite.0,
                    &config(),
                    None,
                    None,
                    Some(invalidate),
                    ptr::addr_of_mut!(change_abi).cast()
                ),
                Status::Failed as u32
            );
            let result = summary(suite.0);
            assert_eq!(result.result_count, 1);
            assert_eq!((*result.results).base.base.status, Status::Failed as u32);
            assert_eq!(
                (*result.results).base.base.measurement_seconds,
                DEFAULT_MEASUREMENT_SECONDS
            );
            assert_eq!(
                std::ffi::CStr::from_ptr(benchset_suite_reason(suite.0, 0)).to_bytes(),
                b"incompatible C ABI/config size"
            );
        }
    }
}
