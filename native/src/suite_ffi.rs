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

// Views contain only immutable global calibration pointers. The vectors and
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

struct CallbackContext {
    benchmark: Benchmark,
    callback: SuiteCallback,
    user: *mut c_void,
}

unsafe extern "C" fn progress_callback(progress: *const CProgress, user: *mut c_void) {
    let context = &*(user as *const CallbackContext);
    if let Some(callback) = context.callback {
        let progress = &*progress;
        callback(
            &CSuiteProgress {
                benchmark: context.benchmark as u32,
                progress: CProgress {
                    phase: progress.phase,
                    workers: progress.workers,
                    fraction: progress.fraction,
                    phase_elapsed_seconds: progress.phase_elapsed_seconds,
                    phase_duration_seconds: progress.phase_duration_seconds,
                    iterations: progress.iterations,
                },
            },
            context.user,
        );
    }
}

/// Config.benchmark is ignored; all other config validation is shared with run.
#[no_mangle]
pub unsafe extern "C" fn benchset_run_all(
    handle: *mut SuiteHandle,
    config: *const Config,
    callback: SuiteCallback,
    user: *mut c_void,
) -> u32 {
    benchset_run_all_observed(handle, config, callback, None, user)
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
    benchset_run_all_configured(handle, config, callback, on_result, None, user)
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
    execute_suite(
        handle,
        config,
        callback,
        on_result,
        on_config,
        user,
        &mut |b, o, c, p| run(b, o, c, p),
    )
}

unsafe fn execute_suite(
    handle: *mut SuiteHandle,
    config: *const Config,
    callback: SuiteCallback,
    on_result: ResultCallback,
    on_config: ConfigCallback,
    user: *mut c_void,
    runner: &mut RustRunner<'_>,
) -> u32 {
    let Some(handle) = handle.as_ref() else {
        return Status::Failed as u32;
    };
    if handle.used.swap(true, Ordering::AcqRel) {
        return Status::Failed as u32;
    }
    let mut reasons = Vec::new();
    let mut configuration_aborted = false;
    let result = run_suite_with(|benchmark| {
        // Inspect the fixed prefix before copying the whole config, just as run does.
        let mut copied;
        let config = if !config.is_null()
            && ptr::addr_of!((*config).abi_version).read() == ABI_VERSION
            && ptr::addr_of!((*config).struct_size).read() == std::mem::size_of::<Config>() as u32
        {
            copied = config.read();
            if on_config.is_some_and(|callback| callback(benchmark as u32, &mut copied, user) == 0)
            {
                configuration_aborted = true;
                return None;
            }
            copied.benchmark = benchmark as u32;
            &copied
        } else {
            config
        };
        let mut current = Handle {
            cancellation: handle.cancellation.clone(),
            used: AtomicBool::new(false),
            result: Mutex::new(None),
        };
        let mut context = CallbackContext {
            benchmark,
            callback,
            user,
        };
        execute(
            &mut current,
            config,
            Some(progress_callback),
            (&mut context as *mut CallbackContext).cast(),
            runner,
        );
        let proceed = on_result.map_or(true, |callback| {
            callback(benchmark as u32, &current, user) == 0
        });
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
    if let Some(handle) = handle.as_ref() {
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
    let stored = (*handle).result.lock().unwrap_or_else(|e| e.into_inner());
    let Some(stored) = stored.as_ref() else {
        return 0;
    };
    let equivalent = stored.result.aggregated_scores.pi_equivalent;
    out.write(CSuiteResult {
        results: stored.views.as_ptr(),
        result_count: stored.result.results.len() as u64,
        aggregated_scores: CAggregatedScores {
            pi_equivalent: equivalent.unwrap_or(0.0),
            has_pi_equivalent: u32::from(equivalent.is_some()),
        },
    });
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
    let stored = (*handle).result.lock().unwrap_or_else(|e| e.into_inner());
    let Some(result) = stored
        .as_ref()
        .and_then(|s| s.result.results.get(index as usize))
    else {
        return 0;
    };
    out.write(linked_result(result));
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
    let stored = (*handle).result.lock().unwrap_or_else(|e| e.into_inner());
    let Some(sample) = stored
        .as_ref()
        .and_then(|s| s.result.results.get(result_index as usize))
        .and_then(|r| r.samples.get(sample_index as usize))
    else {
        return 0;
    };
    out.write(CSample {
        elapsed_seconds: sample.elapsed_seconds,
        duration_seconds: sample.duration_seconds,
        iterations: sample.iterations,
        iterations_per_second: sample.iterations_per_second,
    });
    1
}

#[no_mangle]
pub unsafe extern "C" fn benchset_suite_reason(
    handle: *const SuiteHandle,
    index: u64,
) -> *const c_char {
    let Some(handle) = handle.as_ref() else {
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
        drop(Box::from_raw(handle));
    }
}

#[cfg(test)]
#[path = "suite_ffi_tests.rs"]
mod tests;

#[cfg(test)]
#[path = "observed_suite_tests.rs"]
mod observed_tests;

#[cfg(test)]
#[path = "configured_suite_tests.rs"]
mod configured_tests;
