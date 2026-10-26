use crate::{
    run, Benchmark, BenchmarkResult, Cancellation, Options, Progress, Status,
    DEFAULT_MEASUREMENT_SECONDS, DEFAULT_WARMUP_SECONDS, PI_REFERENCE_MACHINE, PI_REFERENCE_RUN,
};
use std::ffi::{c_char, c_void, CString};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::ptr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};
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
#[derive(Clone, Copy)]
#[cfg_attr(test, derive(Default))]
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

/// The original 88-byte result prefix stays binary-compatible.
#[repr(C)]
pub struct CResultV2 {
    pub base: CResult,
    pub pi_equivalent: f64,
    pub has_pi_equivalent: u32,
}

#[repr(C)]
pub struct CResultV3 {
    pub base: CResultV2,
    pub reference_machine: *const CReferenceMachine,
    pub reference_run: *const CReferenceRun,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct CReferenceMachine {
    pub name: *const c_char,
    pub short_name: *const c_char,
    pub os_name: *const c_char,
    pub os_version: *const c_char,
    pub cpu_architecture: *const c_char,
    pub memory_bytes: u64,
    pub cpu_cores: u32,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct CReferenceRun {
    pub started_at_unix_seconds: f64,
    pub start_temperature_c: f64,
    pub end_temperature_c: f64,
    pub warmup_seconds: f64,
    pub result_count: u64,
}

#[repr(C)]
pub struct CReferenceBenchmarkResult {
    pub benchmark: *const c_char,
    pub score: f64,
}

struct ReferenceStrings {
    machine: [CString; 5],
    benchmarks: Vec<CString>,
}

fn reference_strings() -> &'static ReferenceStrings {
    static STRINGS: OnceLock<ReferenceStrings> = OnceLock::new();
    STRINGS.get_or_init(|| ReferenceStrings {
        machine: [
            PI_REFERENCE_MACHINE.name,
            PI_REFERENCE_MACHINE.short_name,
            PI_REFERENCE_MACHINE.os_name,
            PI_REFERENCE_MACHINE.os_version,
            PI_REFERENCE_MACHINE.cpu_architecture,
        ]
        .map(|value| CString::new(value).expect("reference strings contain no NUL")),
        benchmarks: PI_REFERENCE_RUN
            .results
            .iter()
            .map(|value| CString::new(value.benchmark).expect("reference names contain no NUL"))
            .collect(),
    })
}

struct ReferenceViews {
    machine: CReferenceMachine,
    run: CReferenceRun,
}

// All pointers borrow immutable strings in reference_strings() for the library's
// lifetime. The views are initialized once and exposed only through const pointers.
unsafe impl Send for ReferenceViews {}
unsafe impl Sync for ReferenceViews {}

fn reference_views() -> &'static ReferenceViews {
    static VIEWS: OnceLock<ReferenceViews> = OnceLock::new();
    VIEWS.get_or_init(|| {
        let strings = &reference_strings().machine;
        ReferenceViews {
            machine: CReferenceMachine {
                name: strings[0].as_ptr(),
                short_name: strings[1].as_ptr(),
                os_name: strings[2].as_ptr(),
                os_version: strings[3].as_ptr(),
                cpu_architecture: strings[4].as_ptr(),
                memory_bytes: PI_REFERENCE_MACHINE.memory_bytes,
                cpu_cores: PI_REFERENCE_MACHINE.cpu_cores,
            },
            run: CReferenceRun {
                started_at_unix_seconds: PI_REFERENCE_RUN.started_at_unix_seconds,
                start_temperature_c: PI_REFERENCE_RUN.start_temperature_c,
                end_temperature_c: PI_REFERENCE_RUN.end_temperature_c,
                warmup_seconds: PI_REFERENCE_RUN.warmup_seconds,
                result_count: PI_REFERENCE_RUN.results.len() as u64,
            },
        }
    })
}

#[no_mangle]
pub unsafe extern "C" fn benchset_reference_machine(out: *mut CReferenceMachine, size: u32) -> i32 {
    if out.is_null() || size as usize != std::mem::size_of::<CReferenceMachine>() {
        return 0;
    }
    out.write(reference_views().machine);
    1
}

#[no_mangle]
pub unsafe extern "C" fn benchset_reference_run(out: *mut CReferenceRun, size: u32) -> i32 {
    if out.is_null() || size as usize != std::mem::size_of::<CReferenceRun>() {
        return 0;
    }
    out.write(reference_views().run);
    1
}

#[no_mangle]
pub unsafe extern "C" fn benchset_reference_result(
    index: u64,
    out: *mut CReferenceBenchmarkResult,
    size: u32,
) -> i32 {
    if out.is_null()
        || size as usize != std::mem::size_of::<CReferenceBenchmarkResult>()
        || index >= PI_REFERENCE_RUN.results.len() as u64
    {
        return 0;
    }
    out.write(CReferenceBenchmarkResult {
        benchmark: reference_strings().benchmarks[index as usize].as_ptr(),
        score: PI_REFERENCE_RUN.results[index as usize].score,
    });
    1
}

#[repr(C)]
#[cfg_attr(test, derive(Default))]
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
        warmup_seconds: DEFAULT_WARMUP_SECONDS,
        measurement_seconds: DEFAULT_MEASUREMENT_SECONDS,
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
    out.write(legacy_result(result));
    1
}

fn legacy_result(result: &BenchmarkResult) -> CResult {
    CResult {
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
    }
}

#[no_mangle]
pub unsafe extern "C" fn benchset_result_v2(
    handle: *const Handle,
    out: *mut CResultV2,
    size: u32,
) -> i32 {
    if handle.is_null() || out.is_null() || size as usize != std::mem::size_of::<CResultV2>() {
        return 0;
    }
    let stored = (*handle).result.lock().unwrap_or_else(|e| e.into_inner());
    let result = match &*stored {
        Some(stored) => &stored.result,
        None => return 0,
    };
    out.write(extended_result(result));
    1
}

fn extended_result(result: &BenchmarkResult) -> CResultV2 {
    CResultV2 {
        base: legacy_result(result),
        pi_equivalent: result.pi_equivalent.unwrap_or(0.0),
        has_pi_equivalent: u32::from(result.pi_equivalent.is_some()),
    }
}

#[no_mangle]
pub unsafe extern "C" fn benchset_result_v3(
    handle: *const Handle,
    out: *mut CResultV3,
    size: u32,
) -> i32 {
    if handle.is_null() || out.is_null() || size as usize != std::mem::size_of::<CResultV3>() {
        return 0;
    }
    let stored = (*handle).result.lock().unwrap_or_else(|e| e.into_inner());
    let result = match &*stored {
        Some(stored) => &stored.result,
        None => return 0,
    };
    let views = reference_views();
    out.write(CResultV3 {
        base: extended_result(result),
        reference_machine: &views.machine,
        reference_run: &views.run,
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
    let Some(sample) = stored
        .as_ref()
        .and_then(|stored| stored.result.samples.get(index as usize))
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

#[cfg(test)]
#[path = "ffi_reference_tests.rs"]
mod reference_tests;
