use super::*;

pub(super) fn duration_error(warmup: f64, measurement: f64) -> u32 {
    if !crate::nonnegative(warmup) {
        2
    } else if !crate::positive(measurement) {
        3
    } else {
        0
    }
}

/// Length-delimited UTF-8 keeps unknown names, including embedded NUL, unknown.
#[no_mangle]
pub unsafe extern "C" fn benchset_benchmark_id(name: *const u8, size: u64) -> u32 {
    if name.is_null() || size > isize::MAX as u64 {
        return 0;
    }
    std::str::from_utf8(std::slice::from_raw_parts(name, size as usize))
        .ok()
        .and_then(Benchmark::from_name)
        .map_or(0, |benchmark| benchmark as u32)
}

pub type NameMatcher = Option<unsafe extern "C" fn(*const u8, u64, *mut c_void) -> i32>;

/// The catalog selects comparison order; execution retains the legacy final fallback.
#[no_mangle]
pub unsafe extern "C" fn benchset_benchmark_match(
    matcher: NameMatcher,
    user: *mut c_void,
    execution: u32,
) -> u32 {
    let Some(matcher) = matcher else { return 0 };
    for (index, benchmark) in crate::BENCHMARKS.into_iter().enumerate() {
        if execution != 0 && index + 1 == crate::BENCHMARKS.len() {
            return benchmark as u32;
        }
        let name = benchmark.name().as_bytes();
        match matcher(name.as_ptr(), name.len() as u64, user) {
            value if value > 0 => return benchmark as u32,
            0 => {}
            _ => return 0,
        }
    }
    0
}

#[no_mangle]
pub extern "C" fn benchset_duration_error(warmup: f64, measurement: f64) -> u32 {
    duration_error(warmup, measurement)
}

#[no_mangle]
pub extern "C" fn benchset_validation_error(benchmark: u32, warmup: f64, measurement: f64) -> u32 {
    if Benchmark::from_id(benchmark).is_none() {
        1
    } else {
        duration_error(warmup, measurement)
    }
}

/// Messages belong to the core; bindings only select their parameter spelling.
#[no_mangle]
pub extern "C" fn benchset_validation_message(error: u32, cli: u32) -> *const c_char {
    let message: &[u8] = match (error, cli != 0) {
        (1, _) => b"unknown benchmark\0",
        (2, false) => b"warmup_seconds must be finite and nonnegative\0",
        (3, false) => b"measurement_seconds must be finite and positive\0",
        (2, true) => b"--warmup must be finite and nonnegative\0",
        (3, true) => b"--duration must be finite and positive\0",
        _ => b"\0",
    };
    message.as_ptr().cast()
}

#[cfg(test)]
#[path = "config_ffi_tests.rs"]
mod tests;
