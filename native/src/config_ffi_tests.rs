use super::*;
use std::ffi::CStr;

/// Host equality follows catalog order, accepts positive matches, stops on errors,
/// and execution uses its final fallback without requesting another comparison.
#[test]
fn host_name_matching_preserves_comparison_order_and_fallback() {
    struct State {
        answers: [i32; 2],
        names: Vec<String>,
    }
    unsafe extern "C" fn matches(name: *const u8, size: u64, user: *mut c_void) -> i32 {
        let state = &mut *user.cast::<State>();
        let answer = state.answers[state.names.len()];
        state.names.push(
            std::str::from_utf8(std::slice::from_raw_parts(name, size as usize))
                .unwrap()
                .to_owned(),
        );
        answer
    }
    unsafe {
        assert_eq!(benchset_benchmark_match(None, ptr::null_mut(), 0), 0);
        for (answers, execution, expected, calls) in [
            ([0, 7], 0, 2, 2),
            ([0, -1], 1, 2, 1),
            ([7, -1], 0, 1, 1),
            ([-1, 7], 0, 0, 1),
            ([0, 0], 0, 0, 2),
        ] {
            let mut state = State {
                answers,
                names: Vec::new(),
            };
            assert_eq!(
                benchset_benchmark_match(
                    Some(matches),
                    (&mut state as *mut State).cast(),
                    execution
                ),
                expected
            );
            let expected_names = crate::BENCHMARKS.map(|benchmark| benchmark.name());
            assert_eq!(state.names, expected_names[..calls]);
        }
    }
}

/// Name lookup uses every UTF-8 byte and never treats embedded NUL as terminator.
#[test]
fn names_are_length_delimited_and_catalog_driven() {
    unsafe {
        for benchmark in crate::BENCHMARKS {
            let name = benchmark.name().as_bytes();
            assert_eq!(
                benchset_benchmark_id(name.as_ptr(), name.len() as u64),
                benchmark as u32
            );
            assert_eq!(benchset_benchmark_id(name.as_ptr(), 0), 0);
        }
        for name in [
            b"single_core\0suffix".as_slice(),
            "неизвестный".as_bytes(),
            &[0xff],
            b"SINGLE_CORE",
            b" single_core",
            b"single_core\n",
            b"single-core",
            b"all",
            b"single_core_suffix",
            b"prefix_single_core",
        ] {
            assert_eq!(benchset_benchmark_id(name.as_ptr(), name.len() as u64), 0);
        }
        assert_eq!(benchset_benchmark_id(ptr::null(), 0), 0);
        assert_eq!(benchset_benchmark_id(ptr::null(), 1), 0);
        assert_eq!(benchset_benchmark_id(b"x".as_ptr(), u64::MAX), 0);
    }
}

/// Bindings share error precedence and exact diagnostics, including NaN and signed zero.
#[test]
fn validation_rules_and_messages_preserve_each_interface() {
    assert_eq!(benchset_validation_error(999, -1.0, 0.0), 1);
    for warmup in [-1.0, f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        assert_eq!(benchset_validation_error(1, warmup, 0.0), 2);
        assert_eq!(benchset_duration_error(warmup, 0.0), 2);
    }
    for measurement in [-1.0, -0.0, f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        assert_eq!(benchset_validation_error(2, -0.0, measurement), 3);
    }
    for (warmup, measurement) in [(0.0, 1.0), (-0.0, 1e-12), (f64::MAX, f64::MAX)] {
        assert_eq!(benchset_validation_error(1, warmup, measurement), 0);
    }
    for (code, api, cli) in [
        (0, "", ""),
        (1, "unknown benchmark", "unknown benchmark"),
        (
            2,
            "warmup_seconds must be finite and nonnegative",
            "--warmup must be finite and nonnegative",
        ),
        (
            3,
            "measurement_seconds must be finite and positive",
            "--duration must be finite and positive",
        ),
        (999, "", ""),
    ] {
        unsafe {
            assert_eq!(
                CStr::from_ptr(benchset_validation_message(code, 0))
                    .to_str()
                    .unwrap(),
                api
            );
            for flag in [1, 7, u32::MAX] {
                assert_eq!(
                    CStr::from_ptr(benchset_validation_message(code, flag))
                        .to_str()
                        .unwrap(),
                    cli
                );
            }
        }
    }
}

/// Refactoring shared rules must not replace the original C diagnostic vocabulary
/// with Python/CLI parameter names, nor hide the requested invalid double values.
#[test]
fn existing_c_calls_keep_their_exact_validation_diagnostics() {
    unsafe {
        for (benchmark, reserved, warmup, measurement, expected) in [
            (999, 0, -1.0, 0.0, "unknown benchmark"),
            (1, 1, -1.0, 0.0, "reserved config field must be zero"),
            (
                1,
                0,
                -1.0,
                1.0,
                "durations must be finite: warmup >= 0, measurement > 0",
            ),
            (
                2,
                0,
                f64::NAN,
                f64::INFINITY,
                "durations must be finite: warmup >= 0, measurement > 0",
            ),
            (
                2,
                0,
                0.0,
                -0.0,
                "durations must be finite: warmup >= 0, measurement > 0",
            ),
            (
                1,
                0,
                f64::MAX,
                1.0,
                "durations must be finite: warmup >= 0, measurement > 0",
            ),
            (1, 0, 0.0, 1e-12, "measurement duration must be positive"),
        ] {
            let handle = benchset_create();
            let config = Config {
                abi_version: ABI_VERSION,
                struct_size: std::mem::size_of::<Config>() as u32,
                benchmark,
                reserved,
                warmup_seconds: warmup,
                measurement_seconds: measurement,
            };
            assert_eq!(
                benchset_run(handle, &config, None, ptr::null_mut()),
                Status::Failed as u32
            );
            assert_eq!(
                CStr::from_ptr(benchset_reason(handle)).to_str().unwrap(),
                expected
            );
            let mut view = std::mem::MaybeUninit::<CResult>::uninit();
            assert_eq!(benchset_result(handle, view.as_mut_ptr()), 1);
            let view = view.assume_init();
            assert_eq!(view.warmup_seconds.to_bits(), warmup.to_bits());
            assert_eq!(view.measurement_seconds.to_bits(), measurement.to_bits());
            benchset_free(handle);
        }
    }
}
