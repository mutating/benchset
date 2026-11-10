use super::*;
use crate::{run_suite_with, SuiteResult};

#[repr(C)]
pub struct CAggregatedScores {
    pub pi_equivalent: f64,
    pub has_pi_equivalent: u32,
}

#[repr(C)]
pub struct CSuiteResult {
    pub results: *const CResultV3,
    pub result_count: u64,
    pub aggregated_scores: CAggregatedScores,
}

#[repr(C)]
pub struct CSuiteProgress {
    pub benchmark: u32,
    pub progress: CProgress,
}

pub type SuiteCallback = Option<unsafe extern "C" fn(*const CSuiteProgress, *mut c_void)>;
pub type ResultCallback = Option<unsafe extern "C" fn(u32, *const Handle, *mut c_void) -> u32>;
pub type ConfigCallback = Option<unsafe extern "C" fn(u32, *mut Config, *mut c_void) -> u32>;

struct StoredSuite {
    result: SuiteResult,
    views: Vec<CResultV3>,
    reasons: Vec<CString>,
}

// SAFETY: Views contain only immutable global calibration pointers. The vectors and
// their values are owned by this record and accessed under the handle's mutex.
unsafe impl Send for StoredSuite {}

pub struct SuiteHandle {
    cancellation: Cancellation,
    used: AtomicBool,
    result: Mutex<Option<StoredSuite>>,
}

#[no_mangle]
pub extern "C" fn benchset_suite_create() -> *mut SuiteHandle {
    catch_unwind(|| {
        Box::into_raw(Box::new(SuiteHandle {
            cancellation: Cancellation::default(),
            used: AtomicBool::new(false),
            result: Mutex::new(None),
        }))
    })
    .unwrap_or(ptr::null_mut())
}

/// Config.benchmark is ignored; all other config validation is shared with run.
#[no_mangle]
pub unsafe extern "C" fn benchset_run_all(
    handle: *mut SuiteHandle,
    config: *const Config,
    callback: SuiteCallback,
    user: *mut c_void,
) -> u32 {
    unsafe { benchset_run_all_observed(handle, config, callback, None, user) }
}

/// Publish borrowed child snapshots before the next benchmark starts.
#[no_mangle]
pub unsafe extern "C" fn benchset_run_all_observed(
    handle: *mut SuiteHandle,
    config: *const Config,
    callback: SuiteCallback,
    on_result: ResultCallback,
    user: *mut c_void,
) -> u32 {
    unsafe { benchset_run_all_configured(handle, config, callback, on_result, None, user) }
}

/// Configure each borrowed child copy before validation, or abort without a child result.
#[no_mangle]
pub unsafe extern "C" fn benchset_run_all_configured(
    handle: *mut SuiteHandle,
    config: *const Config,
    callback: SuiteCallback,
    on_result: ResultCallback,
    on_config: ConfigCallback,
    user: *mut c_void,
) -> u32 {
    let runner: &mut RustRunner<'_> = &mut |b, o, c, p| run(b, o, c, p);
    unsafe { execute_suite_ffi(handle, config, callback, on_result, on_config, user, runner) }
}

unsafe fn execute_suite_ffi(
    handle: *mut SuiteHandle,
    config: *const Config,
    callback: SuiteCallback,
    on_result: ResultCallback,
    on_config: ConfigCallback,
    user: *mut c_void,
    runner: &mut RustRunner<'_>,
) -> u32 {
    let Some(handle) = (unsafe { handle.as_ref() }) else {
        return Status::Failed as u32;
    };
    execute_suite(
        handle,
        || unsafe { read_config(config) },
        |benchmark, progress| {
            if let Some(callback) = callback {
                let progress = CSuiteProgress {
                    benchmark: benchmark as u32,
                    progress: c_progress(progress),
                };
                unsafe { callback(&progress, user) };
            }
        },
        |benchmark, child| {
            on_result.map_or(true, |callback| {
                (unsafe { callback(benchmark as u32, child, user) }) == 0
            })
        },
        |benchmark, config| {
            on_config.map_or(true, |callback| {
                (unsafe { callback(benchmark as u32, config, user) }) != 0
            })
        },
        runner,
    )
}

fn execute_suite(
    handle: &SuiteHandle,
    mut snapshot: impl FnMut() -> ConfigSnapshot,
    mut notify: impl FnMut(Benchmark, Progress),
    mut observe: impl FnMut(Benchmark, &Handle) -> bool,
    mut configure: impl FnMut(Benchmark, &mut Config) -> bool,
    runner: &mut RustRunner<'_>,
) -> u32 {
    if handle.used.swap(true, Ordering::AcqRel) {
        return Status::Failed as u32;
    }
    let mut reasons = Vec::new();
    let mut configuration_aborted = false;
    let result = run_suite_with(|benchmark| {
        let config = match snapshot() {
            Ok(mut config) => {
                if !configure(benchmark, &mut config) {
                    configuration_aborted = true;
                    return None;
                }
                config.benchmark = benchmark as u32;
                check_config_prefix(config.abi_version, || config.struct_size).map(|()| config)
            }
            invalid => invalid,
        };
        let current = Handle {
            cancellation: handle.cancellation.clone(),
            used: AtomicBool::new(false),
            result: Mutex::new(None),
        };
        execute(
            &current,
            &mut || config,
            &mut |progress| notify(benchmark, progress),
            runner,
        );
        let proceed = observe(benchmark, &current);
        let stored = current.result.into_inner().unwrap().unwrap();
        reasons.push(stored.reason);
        Some((stored.result, proceed))
    });
    let status = if configuration_aborted {
        Status::Failed as u32
    } else {
        result
            .results
            .last()
            .map_or(Status::Failed, |result| result.status) as u32
    };
    let views = result.results.iter().map(linked_result).collect();
    *handle.result.lock().unwrap_or_else(|e| e.into_inner()) = Some(StoredSuite {
        result,
        views,
        reasons,
    });
    status
}

#[no_mangle]
pub unsafe extern "C" fn benchset_suite_cancel(handle: *mut SuiteHandle) {
    if let Some(handle) = unsafe { handle.as_ref() } {
        handle.cancellation.cancel();
    }
}

#[no_mangle]
pub unsafe extern "C" fn benchset_suite_result(
    handle: *const SuiteHandle,
    out: *mut CSuiteResult,
    size: u32,
) -> i32 {
    if handle.is_null() || out.is_null() || size as usize != std::mem::size_of::<CSuiteResult>() {
        return 0;
    }
    let handle = unsafe { &*handle };
    let stored = handle.result.lock().unwrap_or_else(|e| e.into_inner());
    let Some(stored) = stored.as_ref() else {
        return 0;
    };
    let equivalent = stored.result.aggregated_scores.pi_equivalent;
    let value = CSuiteResult {
        results: stored.views.as_ptr(),
        result_count: stored.result.results.len() as u64,
        aggregated_scores: CAggregatedScores {
            pi_equivalent: equivalent.unwrap_or(0.0),
            has_pi_equivalent: u32::from(equivalent.is_some()),
        },
    };
    unsafe { out.write(value) };
    1
}

#[no_mangle]
pub unsafe extern "C" fn benchset_suite_result_at(
    handle: *const SuiteHandle,
    index: u64,
    out: *mut CResultV3,
    size: u32,
) -> i32 {
    if handle.is_null() || out.is_null() || size as usize != std::mem::size_of::<CResultV3>() {
        return 0;
    }
    let handle = unsafe { &*handle };
    let stored = handle.result.lock().unwrap_or_else(|e| e.into_inner());
    let Some(result) = stored
        .as_ref()
        .and_then(|s| s.result.results.get(index as usize))
    else {
        return 0;
    };
    let value = linked_result(result);
    unsafe { out.write(value) };
    1
}

#[no_mangle]
pub unsafe extern "C" fn benchset_suite_sample(
    handle: *const SuiteHandle,
    result_index: u64,
    sample_index: u64,
    out: *mut CSample,
) -> i32 {
    if handle.is_null() || out.is_null() {
        return 0;
    }
    let handle = unsafe { &*handle };
    let stored = handle.result.lock().unwrap_or_else(|e| e.into_inner());
    let Some(sample) = stored
        .as_ref()
        .and_then(|s| s.result.results.get(result_index as usize))
        .and_then(|r| r.samples.get(sample_index as usize))
    else {
        return 0;
    };
    let value = CSample {
        elapsed_seconds: sample.elapsed_seconds,
        duration_seconds: sample.duration_seconds,
        iterations: sample.iterations,
        iterations_per_second: sample.iterations_per_second,
    };
    unsafe { out.write(value) };
    1
}

#[no_mangle]
pub unsafe extern "C" fn benchset_suite_reason(
    handle: *const SuiteHandle,
    index: u64,
) -> *const c_char {
    let Some(handle) = (unsafe { handle.as_ref() }) else {
        return ptr::null();
    };
    let stored = handle.result.lock().unwrap_or_else(|e| e.into_inner());
    stored
        .as_ref()
        .and_then(|s| s.reasons.get(index as usize))
        .map_or(ptr::null(), |reason| reason.as_ptr())
}

#[no_mangle]
pub unsafe extern "C" fn benchset_suite_free(handle: *mut SuiteHandle) {
    if !handle.is_null() {
        let handle = unsafe { Box::from_raw(handle) };
        drop(handle);
    }
}

#[cfg(test)]
#[allow(unsafe_op_in_unsafe_fn)]
#[path = "suite_ffi_tests.rs"]
mod tests;

#[cfg(test)]
#[allow(unsafe_op_in_unsafe_fn)]
#[path = "observed_suite_tests.rs"]
mod observed_tests;

#[cfg(test)]
#[allow(unsafe_op_in_unsafe_fn)]
#[path = "configured_suite_tests.rs"]
mod configured_tests;
