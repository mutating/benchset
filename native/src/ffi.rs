use crate::{run, Benchmark, BenchmarkResult, Cancellation, Options, Progress, Status};
use std::ffi::{c_char, c_void, CString};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::ptr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;

const ABI_VERSION: u32 = 1;

#[repr(C)]
#[derive(Clone, Copy)]
pub struct Config {
    pub abi_version: u32,
    pub struct_size: u32,
    pub benchmark: u32,
    pub reserved: u32,
    pub warmup_seconds: f64,
    pub measurement_seconds: f64,
}

#[repr(C)]
pub struct CProgress {
    pub phase: u32,
    pub workers: u32,
    pub fraction: f64,
    pub phase_elapsed_seconds: f64,
    pub phase_duration_seconds: f64,
    pub iterations: u64,
}

#[repr(C)]
pub struct CResult {
    pub abi_version: u32,
    pub status: u32,
    pub benchmark: u32,
    pub workers: u32,
    pub score: f64,
    pub warmup_seconds: f64,
    pub measurement_seconds: f64,
    pub elapsed_seconds: f64,
    pub variation_percent: f64,
    pub iterations: u64,
    pub checksum: u64,
    pub sample_count: u64,
    pub unstable: u32,
    pub has_variation: u32,
}

#[repr(C)]
pub struct CSample {
    pub elapsed_seconds: f64,
    pub duration_seconds: f64,
    pub iterations: u64,
    pub iterations_per_second: f64,
}

struct Stored {
    result: BenchmarkResult,
    reason: CString,
}

pub struct Handle {
    cancellation: Cancellation,
    used: AtomicBool,
    result: Mutex<Option<Stored>>,
}

pub type Callback = Option<unsafe extern "C" fn(*const CProgress, *mut c_void)>;

#[no_mangle]
pub extern "C" fn benchset_abi_version() -> u32 {
    ABI_VERSION
}

#[no_mangle]
pub extern "C" fn benchset_version() -> *const c_char {
    concat!(env!("CARGO_PKG_VERSION"), "\0").as_ptr().cast()
}

#[no_mangle]
pub unsafe extern "C" fn benchset_default_config(out: *mut Config) -> i32 {
    if out.is_null() {
        return 0;
    }
    out.write(Config {
        abi_version: ABI_VERSION,
        struct_size: std::mem::size_of::<Config>() as u32,
        benchmark: 1,
        reserved: 0,
        warmup_seconds: 120.0,
        measurement_seconds: 60.0,
    });
    1
}

#[no_mangle]
pub extern "C" fn benchset_create() -> *mut Handle {
    create_handle(&mut || Handle {
        cancellation: Cancellation::default(),
        used: AtomicBool::new(false),
        result: Mutex::new(None),
    })
}

fn create_handle(factory: &mut dyn FnMut() -> Handle) -> *mut Handle {
    catch_unwind(AssertUnwindSafe(|| Box::into_raw(Box::new(factory())))).unwrap_or(ptr::null_mut())
}

/// The caller must keep the handle, callback and user data alive until run returns.
#[no_mangle]
pub unsafe extern "C" fn benchset_run(
    handle: *mut Handle,
    config: *const Config,
    callback: Callback,
    user: *mut c_void,
) -> u32 {
    execute(handle, config, callback, user, &mut |b, o, c, p| {
        run(b, o, c, p)
    })
}

type RustRunner<'a> =
    dyn FnMut(Benchmark, Options, &Cancellation, &mut dyn FnMut(Progress)) -> BenchmarkResult + 'a;

unsafe fn execute(
    handle: *mut Handle,
    config: *const Config,
    callback: Callback,
    user: *mut c_void,
    runner: &mut RustRunner<'_>,
) -> u32 {
    if handle.is_null() {
        return Status::Failed as u32;
    }
    let handle = &*handle;
    if handle.used.swap(true, Ordering::AcqRel) {
        return Status::Failed as u32;
    }
    let mut benchmark = Benchmark::SingleCore;
    let mut options = Options::default();
    let mut requested = None;
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        if config.is_null() {
            return BenchmarkResult::failed(benchmark, options, "config is null".into());
        }
        // Read the fixed prefix before reading the remainder of the structure.
        if ptr::addr_of!((*config).abi_version).read() != ABI_VERSION
            || ptr::addr_of!((*config).struct_size).read() != std::mem::size_of::<Config>() as u32
        {
            return BenchmarkResult::failed(
                benchmark,
                options,
                "incompatible C ABI/config size".into(),
            );
        }
        let config = config.read();
        requested = Some((config.warmup_seconds, config.measurement_seconds));
        benchmark = match config.benchmark {
            1 => Benchmark::SingleCore,
            2 => Benchmark::MultiCore,
            _ => return BenchmarkResult::failed(benchmark, options, "unknown benchmark".into()),
        };
        if config.reserved != 0 {
            return BenchmarkResult::failed(
                benchmark,
                options,
                "reserved config field must be zero".into(),
            );
        }
        let warmup = Duration::try_from_secs_f64(config.warmup_seconds);
        let measurement = Duration::try_from_secs_f64(config.measurement_seconds);
        options = match (warmup, measurement) {
            (Ok(warmup), Ok(measurement)) if config.measurement_seconds > 0.0 => Options {
                warmup,
                measurement,
            },
            _ => {
                return BenchmarkResult::failed(
                    benchmark,
                    options,
                    "durations must be finite: warmup >= 0, measurement > 0".into(),
                )
            }
        };
        runner(
            benchmark,
            options,
            &handle.cancellation,
            &mut |progress: Progress| {
                if let Some(callback) = callback {
                    let progress = CProgress {
                        phase: progress.phase as u32,
                        workers: progress.workers,
                        fraction: progress.fraction,
                        phase_elapsed_seconds: progress.phase_elapsed_seconds,
                        phase_duration_seconds: progress.phase_duration_seconds,
                        iterations: progress.iterations,
                    };
                    callback(&progress, user);
                }
            },
        )
    }));
    let mut result = outcome.unwrap_or_else(|_| {
        BenchmarkResult::failed(benchmark, options, "native benchmark panicked".into())
    });
    if let Some((warmup, measurement)) = requested {
        result.warmup_seconds = warmup;
        result.measurement_seconds = measurement;
    }
    let status = result.status as u32;
    let reason = CString::new(result.reason.replace('\0', " ")).expect("NUL bytes were replaced");
    *handle.result.lock().unwrap_or_else(|e| e.into_inner()) = Some(Stored { result, reason });
    status
}

#[no_mangle]
pub unsafe extern "C" fn benchset_cancel(handle: *mut Handle) {
    if let Some(handle) = handle.as_ref() {
        handle.cancellation.cancel();
    }
}

#[no_mangle]
pub unsafe extern "C" fn benchset_result(handle: *const Handle, out: *mut CResult) -> i32 {
    if handle.is_null() || out.is_null() {
        return 0;
    }
    let stored = (*handle).result.lock().unwrap_or_else(|e| e.into_inner());
    let result = match &*stored {
        Some(stored) => &stored.result,
        None => return 0,
    };
    out.write(CResult {
        abi_version: ABI_VERSION,
        status: result.status as u32,
        benchmark: result.benchmark as u32,
        workers: result.workers,
        score: result.score.unwrap_or(0.0),
        warmup_seconds: result.warmup_seconds,
        measurement_seconds: result.measurement_seconds,
        elapsed_seconds: result.elapsed_seconds,
        variation_percent: result.variation_percent.unwrap_or(0.0),
        iterations: result.iterations,
        checksum: result.checksum,
        sample_count: result.samples.len() as u64,
        unstable: u32::from(result.unstable),
        has_variation: u32::from(result.variation_percent.is_some()),
    });
    1
}

#[no_mangle]
pub unsafe extern "C" fn benchset_sample(
    handle: *const Handle,
    index: u64,
    out: *mut CSample,
) -> i32 {
    if handle.is_null() || out.is_null() {
        return 0;
    }
    let stored = (*handle).result.lock().unwrap_or_else(|e| e.into_inner());
    let sample = match &*stored {
        Some(stored) => stored.result.samples.get(index as usize),
        None => None,
    };
    let sample = match sample {
        Some(sample) => sample,
        None => return 0,
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
pub unsafe extern "C" fn benchset_reason(handle: *const Handle) -> *const c_char {
    if handle.is_null() {
        return ptr::null();
    }
    let stored = (*handle).result.lock().unwrap_or_else(|e| e.into_inner());
    match &*stored {
        Some(stored) => stored.reason.as_ptr(),
        None => ptr::null(),
    }
}

#[no_mangle]
pub unsafe extern "C" fn benchset_free(handle: *mut Handle) {
    if !handle.is_null() {
        drop(Box::from_raw(handle));
    }
}

#[cfg(test)]
#[path = "ffi_tests.rs"]
mod tests;
