use super::*;
use crate::{Phase, Sample};
use std::ffi::CStr;
use std::mem::{size_of, MaybeUninit};

struct Owned(*mut Handle);
impl Owned {
    fn new() -> Self {
        let value = benchset_create();
        assert!(!value.is_null());
        Self(value)
    }
}
impl Drop for Owned {
    fn drop(&mut self) {
        unsafe { benchset_free(self.0) }
    }
}
fn config() -> Config {
    let mut value = MaybeUninit::uninit();
    assert_eq!(unsafe { benchset_default_config(value.as_mut_ptr()) }, 1);
    unsafe { value.assume_init() }
}
unsafe fn result(handle: *const Handle) -> CResult {
    let mut out = MaybeUninit::uninit();
    assert_eq!(benchset_result(handle, out.as_mut_ptr()), 1);
    out.assume_init()
}
unsafe fn reason(handle: *const Handle) -> String {
    CStr::from_ptr(benchset_reason(handle))
        .to_str()
        .unwrap()
        .into()
}

#[test]
fn defaults_versions_and_layout() {
    let c = config();
    assert_eq!(benchset_abi_version(), ABI_VERSION);
    assert_eq!(
        unsafe { CStr::from_ptr(benchset_version()) }
            .to_str()
            .unwrap(),
        crate::VERSION
    );
    assert_eq!(
        (c.abi_version, c.struct_size, c.benchmark, c.reserved),
        (1, 32, 1, 0)
    );
    assert_eq!((c.warmup_seconds, c.measurement_seconds), (120.0, 60.0));
    assert_eq!(
        (
            size_of::<Config>(),
            size_of::<CProgress>(),
            size_of::<CResult>(),
            size_of::<CSample>()
        ),
        (32, 40, 88, 32)
    );
}

#[test]
fn nullable_arguments_and_getters_before_run_preserve_outputs() {
    unsafe {
        let h = Owned::new();
        let mut r = CResult::default();
        r.status = 999;
        let mut s = CSample {
            iterations: 999,
            ..CSample::default()
        };
        assert_eq!(benchset_default_config(ptr::null_mut()), 0);
        assert_eq!(
            benchset_run(ptr::null_mut(), ptr::null(), None, ptr::null_mut()),
            2
        );
        for handle in [ptr::null(), h.0 as *const Handle] {
            assert_eq!(benchset_result(handle, &mut r), 0);
            assert_eq!(benchset_sample(handle, 0, &mut s), 0);
            assert!(benchset_reason(handle).is_null());
        }
        assert_eq!(r.status, 999);
        assert_eq!(s.iterations, 999);
        assert_eq!(benchset_result(h.0, ptr::null_mut()), 0);
        assert_eq!(benchset_sample(h.0, 0, ptr::null_mut()), 0);
        benchset_cancel(ptr::null_mut());
        benchset_free(ptr::null_mut());
        assert_eq!(benchset_run(h.0, ptr::null(), None, ptr::null_mut()), 2);
        assert_eq!(reason(h.0), "config is null");
    }
}

#[test]
fn config_prefix_is_checked_before_tail_and_invalid_handles_are_single_use() {
    unsafe {
        #[repr(C)]
        struct Prefix {
            abi: u32,
            size: u32,
        }
        let prefix = Prefix {
            abi: ABI_VERSION,
            size: 8,
        };
        let h = Owned::new();
        assert_eq!(
            benchset_run(
                h.0,
                (&prefix as *const Prefix).cast(),
                None,
                ptr::null_mut()
            ),
            2
        );
        assert!(reason(h.0).contains("incompatible"));
        let first_reason = benchset_reason(h.0);
        assert_eq!(benchset_run(h.0, &config(), None, ptr::null_mut()), 2);
        assert_eq!(benchset_reason(h.0), first_reason);
        assert!(reason(h.0).contains("incompatible"));
    }
}

#[test]
fn invalid_config_matrix() {
    let mut cases = Vec::new();
    for abi in [0, 2, u32::MAX] {
        cases.push((
            Config {
                abi_version: abi,
                ..config()
            },
            "incompatible",
        ));
    }
    for size in [0, 31, 33, u32::MAX] {
        cases.push((
            Config {
                struct_size: size,
                ..config()
            },
            "incompatible",
        ));
    }
    for benchmark in [0, 3, u32::MAX] {
        cases.push((
            Config {
                benchmark,
                ..config()
            },
            "unknown benchmark",
        ));
    }
    cases.push((
        Config {
            reserved: 1,
            ..config()
        },
        "reserved",
    ));
    for seconds in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY, -1.0, f64::MAX] {
        cases.push((
            Config {
                warmup_seconds: seconds,
                ..config()
            },
            "durations",
        ));
        cases.push((
            Config {
                measurement_seconds: seconds,
                ..config()
            },
            "durations",
        ));
    }
    for seconds in [0.0, -0.0] {
        cases.push((
            Config {
                measurement_seconds: seconds,
                ..config()
            },
            "durations",
        ));
    }
    cases.push((
        Config {
            measurement_seconds: 1e-12,
            ..config()
        },
        "must be positive",
    ));
    for (c, expected) in cases {
        let h = Owned::new();
        unsafe {
            assert_eq!(benchset_run(h.0, &c, None, ptr::null_mut()), 2);
            assert!(reason(h.0).contains(expected), "{}", reason(h.0));
            let r = result(h.0);
            assert_eq!(r.status, 2);
            assert_eq!(r.score, 0.0);
            assert_eq!(r.sample_count, 0);
            assert_eq!(r.has_variation, 0);
        }
    }
}

struct Context {
    handle: *mut Handle,
    thread: std::thread::ThreadId,
    same_thread: bool,
    events: Vec<CProgress>,
    cancel: bool,
}
unsafe extern "C" fn callback(p: *const CProgress, user: *mut c_void) {
    let context = &mut *user.cast::<Context>();
    let p = &*p;
    context.same_thread &= context.thread == std::thread::current().id();
    context.events.push(CProgress {
        phase: p.phase,
        workers: p.workers,
        fraction: p.fraction,
        phase_elapsed_seconds: p.phase_elapsed_seconds,
        phase_duration_seconds: p.phase_duration_seconds,
        iterations: p.iterations,
    });
    if context.cancel && p.phase == Phase::Measurement as u32 {
        benchset_cancel(context.handle);
    }
}

#[test]
fn controlled_callback_preserves_every_progress_field() {
    let phases = [
        Phase::Warmup,
        Phase::Measurement,
        Phase::Completed,
        Phase::Cancelled,
        Phase::Failed,
    ];
    unsafe {
        let h = Owned::new();
        let mut context = Context {
            handle: h.0,
            thread: std::thread::current().id(),
            same_thread: true,
            events: vec![],
            cancel: false,
        };
        assert_eq!(
            execute(
                h.0,
                &config(),
                Some(callback),
                (&mut context as *mut Context).cast(),
                &mut |benchmark, options, _, notify| {
                    for phase in phases {
                        notify(Progress {
                            phase,
                            workers: u32::MAX,
                            fraction: 0.12345678901234567,
                            phase_elapsed_seconds: 1.2345678901234567,
                            phase_duration_seconds: 2.345678901234567,
                            iterations: u64::MAX,
                        });
                    }
                    BenchmarkResult::failed(benchmark, options, "test fixture".into())
                },
            ),
            Status::Failed as u32
        );
        assert!(context.same_thread);
        assert_eq!(context.events.len(), phases.len());
        for (actual, expected_phase) in context.events.iter().zip([1, 2, 3, 4, 5]) {
            assert_eq!(
                (
                    actual.phase,
                    actual.workers,
                    actual.fraction,
                    actual.phase_elapsed_seconds,
                    actual.phase_duration_seconds,
                    actual.iterations,
                ),
                (
                    expected_phase,
                    u32::MAX,
                    0.12345678901234567,
                    1.2345678901234567,
                    2.345678901234567,
                    u64::MAX
                )
            );
        }
    }
}

#[test]
fn live_callbacks_results_samples_precancel_and_single_use() {
    let _serial = crate::tests::TEST_LOCK.lock().unwrap();
    unsafe {
        for benchmark in [1, 2] {
            for cancel in [false, true] {
                let h = Owned::new();
                let c = Config {
                    benchmark,
                    warmup_seconds: -0.0,
                    measurement_seconds: 0.04,
                    ..config()
                };
                let mut context = Context {
                    handle: h.0,
                    thread: std::thread::current().id(),
                    same_thread: true,
                    events: vec![],
                    cancel,
                };
                let status = benchset_run(
                    h.0,
                    &c,
                    Some(callback),
                    (&mut context as *mut Context).cast(),
                );
                assert_eq!(status, u32::from(cancel));
                assert!(context.same_thread);
                assert_eq!(context.events[0].phase, 2);
                assert_eq!(
                    context.events.last().unwrap().phase,
                    if cancel { 4 } else { 3 }
                );
                let r = result(h.0);
                assert_eq!(r.benchmark, benchmark);
                assert_eq!(r.abi_version, 1);
                assert_eq!(r.status, status);
                assert_eq!(context.events.last().unwrap().iterations, r.iterations);
                if !cancel {
                    assert!(r.score > 0.0);
                    assert_eq!(r.sample_count, 1);
                    assert_eq!(reason(h.0), "");
                    let mut sample = MaybeUninit::uninit();
                    assert_eq!(benchset_sample(h.0, 0, sample.as_mut_ptr()), 1);
                    let sample = sample.assume_init();
                    assert_eq!(sample.iterations, r.iterations);
                    assert_eq!(sample.duration_seconds, r.elapsed_seconds);
                    assert_eq!(sample.elapsed_seconds, r.elapsed_seconds);
                    assert_eq!(sample.iterations_per_second, r.score);
                } else {
                    assert_eq!(r.score, 0.0);
                    assert_eq!(reason(h.0), "cancelled");
                }
                let old_reason = benchset_reason(h.0);
                assert_eq!(benchset_run(h.0, &c, None, ptr::null_mut()), 2);
                assert_eq!(result(h.0).status, status);
                assert_eq!(benchset_reason(h.0), old_reason);
                let mut sample = CSample {
                    iterations: 999,
                    ..CSample::default()
                };
                for index in [r.sample_count, u64::MAX] {
                    assert_eq!(benchset_sample(h.0, index, &mut sample), 0);
                }
                assert_eq!(sample.iterations, 999);
            }
        }
        let h = Owned::new();
        benchset_cancel(h.0);
        assert_eq!(
            benchset_run(
                h.0,
                &Config {
                    warmup_seconds: 0.0,
                    measurement_seconds: 0.03,
                    ..config()
                },
                None,
                ptr::null_mut()
            ),
            1
        );
        assert_eq!(reason(h.0), "cancelled before starting");
        // Completed run with no callback exercises the optional function path.
        let h = Owned::new();
        assert_eq!(
            benchset_run(
                h.0,
                &Config {
                    warmup_seconds: 0.0,
                    measurement_seconds: 0.03,
                    ..config()
                },
                None,
                ptr::null_mut()
            ),
            0
        );
    }
}

#[test]
fn panic_boundary_create_and_poison_recovery() {
    assert!(create_handle(&mut || panic!("constructor fault")).is_null());
    unsafe {
        let h = Owned::new();
        let poisoned = std::panic::catch_unwind(AssertUnwindSafe(|| {
            let _guard = (*h.0).result.lock().unwrap();
            panic!("poison");
        }));
        assert!(poisoned.is_err());
        assert_eq!(
            execute(
                h.0,
                &config(),
                None,
                ptr::null_mut(),
                &mut |_, _, _, _| panic!("runner fault")
            ),
            2
        );
        assert_eq!(reason(h.0), "native benchmark panicked");
        assert_eq!(result(h.0).status, 2);
        assert_eq!(benchset_sample(h.0, 0, ptr::null_mut()), 0);
        let mut out = MaybeUninit::uninit();
        assert_eq!(benchset_sample(h.0, 0, out.as_mut_ptr()), 0);
    }
}

#[test]
fn complete_result_mapping_unicode_nul_and_large_unsigned_fields() {
    unsafe {
        for status in [Status::Completed, Status::Cancelled, Status::Failed] {
            for variation in [None, Some(0.0), Some(12.345678901234567)] {
                let h = Owned::new();
                assert_eq!(
                    execute(
                        h.0,
                        &config(),
                        None,
                        ptr::null_mut(),
                        &mut |benchmark, options, _, _| {
                            let mut r = BenchmarkResult::failed(
                                benchmark,
                                options,
                                "проверка\0reason".into(),
                            );
                            r.status = status;
                            r.score = if status == Status::Completed {
                                Some(123.5)
                            } else {
                                None
                            };
                            r.workers = u32::MAX;
                            r.elapsed_seconds = 4.0;
                            r.iterations = u64::MAX;
                            r.checksum = u64::MAX - 1;
                            r.variation_percent = variation;
                            r.unstable = true;
                            r.samples = vec![
                                Sample {
                                    elapsed_seconds: 1.0,
                                    duration_seconds: 1.0,
                                    iterations: u64::MAX,
                                    iterations_per_second: 42.123456789012345,
                                },
                                Sample {
                                    elapsed_seconds: 4.0,
                                    duration_seconds: 3.0,
                                    iterations: 7,
                                    iterations_per_second: 123.98765432109876,
                                },
                            ];
                            r
                        }
                    ),
                    status as u32
                );
                let r = result(h.0);
                assert_eq!(r.status, status as u32);
                assert_eq!(
                    (r.workers, r.iterations, r.checksum),
                    (u32::MAX, u64::MAX, u64::MAX - 1)
                );
                assert_eq!(
                    (r.score, r.elapsed_seconds, r.variation_percent),
                    (
                        if status == Status::Completed {
                            123.5
                        } else {
                            0.0
                        },
                        4.0,
                        variation.unwrap_or(0.0)
                    )
                );
                assert_eq!(
                    (r.unstable, r.has_variation, r.sample_count),
                    (1, u32::from(variation.is_some()), 2)
                );
                assert_eq!(reason(h.0), "проверка reason");
                for (index, expected) in [
                    (1.0, 1.0, u64::MAX, 42.123456789012345),
                    (4.0, 3.0, 7, 123.98765432109876),
                ]
                .into_iter()
                .enumerate()
                {
                    let mut out = MaybeUninit::uninit();
                    assert_eq!(benchset_sample(h.0, index as u64, out.as_mut_ptr()), 1);
                    let s = out.assume_init();
                    assert_eq!(
                        (
                            s.elapsed_seconds,
                            s.duration_seconds,
                            s.iterations,
                            s.iterations_per_second
                        ),
                        expected
                    );
                }
            }
        }
    }
}
